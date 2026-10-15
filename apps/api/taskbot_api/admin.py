"""Explicit local bootstrap of the sole Super Admin after bot registration."""

import argparse
import asyncio

from sqlalchemy import select

from taskbot_api.config import get_settings
from taskbot_api.db import build_engine, session_factory
from taskbot_api.models import AuditLog, User, UserRole, now


async def bootstrap_super_admin(telegram_id: int) -> str:
    engine = build_engine(get_settings())
    if engine is None:
        raise RuntimeError("TASKBOT_DATABASE_URL is required")
    try:
        async with session_factory(engine)() as db:
            async with db.begin():
                existing = await db.scalar(select(UserRole).where(UserRole.super_admin_slot == 1))
                if existing is not None:
                    raise RuntimeError("Super Admin already exists")
                user = await db.scalar(select(User).where(User.telegram_user_id == telegram_id).with_for_update())
                if user is None or user.status != "PENDING":
                    raise RuntimeError("A verified PENDING bot registration for this Telegram ID is required")
                user.status, user.version, user.updated_at = "ACTIVE", user.version + 1, now()
                db.add(UserRole(user_id=user.id, code="SUPER_ADMIN", super_admin_slot=1))
                db.add(AuditLog(actor_user_id=user.id, action="super_admin.bootstrapped",
                                target_type="user", target_id=str(user.id), details={"method": "local_cli"}))
            return str(user.id)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap sole Super Admin from a verified pending bot registration")
    parser.add_argument("--telegram-id", type=int, required=True)
    args = parser.parse_args()
    if args.telegram_id <= 0:
        parser.error("--telegram-id must be positive")
    print(asyncio.run(bootstrap_super_admin(args.telegram_id)))


if __name__ == "__main__":
    main()
