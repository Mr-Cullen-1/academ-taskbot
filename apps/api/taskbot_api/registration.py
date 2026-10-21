"""Telegram polling registration state, persisted and deduplicated by update_id."""

from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from taskbot_api.models import (
    AuditLog, Notification, Project, ProjectMember, RegistrationDraft,
    TelegramUpdate, User, UserRole, now,
)
from taskbot_api.security import role_codes


REQUESTABLE_ROLES = {"PM", "TEAM_LEAD", "SUBJECT_DEVELOPER", "QA"}
ROLE_NAMES = {"PM": "менеджер проекта", "TEAM_LEAD": "руководитель команды",
              "SUBJECT_DEVELOPER": "предметник-разработчик", "QA": "QA-проверяющий"}


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


async def requested_access(db: AsyncSession, user_id: UUID) -> dict | None:
    """The immutable request snapshot lives in the existing registration audit log."""
    row = await db.scalar(select(AuditLog).where(
        AuditLog.action == "registration.requested",
        AuditLog.target_type == "user", AuditLog.target_id == str(user_id),
    ).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(1))
    return row.details if row else None


async def submit_access_request(factory: async_sessionmaker[AsyncSession], *,
                                update_id: int, telegram_user_id: int,
                                role: str, project_id: UUID | None) -> str:
    if role not in REQUESTABLE_ROLES:
        return "INVALID_ROLE"
    async with factory() as db:
        try:
            async with db.begin():
                update = TelegramUpdate(update_id=update_id, response_code="PROCESSING")
                db.add(update)
                await db.flush()
                user = await db.scalar(select(User).where(
                    User.telegram_user_id == telegram_user_id).with_for_update())
                if user is None or user.status != "PENDING":
                    update.response_code = "ACCOUNT_NOT_PENDING"
                    return "ACCOUNT_NOT_PENDING"
                if await requested_access(db, user.id):
                    update.response_code = "REQUEST_ALREADY_SUBMITTED"
                    return "REQUEST_ALREADY_SUBMITTED"
                project = await db.get(Project, project_id) if project_id else None
                if project_id and project is None:
                    update.response_code = "PROJECT_NOT_FOUND"
                    return "PROJECT_NOT_FOUND"
                admin = await db.scalar(select(User).join(UserRole, UserRole.user_id == User.id).where(
                    UserRole.code == "SUPER_ADMIN", User.status == "ACTIVE"))
                if admin is None:
                    update.response_code = "ADMIN_UNAVAILABLE"
                    return "ADMIN_UNAVAILABLE"
                db.add(AuditLog(action="registration.requested", target_type="user",
                                target_id=str(user.id), details={
                                    "role": role, "project_id": str(project.id) if project else None,
                                    "project_name": project.name if project else None,
                                }))
                db.add(Notification(
                    recipient_user_id=admin.id, subject_user_id=user.id,
                    kind="REGISTRATION_REQUEST",
                    dedupe_key=f"registration:{user.id}:request:{admin.id}",
                    body=(f"Заявка на доступ от {user.name}. Роль: {ROLE_NAMES.get(role, role)}. "
                          f"Проект: {project.name if project else 'не указан'}."),
                    scheduled_at=now(), state="PENDING",
                ))
                update.response_code = "REQUEST_SUBMITTED"
                return "REQUEST_SUBMITTED"
        except IntegrityError:
            await db.rollback()
            previous = await db.get(TelegramUpdate, update_id)
            if previous is not None:
                return previous.response_code
            raise


async def decide_pending_user(db: AsyncSession, actor: User, target: User, *,
                              status: str, roles: set[str] | None = None,
                              project_id: UUID | None = None,
                              reason: str | None = None) -> set[str]:
    """Shared transaction body for HTTP and Telegram approval."""
    if actor.status != "ACTIVE" or "SUPER_ADMIN" not in await role_codes(db, actor.id):
        raise HTTPException(403, "FORBIDDEN")
    if target.status != "PENDING":
        raise HTTPException(409, "INVALID_STATUS_TRANSITION")
    if status not in {"ACTIVE", "REJECTED"}:
        raise HTTPException(422, "INVALID_STATUS")
    chosen = set(roles or ())
    if status == "ACTIVE":
        if not chosen or chosen - REQUESTABLE_ROLES:
            raise HTTPException(422, "ACTIVE_ROLES_REQUIRED")
        project = await db.get(Project, project_id) if project_id else None
        if project_id and project is None:
            raise HTTPException(422, "PROJECT_NOT_FOUND")
        for code in sorted(chosen):
            db.add(UserRole(user_id=target.id, code=code, granted_by=actor.id))
        if project and await db.get(ProjectMember, (project.id, target.id)) is None:
            db.add(ProjectMember(project_id=project.id, user_id=target.id))
    elif roles is not None or project_id is not None:
        raise HTTPException(422, "ROLES_ONLY_ON_APPROVAL")
    target.status = status
    target.version += 1
    target.updated_at = now()
    db.add(AuditLog(actor_user_id=actor.id, action="user.status_changed",
                    target_type="user", target_id=str(target.id), details={
                        "from": "PENDING", "to": status, "roles": sorted(chosen),
                        "project_id": str(project_id) if project_id else None,
                        "reason": reason,
                    }))
    db.add(Notification(
        recipient_user_id=target.id, subject_user_id=target.id,
        kind="REGISTRATION_APPROVED" if status == "ACTIVE" else "REGISTRATION_REJECTED",
        dedupe_key=f"registration:{target.id}:decision",
        body=(("Ваша заявка одобрена. Назначенная роль: "
               + ", ".join(ROLE_NAMES.get(code, code) for code in sorted(chosen))
               + (f". Проект: {project.name}" if project else "")
               + ". Откройте задачи через бота.")
              if status == "ACTIVE" else "Ваша заявка отклонена. Обратитесь к администратору."),
        scheduled_at=now(), state="PENDING",
    ))
    return chosen
