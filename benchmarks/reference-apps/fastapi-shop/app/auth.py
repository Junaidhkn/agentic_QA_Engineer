from __future__ import annotations

import hashlib
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, EmailStr

from app import db

router = APIRouter(prefix="/auth", tags=["auth"])


class SignupIn(BaseModel):
    email: EmailStr
    password: str
    name: str


class LoginIn(BaseModel):
    email: EmailStr
    password: str


def _hash(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


@router.post("/signup", status_code=201)
def signup(body: SignupIn) -> dict[str, object]:
    if db.query("SELECT 1 FROM users WHERE email = ?", body.email):
        raise HTTPException(409, "email already registered")
    user_id = db.execute(
        "INSERT INTO users (email, name, password) VALUES (?, ?, ?)", body.email, body.name, _hash(body.password)
    )
    return {"id": user_id, "email": body.email, "name": body.name}


@router.post("/login")
def login(body: LoginIn) -> dict[str, str]:
    rows = db.query("SELECT id FROM users WHERE email = ? AND password = ?", body.email, _hash(body.password))
    if not rows:
        raise HTTPException(401, "invalid credentials")
    token = secrets.token_urlsafe(24)
    db.execute("INSERT INTO tokens (token, user_id) VALUES (?, ?)", token, rows[0]["id"])
    return {"access_token": token, "token_type": "bearer"}


def get_current_user(authorization: str | None = Header(default=None)) -> int:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing token")
    rows = db.query("SELECT user_id FROM tokens WHERE token = ?", authorization.split(" ", 1)[1])
    if not rows:
        raise HTTPException(401, "invalid token")
    return int(rows[0]["user_id"])


CurrentUser = Depends(get_current_user)
