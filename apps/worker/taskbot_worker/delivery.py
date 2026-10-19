"""Durable outbox fan-out and opt-in Telegram delivery."""

import logging
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from taskbot_api.config import Settings
from taskbot_api.models import (
    Notification, NotificationAttempt, OutboxEvent, ProjectMember, Task,
    TaskEvent, User, WeeklyReport, now, utc,
)
from taskbot_api.reporting import TASHKENT, report_projects
from taskbot_api.security import role_codes
from taskbot_api.task_service import OVERDUE_STATUSES


log = logging.getLogger(__name__)
DEADLINE_KINDS = {"DUE_24H", "DUE_2H", "OVERDUE"}
REACTION_KINDS = {"NO_REACTION", "NO_RESPONSE_ALERT"}


class MessageTransport(Protocol):
    async def send(self, chat_id: int, body: str,
                   keyboard: InlineKeyboardMarkup | None = None) -> None: ...


class DryRunTransport:
    async def send(self, chat_id: int, body: str,
                   keyboard: InlineKeyboardMarkup | None = None) -> None:
        return


class TelegramTransport:
    def __init__(self, bot: Bot):
        self.bot = bot

    async def send(self, chat_id: int, body: str,
                   keyboard: InlineKeyboardMarkup | None = None) -> None:
        await self.bot.send_message(chat_id=chat_id, text=body, reply_markup=keyboard)


async def project_access(db: AsyncSession, user: User, project_id: UUID) -> bool:
    if await db.get(ProjectMember, (project_id, user.id)) is not None:
        return True
    return bool({"SUPER_ADMIN", "TEAM_LEAD"} & await role_codes(db, user.id))


async def put_notification(db: AsyncSession, *, recipient_id: UUID, kind: str,
                           key: str, body: str, scheduled_at: datetime,
                           task_id: UUID | None = None, source_event_id: UUID | None = None,
                           due_date_snapshot=None, subject_user_id: UUID | None = None) -> bool:
    try:
        async with db.begin_nested():
            db.add(Notification(recipient_user_id=recipient_id, subject_user_id=subject_user_id,
                                task_id=task_id, source_event_id=source_event_id,
                                kind=kind, dedupe_key=key, body=body[:4000],
                                scheduled_at=scheduled_at, due_date_snapshot=due_date_snapshot,
                                state="PENDING", attempts=0, created_at=now()))
            await db.flush()
        return True
    except IntegrityError:
        # The database UNIQUE key wins even when two workers race.
        return False


def event_recipients(event: TaskEvent, task: Task) -> list[tuple[str, UUID]]:
    before, after = event.before_data, event.after_data
    def user(key: str) -> UUID | None:
        value = after.get(key, getattr(task, key, None))
        return UUID(value) if isinstance(value, str) else value
    kind = event.type
    targets: list[tuple[str, UUID | None]] = []
    if kind == "CREATED":
        targets = [("ASSIGNED", user("assignee_id"))]
    elif kind == "UPDATED":
        if before.get("assignee_id") != after.get("assignee_id"):
            previous = before.get("assignee_id")
            targets = [("ASSIGNED", user("assignee_id")),
                       ("ASSIGNMENT_REMOVED", UUID(previous) if previous else None),
                       ("TASK_UPDATED", user("created_by"))]
        else:
            targets = [("TASK_UPDATED", user("assignee_id")),
                       ("TASK_UPDATED", user("created_by"))]
    elif kind == "QA_REVIEWER_CHANGED":
        previous = before.get("qa_reviewer_id")
        targets = [("QA_ASSIGNED", user("qa_reviewer_id")),
                   ("QA_UNASSIGNED", UUID(previous) if previous else None)]
    elif kind == "SUBMIT_REVIEW":
        targets = [("REVIEW_REQUESTED", user("created_by")),
                   ("QA_REVIEW_REQUESTED", user("qa_reviewer_id"))]
    elif kind == "REQUEST_REWORK":
        targets = [("REWORK", user("assignee_id")), ("TASK_STATUS", user("created_by"))]
    elif kind == "QA_REVIEW_DONE":
        targets = [("QA_DONE", user("created_by"))]
    elif kind in {"APPROVE", "ADMIN_APPROVE"}:
        targets = [("TASK_DONE", user("assignee_id"))]
    elif kind == "CANCEL":
        targets = [("TASK_CANCELLED", user("assignee_id")),
                   ("TASK_CANCELLED", user("qa_reviewer_id")),
                   ("TASK_CANCELLED", user("created_by"))]
    elif kind == "BLOCK":
        targets = [("TASK_BLOCKED", user("created_by"))]
    elif kind in {"ACCEPT", "REJECT", "START", "RESUME"}:
        targets = [("ASSIGNMENT_RESPONSE", user("created_by"))]
    elif kind == "COMMENTED":
        targets = [("TASK_COMMENT", user("created_by")),
                   ("TASK_COMMENT", user("assignee_id")),
                   ("TASK_COMMENT", user("qa_reviewer_id"))]
    seen = set()
    result = []
    for notification_kind, recipient in targets:
        if recipient is None or recipient == event.actor_user_id or recipient in seen:
            continue
        seen.add(recipient)
        result.append((notification_kind, recipient))
    return result


