"""Telegram polling registration state, persisted and deduplicated by update_id."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from taskbot_api.models import AuditLog, RegistrationDraft, TelegramUpdate, User, now


@dataclass(frozen=True)
class RegistrationEvent:
    update_id: int
    telegram_user_id: int
    telegram_chat_id: int
    kind: str  # START, CONTACT, NAME
    contact_user_id: int | None = None
    phone: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class RegistrationResult:
    code: str
    duplicate: bool = False


async def process_registration(
    factory: async_sessionmaker[AsyncSession], event: RegistrationEvent
) -> RegistrationResult:
    if event.telegram_user_id <= 0 or event.telegram_chat_id != event.telegram_user_id:
        return RegistrationResult("PRIVATE_CHAT_REQUIRED")
    async with factory() as session:
        try:
            async with session.begin():
                session.add(TelegramUpdate(update_id=event.update_id, response_code="PROCESSING"))
                await session.flush()  # UNIQUE(update_id) serializes competing deliveries.
                result = await _apply(session, event)
                update = await session.get(TelegramUpdate, event.update_id)
                update.response_code = result
            return RegistrationResult(result)
        except IntegrityError:
            await session.rollback()
            update = await session.get(TelegramUpdate, event.update_id)
            if update is not None:
                return RegistrationResult(update.response_code, duplicate=True)
            raise


async def _apply(session: AsyncSession, event: RegistrationEvent) -> str:
    user = await session.scalar(select(User).where(User.telegram_user_id == event.telegram_user_id))
    if user is not None:
        return f"ACCOUNT_{user.status}"

    draft = await session.get(RegistrationDraft, event.telegram_user_id)
    if event.kind == "START":
        if draft is None:
            session.add(RegistrationDraft(telegram_user_id=event.telegram_user_id, telegram_chat_id=event.telegram_chat_id))
            return "CONTACT_REQUIRED"
        return "NAME_REQUIRED" if draft.stage == "NAME" else "CONTACT_REQUIRED"

    if draft is None:
        return "START_REQUIRED"
    if event.kind == "CONTACT":
        if event.contact_user_id != event.telegram_user_id or not event.phone or len(event.phone) > 32:
            return "INVALID_CONTACT"
        draft.phone = event.phone.strip()
        draft.stage = "NAME"
        draft.updated_at = now()
        return "NAME_REQUIRED"
    if event.kind == "NAME":
        name = (event.name or "").strip()
        if draft.stage != "NAME" or not draft.phone:
            return "CONTACT_REQUIRED"
        if not 2 <= len(name) <= 160:
            return "INVALID_NAME"
        user = User(telegram_user_id=event.telegram_user_id, telegram_chat_id=event.telegram_chat_id,
                    phone=draft.phone, name=name, status="PENDING")
        session.add(user)
        await session.flush()
        session.add(AuditLog(action="registration.submitted", target_type="user", target_id=str(user.id), details={}))
        await session.delete(draft)
        return "ACCOUNT_PENDING"
    return "UNSUPPORTED"
