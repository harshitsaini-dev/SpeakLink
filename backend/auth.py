"""JWT + bcrypt auth for HQ users. Simple, single-file."""
import os
from datetime import datetime, timedelta, timezone
import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from db import get_db
from models import HQUser

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 8


def get_jwt_secret() -> str:
    return os.environ["JWT_SECRET"]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def create_access_token(user_id: int, username: str, session_version: int = 1) -> str:
    payload = {
        "sub": str(user_id),
        "username": username,
        # The whole revocation mechanism. Anything that must end this account's
        # sessions - a password change or reset, a role change, a disable, an
        # archive - increments the stored value, and every request below
        # compares the two. Without it a token stays valid for the rest of its
        # eight hours no matter what an administrator just did.
        "sv": int(session_version),
        "exp": datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS),
        "type": "access",
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


def _extract_token(request: Request) -> str:
    """Read the access token from the Authorization header, and nowhere else.

    This used to fall back to ``?token=`` in the query string, for the benefit
    of WebSockets. The HQ sockets now authenticate with single-use tickets, so
    the fallback protected nothing and was simply a second, worse way into
    every authenticated route.

    Worse, because a URL is the least private part of a request: it reaches
    application access logs, reverse-proxy logs, browser history, copied links,
    monitoring tools, screenshots and Referer headers. A reusable token sitting
    in one is a session anybody who can read a log line can take over. Ordinary
    HTTP has no excuse either - every client here, browsers included, can set a
    header perfectly well.

    The token is not echoed in the refusal, so a bad value cannot be read back
    out of an error response or a log that records it.
    """
    authorization = request.headers.get("Authorization", "")
    scheme, _, credential = authorization.partition(" ")
    if scheme == "Bearer" and credential.strip():
        return credential
    raise HTTPException(status_code=401, detail="Not authenticated")


def get_current_user(request: Request, db: Session = Depends(get_db)) -> HQUser:
    token = _extract_token(request)
    payload = decode_token(token)
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Invalid token type")
    user = db.query(HQUser).filter(HQUser.id == int(payload["sub"])).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")

    # A token minted before the account changed is no longer this account's
    # token. Tokens with no version at all are from before this existed and are
    # refused rather than trusted - the alternative is a permanent bypass that
    # anybody holding an older token keeps forever.
    current_version = int(getattr(user, "session_version", 1) or 1)
    if int(payload.get("sv", 0)) != current_version:
        raise HTTPException(status_code=401, detail="Session is no longer valid")
    return user
