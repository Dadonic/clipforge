import hashlib
import hmac
import os
import time

import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from . import config
from .db import User, get_db

COOKIE = "cf_session"
TTL = 60 * 60 * 24 * 14


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 240_000)
    return f"pbkdf2${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt, dk = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 240_000)
        return hmac.compare_digest(calc.hex(), dk)
    except Exception:
        return False


def make_token(user_id: int) -> str:
    return jwt.encode({"sub": str(user_id), "exp": int(time.time()) + TTL}, config.SECRET_KEY, algorithm="HS256")


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(COOKIE)
    if not token:
        h = request.headers.get("authorization", "")
        if h.lower().startswith("bearer "):
            token = h[7:]
    if not token:
        raise HTTPException(401, "Not signed in")
    try:
        uid = int(jwt.decode(token, config.SECRET_KEY, algorithms=["HS256"])["sub"])
    except Exception:
        raise HTTPException(401, "Session expired, sign in again")
    user = db.get(User, uid)
    if not user:
        raise HTTPException(401, "Account not found")
    return user
