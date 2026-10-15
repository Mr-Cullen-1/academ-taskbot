"""Verified Telegram Login Widget proof and database-backed web sessions."""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from taskbot_api.models import LoginChallenge, LoginProof, User, UserRole, WebSession, now, utc


COOKIE_NAME = "taskbot_session"
CHALLENGE_COOKIE = "taskbot_login_challenge"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class TelegramProof(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int = Field(gt=0)
    auth_date: int
    hash: str
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    photo_url: str | None = None


def verify_telegram_proof(proof: TelegramProof, bot_token: str, max_age: int) -> None:
    try:
        timestamp = datetime.fromtimestamp(proof.auth_date, timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise HTTPException(401, "TELEGRAM_PROOF_EXPIRED") from None
    age = (now() - timestamp).total_seconds()
    if age < -30 or age > max_age:
        raise HTTPException(401, "TELEGRAM_PROOF_EXPIRED")
    data = proof.model_dump(exclude_none=True, exclude={"hash"})
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    key = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(key, check_string.encode(), hashlib.sha256).hexdigest()
    if len(proof.hash) != 64 or not hmac.compare_digest(expected, proof.hash):
        raise HTTPException(401, "INVALID_TELEGRAM_PROOF")


async def get_db(request: Request):
    factory = request.app.state.session_factory
    if factory is None:
        raise HTTPException(503, "DATABASE_NOT_CONFIGURED")
    async with factory() as session:
        yield session


Db = Annotated[AsyncSession, Depends(get_db)]


async def current_user(request: Request, db: Db) -> User:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        raise HTTPException(401, "AUTH_REQUIRED")
    session = await db.scalar(select(WebSession).where(WebSession.credential_digest == digest(raw)))
    if session is None or session.revoked_at is not None or utc(session.expires_at) <= now():
        raise HTTPException(401, "SESSION_INVALID")
    user = await db.get(User, session.user_id)
    if user is None or user.status != "ACTIVE":
        raise HTTPException(403, "ACCOUNT_INACTIVE")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        csrf = request.headers.get("X-CSRF-Token", "")
        if not csrf or not hmac.compare_digest(session.csrf_digest, digest(csrf)):
            raise HTTPException(403, "CSRF_INVALID")
    request.state.web_session = session
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def role_codes(db: AsyncSession, user_id) -> set[str]:
    return set((await db.scalars(select(UserRole.code).where(UserRole.user_id == user_id))).all())


async def require_super_admin(db: AsyncSession, user: User) -> None:
    if "SUPER_ADMIN" not in await role_codes(db, user.id):
        raise HTTPException(403, "FORBIDDEN")


def new_token() -> str:
    return secrets.token_urlsafe(32)


def new_session(user: User, hours: int) -> tuple[WebSession, str, str]:
    token, csrf = new_token(), new_token()
    session = WebSession(user_id=user.id, credential_digest=digest(token), csrf_digest=digest(csrf),
                         expires_at=now() + timedelta(hours=hours))
    return session, token, csrf