async def process_outbox(factory: async_sessionmaker[AsyncSession], *, limit: int = 50,
                         at: datetime | None = None) -> int:
    processed = 0
    stamp = at or now()
    for _ in range(limit):
        async with factory() as db:
            async with db.begin():
                item = await db.scalar(select(OutboxEvent).where(
                    OutboxEvent.state == "UNPROCESSED", OutboxEvent.available_at <= stamp)
                    .order_by(OutboxEvent.available_at, OutboxEvent.id)
                    .with_for_update(skip_locked=True).limit(1))
                if item is None:
                    break
                event = await db.get(TaskEvent, item.source_event_id)
                task = await db.get(Task, event.task_id) if event else None
                if event and task:
                    for kind, recipient_id in event_recipients(event, task):
                        recipient = await db.get(User, recipient_id)
                        if not recipient or recipient.status != "ACTIVE" or not await project_access(
                                db, recipient, task.project_id):
                            continue
                        reason = event.after_data.get("comment") or event.after_data.get("reason")
                        if kind == "ASSIGNED":
                            author = await db.get(User, task.created_by)
                            message = (f"Новая задача {task.task_key} от {author.name if author else 'автора'}\n"
                                       f"{task.title}\n{task.description[:2500]}\n"
                                       f"Дедлайн: {task.current_due_date}")
                        else:
                            message = f"{task.task_key}: {kind.replace('_', ' ').lower()} — {task.title}"
                        if reason and kind != "ASSIGNED":
                            message += f"\nКомментарий: {str(reason)[:1000]}"
                        await put_notification(db, recipient_id=recipient_id, kind=kind,
                                               key=f"event:{event.id}:user:{recipient_id}",
                                               body=message, scheduled_at=stamp,
                                               task_id=task.id, source_event_id=event.id)
                item.state = "PROCESSED"
                item.processed_at = stamp
                processed += 1
    return processed


async def latest_assignment_event(db: AsyncSession, task_id: UUID) -> TaskEvent | None:
    events = (await db.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id,
                                                      TaskEvent.type.in_(["CREATED", "UPDATED"]))
                               .order_by(TaskEvent.sequence.desc()))).all()
    return next((event for event in events
                 if event.before_data.get("assignee_id") != event.after_data.get("assignee_id")), None)


