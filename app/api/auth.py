"""Login / logout for the admin accounts defined in config.ADMIN_USERS (no sign-ups)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.security import client_ip, login_throttle, verify_credentials

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=200)


@router.post("/login")
async def login(body: LoginBody, request: Request) -> dict:
    ip = client_ip(request)
    locked = login_throttle.locked_for(ip)
    if locked > 0:
        raise HTTPException(429, f"Too many failed attempts. Try again in {int(locked // 60) + 1} minute(s).")
    if not verify_credentials(body.username.strip(), body.password):
        login_throttle.record_failure(ip)
        raise HTTPException(401, "Invalid username or password.")
    login_throttle.reset(ip)
    request.session.clear()
    request.session["user"] = body.username.strip()
    return {"ok": True, "user": body.username.strip()}


@router.post("/logout")
async def logout(request: Request) -> dict:
    request.session.clear()
    return {"ok": True}


@router.get("/me")
async def me(request: Request) -> dict:
    user = request.session.get("user")
    if not user:
        raise HTTPException(401, "Not logged in")
    return {"user": user}
