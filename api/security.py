import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

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


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(message: bytes) -> str:
    signature = hmac.new(JWT_SECRET.encode("utf-8"), message, hashlib.sha256).digest()
    return _b64url_encode(signature)


def _pbkdf2_hash(password: str, salt: str, iterations: int) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    ).hex()


def verify_password(password: str) -> bool:
    if AUTH_PASSWORD_HASH:
        try:
            scheme, iterations, salt, expected = AUTH_PASSWORD_HASH.split("$", 3)
            if scheme != "pbkdf2_sha256":
                return False
            candidate = _pbkdf2_hash(password, salt, int(iterations))
            return hmac.compare_digest(candidate, expected)
        except Exception:
            return False
    return hmac.compare_digest(password, AUTH_PASSWORD)


def authenticate_user(username: str, password: str) -> bool:
    return hmac.compare_digest(username, AUTH_USER) and verify_password(password)


def get_cors_origins() -> list[str]:
    return [origin.strip() for origin in API_CORS_ORIGINS.split(",") if origin.strip()]


def create_access_token(subject: str) -> str:
    header = {"alg": JWT_ALGORITHM, "typ": "JWT"}
    payload = {
        "sub": subject,
        "exp": int(time.time()) + JWT_EXPIRE_MINUTES * 60,
        "iat": int(time.time()),
    }
    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")
    signature_b64 = _sign(signing_input)
    return f"{header_b64}.{payload_b64}.{signature_b64}"


def decode_access_token(token: str) -> dict:
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token format") from exc

    signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")
    expected_signature = _sign(signing_input)
    if not hmac.compare_digest(expected_signature, signature_b64):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token signature")

    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload") from exc

    if payload.get("exp", 0) < int(time.time()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    return payload


def require_auth(credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)) -> dict:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    return decode_access_token(credentials.credentials)
