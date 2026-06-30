import hashlib
import hmac
import os
import threading
import time
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from datetime import datetime, timedelta, timezone
from slowapi import Limiter
from slowapi.util import get_remote_address

RATE_LIMIT = 100
RATE_WINDOW = 60  # secondes

limiter = Limiter(key_func=get_remote_address, default_limits=[f"{RATE_LIMIT}/minute"])


class _RequestTracker:
    def __init__(self):
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def record(self, ip: str) -> None:
        now = time.time()
        with self._lock:
            self._hits[ip].append(now)
            cutoff = now - RATE_WINDOW
            self._hits[ip] = [t for t in self._hits[ip] if t > cutoff]

    def stats(self, ip: str) -> dict:
        now = time.time()
        with self._lock:
            cutoff = now - RATE_WINDOW
            hits = [t for t in self._hits.get(ip, []) if t > cutoff]
        remaining = max(0, RATE_LIMIT - len(hits))
        reset_in = int(RATE_WINDOW - (now - hits[0])) if hits else 0
        return {"limit": RATE_LIMIT, "remaining": remaining, "reset_in_seconds": max(0, reset_in)}


request_tracker = _RequestTracker()

load_dotenv(Path(__file__).parent.parent / ".env")

JWT_SECRET = os.environ.get("API_JWT_SECRET", "change-me-dev-jwt-secret")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = int(os.environ.get("API_JWT_EXPIRE_MINUTES", "120"))
AUTH_USER = os.environ.get("API_AUTH_USER", "admin")
AUTH_PASSWORD = os.environ.get("API_AUTH_PASSWORD", "change-me")
AUTH_PASSWORD_HASH = os.environ.get("API_AUTH_PASSWORD_HASH")
API_CORS_ORIGINS = os.environ.get(
    "API_CORS_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173",
)

bearer_scheme = HTTPBearer(auto_error=False)


def _pbkdf2_hash(password: str, salt: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()


def verify_password(password: str) -> bool:
    if not AUTH_PASSWORD_HASH:
        raise RuntimeError(
            "API_AUTH_PASSWORD_HASH must be set — plaintext passwords are not allowed. "
            "Generate a hash with: python -c \"import hashlib,os,hmac; s=os.urandom(16).hex(); "
            "h=hashlib.pbkdf2_hmac('sha256',b'yourpassword',s.encode(),260000).hex(); "
            "print(f'pbkdf2_sha256$260000${s}${h}')\""
        )
    try:
        scheme, iterations, salt, expected = AUTH_PASSWORD_HASH.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        candidate = _pbkdf2_hash(password, salt, int(iterations))
        return hmac.compare_digest(candidate, expected)
    except Exception:
        return False


def authenticate_user(username: str, password: str) -> bool:
    return hmac.compare_digest(username, AUTH_USER) and verify_password(password)


def get_cors_origins() -> list[str]:
    return [origin.strip() for origin in API_CORS_ORIGINS.split(",") if origin.strip()]


def create_access_token(subject: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES)
    payload = {
        "sub": subject,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token invalide ou expiré",
        ) from exc


def require_auth(credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)) -> dict:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    return decode_access_token(credentials.credentials)