async def still_relevant(db: AsyncSession, notification: Notification, at: datetime) -> bool:
    recipient = await db.get(User, notification.recipient_user_id)
    if not recipient or recipient.status != "ACTIVE":
        return False
    if notification.task_id is None:
        if notification.kind == "WEEKLY_REPORT":
            if not {"SUPER_ADMIN", "TEAM_LEAD"} & await role_codes(db, recipient.id):
                return False
            week = notification.dedupe_key.split(":")[1]
            report = await db.scalar(select(WeeklyReport).where(
                WeeklyReport.recipient_user_id == recipient.id,
                WeeklyReport.week_start == datetime.fromisoformat(week).date()))
            if report is None:
                return False
            visible_ids = {str(project.id) for project in await report_projects(db, recipient)}
            return all(row["project_id"] in visible_ids for row in report.payload["projects"])
        return True
    task = await db.get(Task, notification.task_id)
    if task is None or not await project_access(db, recipient, task.project_id):
        return False
    if notification.kind in DEADLINE_KINDS:
        if (task.status not in OVERDUE_STATUSES or
                task.current_due_date != notification.due_date_snapshot or
                recipient.id not in {task.assignee_id, task.created_by}):
            return False
        boundary = datetime.combine(task.current_due_date + timedelta(days=1),
                                    datetime.min.time(), TASHKENT)
        return (at.astimezone(TASHKENT) >= boundary) if notification.kind == "OVERDUE" else \
               (at.astimezone(TASHKENT) < boundary)
    if notification.kind in REACTION_KINDS:
        assignment = await latest_assignment_event(db, task.id)
        relevant = (task.status == "OPEN" and task.assignee_id == notification.subject_user_id
                    and assignment is not None and assignment.id == notification.source_event_id)
        if notification.kind == "NO_REACTION":
            return relevant and recipient.id == task.assignee_id
        if recipient.id == task.created_by:
            return relevant
        return relevant and "TEAM_LEAD" in await role_codes(db, recipient.id)
    if notification.kind == "ASSIGNED":
        assignment = await latest_assignment_event(db, task.id)
        return (task.status == "OPEN" and task.assignee_id == recipient.id and
                assignment is not None and assignment.id == notification.source_event_id)
    if notification.kind == "ASSIGNMENT_REMOVED":
        assignment = await latest_assignment_event(db, task.id)
        return (task.status == "OPEN" and task.assignee_id != recipient.id and
                assignment is not None and assignment.id == notification.source_event_id)
    if notification.kind in {"REVIEW_REQUESTED", "QA_REVIEW_REQUESTED"}:
        if task.status != "REVIEW":
            return False
        if notification.kind == "QA_REVIEW_REQUESTED" and (
                task.qa_review_status != "PENDING" or task.qa_reviewer_id != recipient.id or
                "QA" not in await role_codes(db, recipient.id)):
            return False
        latest_review = await db.scalar(select(TaskEvent.id).where(
            TaskEvent.task_id == task.id, TaskEvent.type == "SUBMIT_REVIEW")
            .order_by(TaskEvent.sequence.desc()).limit(1))
        return latest_review == notification.source_event_id
    if notification.kind == "REWORK":
        latest_rework = await db.scalar(select(TaskEvent.id).where(
            TaskEvent.task_id == task.id, TaskEvent.type == "REQUEST_REWORK")
            .order_by(TaskEvent.sequence.desc()).limit(1))
        return (task.status == "REWORK" and task.assignee_id == recipient.id and
                latest_rework == notification.source_event_id)
    if notification.kind == "TASK_BLOCKED":
        latest_block = await db.scalar(select(TaskEvent.id).where(
            TaskEvent.task_id == task.id, TaskEvent.type == "BLOCK")
            .order_by(TaskEvent.sequence.desc()).limit(1))
        return task.status == "BLOCKED" and latest_block == notification.source_event_id
    if notification.kind == "QA_ASSIGNED":
        latest_qa_change = await db.scalar(select(TaskEvent.id).where(
            TaskEvent.task_id == task.id, TaskEvent.type == "QA_REVIEWER_CHANGED")
            .order_by(TaskEvent.sequence.desc()).limit(1))
        return (task.status not in {"DONE", "CANCELLED"} and
                task.qa_reviewer_id == recipient.id and
                "QA" in await role_codes(db, recipient.id) and
                latest_qa_change == notification.source_event_id)
    return True


