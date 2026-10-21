"""Private personal reminders, delivered by the existing Notification worker."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from taskbot_api.models import Notification, PersonalReminder, User, now, utc
from taskbot_api.routes import mutation
from taskbot_api.security import CurrentUser, Db
from taskbot_api.task_service import visible_task


router = APIRouter(prefix="/api/v1/reminders", tags=["reminders"])


class ReminderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: UUID | None = None
    remind_at: datetime
    text: str = Field(default="Личное напоминание", min_length=1, max_length=1000)


class ReminderEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    remind_at: datetime | None = None
    text: str | None = Field(default=None, min_length=1, max_length=1000)


class ReminderDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


def reminder_json(row: PersonalReminder) -> dict:
    return {"id": str(row.id), "task_id": str(row.task_id) if row.task_id else None,
            "remind_at": utc(row.remind_at).isoformat(), "text": row.text,
            "version": row.version}


def validate_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(422, "TIMEZONE_REQUIRED")
    instant = value.astimezone(utc(now()).tzinfo)
    if instant <= now():
        raise HTTPException(422, "REMINDER_MUST_BE_FUTURE")
    return instant


async def queue_reminder(db: Db, row: PersonalReminder, actor: User) -> None:
    task = await visible_task(db, actor, row.task_id) if row.task_id else None
    context = (f" по задаче {task.task_key} «{task.title}»" if task else "")
    db.add(Notification(recipient_user_id=row.owner_user_id, task_id=row.task_id,
                        kind="PERSONAL_REMINDER",
                        dedupe_key=f"personal_reminder:{row.id}:{row.version}",
                        body=f"Напоминание{context}: {row.text}",
                        scheduled_at=row.remind_at, state="PENDING"))


@router.get("")
async def reminders(db: Db, actor: CurrentUser, limit: int = 50) -> list[dict]:
    if not 1 <= limit <= 100:
        raise HTTPException(422, "INVALID_PAGE_LIMIT")
    rows = (await db.scalars(select(PersonalReminder).where(
        PersonalReminder.owner_user_id == actor.id,
        PersonalReminder.cancelled_at.is_(None))
        .order_by(PersonalReminder.remind_at, PersonalReminder.id).limit(limit))).all()
    return [reminder_json(row) for row in rows]


@router.post("")
async def create_reminder(body: ReminderCreate, request: Request,
                          db: Db, actor: CurrentUser) -> Response:
    async def perform() -> tuple[int, dict]:
        if body.task_id:
            await visible_task(db, actor, body.task_id)
        row = PersonalReminder(owner_user_id=actor.id, task_id=body.task_id,
                               remind_at=validate_time(body.remind_at), text=body.text.strip())
        if not row.text:
            raise HTTPException(422, "REMINDER_TEXT_REQUIRED")
        db.add(row)
        await db.flush()
        await queue_reminder(db, row, actor)
        return 201, reminder_json(row)

    return await mutation(request, db, actor, body, "reminders:create", perform)


@router.patch("/{reminder_id}")
async def edit_reminder(reminder_id: UUID, body: ReminderEdit, request: Request,
                        db: Db, actor: CurrentUser) -> Response:
    existing = await db.get(PersonalReminder, reminder_id)
    if existing is None or existing.owner_user_id != actor.id or existing.cancelled_at is not None:
        raise HTTPException(404, "NOT_FOUND")
    if existing.task_id:
        await visible_task(db, actor, existing.task_id)

    async def perform() -> tuple[int, dict]:
        row = await db.get(PersonalReminder, reminder_id, with_for_update=True)
        if row is None or row.owner_user_id != actor.id or row.cancelled_at is not None:
            raise HTTPException(404, "NOT_FOUND")
        if row.version != body.expected_version:
            raise HTTPException(409, "VERSION_CONFLICT")
        if row.task_id:
            await visible_task(db, actor, row.task_id)
        if body.remind_at is not None:
            row.remind_at = validate_time(body.remind_at)
        if body.text is not None:
            if not body.text.strip():
                raise HTTPException(422, "REMINDER_TEXT_REQUIRED")
            row.text = body.text.strip()
        row.version += 1
        await queue_reminder(db, row, actor)
        return 200, reminder_json(row)

    return await mutation(request, db, actor, body, f"reminders:edit:{reminder_id}", perform)


@router.delete("/{reminder_id}")
async def delete_reminder(reminder_id: UUID, body: ReminderDelete, request: Request,
                          db: Db, actor: CurrentUser) -> Response:
    async def perform() -> tuple[int, dict]:
        row = await db.get(PersonalReminder, reminder_id, with_for_update=True)
        if row is None or row.owner_user_id != actor.id or row.cancelled_at is not None:
            raise HTTPException(404, "NOT_FOUND")
        if row.version != body.expected_version:
            raise HTTPException(409, "VERSION_CONFLICT")
        row.cancelled_at = now()
        row.version += 1
        return 200, {"cancelled": True}

    return await mutation(request, db, actor, body, f"reminders:delete:{reminder_id}", perform)
