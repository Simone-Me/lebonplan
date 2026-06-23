from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, status

from api.security import AUTH_USER, JWT_EXPIRE_MINUTES, authenticate_user, create_access_token, require_auth

router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_minutes: int


@router.post("/auth/login", response_model=TokenResponse, tags=["Auth"])
def login(payload: LoginRequest):
    if not authenticate_user(payload.username, payload.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    token = create_access_token(payload.username)
    return TokenResponse(access_token=token, expires_in_minutes=JWT_EXPIRE_MINUTES)


@router.get("/auth/me", tags=["Auth"])
def me(claims: dict = Depends(require_auth)):
    return {"username": claims.get("sub", AUTH_USER)}