async def claim_notification(factory: async_sessionmaker[AsyncSession], *, at: datetime,
                             lease_seconds: int) -> tuple[UUID, int, str, bool, InlineKeyboardMarkup | None] | None:
    async with factory() as db:
        async with db.begin():
            row = await db.scalar(select(Notification).where(
                or_(Notification.state.in_(["PENDING", "RETRY"]),
                    (Notification.state == "SENDING") & (Notification.lease_until <= at)),
                Notification.scheduled_at <= at)
                .order_by(Notification.scheduled_at, Notification.id)
                .with_for_update(skip_locked=True).limit(1))
            if row is None:
                return None
            if not await still_relevant(db, row, at):
                row.state = "SKIPPED"
                row.lease_until = None
                return row.id, 0, "", False, None
            recipient = await db.get(User, row.recipient_user_id)
            keyboard = None
            if row.kind == "ASSIGNED" and row.task_id:
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="Принять", callback_data=f"task:accept:{row.task_id}"),
                    InlineKeyboardButton(text="Отказаться", callback_data=f"task:reject:{row.task_id}"),
                ]])
            row.state = "SENDING"
            row.lease_until = at + timedelta(seconds=lease_seconds)
            return row.id, recipient.telegram_chat_id, row.body, True, keyboard


async def finish_notification(factory: async_sessionmaker[AsyncSession], notification_id: UUID,
                              *, at: datetime, outcome: str, error_code: str | None,
                              max_attempts: int, retry_base_seconds: int,
                              retry_after: int | None = None) -> None:
    async with factory() as db:
        async with db.begin():
            row = await db.get(Notification, notification_id, with_for_update=True)
            if row is None or row.state != "SENDING":
                return
            row.attempts += 1
            row.lease_until = None
            if outcome in {"SENT", "SIMULATED"}:
                row.state = outcome
                row.sent_at = at
                row.last_error_code = None
            else:
                row.last_error_code = error_code
                if outcome == "PERMANENT" or row.attempts >= max_attempts:
                    row.state = "FAILED"
                else:
                    row.state = "RETRY"
                    delay = retry_after or min(3600, retry_base_seconds * 2 ** (row.attempts - 1))
                    row.scheduled_at = at + timedelta(seconds=delay)
            db.add(NotificationAttempt(notification_id=row.id, attempted_at=at,
                                       result=row.state, error_code=error_code))


async def deliver_notifications(factory: async_sessionmaker[AsyncSession],
                                transport: MessageTransport, settings: Settings,
                                *, at: datetime | None = None) -> int:
    if not settings.notification_delivery_enabled:
        return 0
    stamp = at or now()
    processed = 0
    for _ in range(settings.worker_batch_size):
        claimed = await claim_notification(factory, at=stamp, lease_seconds=settings.worker_lease_seconds)
        if claimed is None:
            break
        notification_id, chat_id, body, should_send, keyboard = claimed
        if not should_send:
            processed += 1
            continue
        outcome, code, retry_after = "SENT", None, None
        try:
            await transport.send(chat_id, body, keyboard)
            if settings.notification_transport == "dry_run":
                outcome = "SIMULATED"
        except TelegramRetryAfter as exc:
            outcome, code, retry_after = "RETRY", "TELEGRAM_RETRY_AFTER", int(exc.retry_after)
        except (TelegramForbiddenError, TelegramBadRequest):
            outcome, code = "PERMANENT", "TELEGRAM_REJECTED"
        except Exception as exc:
            outcome, code = "RETRY", type(exc).__name__[:80]
            log.warning("Notification delivery failed: %s", code)
        await finish_notification(factory, notification_id, at=stamp, outcome=outcome,
                                  error_code=code, max_attempts=settings.worker_max_attempts,
                                  retry_base_seconds=settings.worker_retry_base_seconds,
                                  retry_after=retry_after)
        processed += 1
    return processed
