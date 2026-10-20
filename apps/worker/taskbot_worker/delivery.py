"""Durable outbox fan-out and opt-in Telegram delivery."""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from taskbot_api.config import Settings
from taskbot_api.models import (
    DeadlineRequest, Notification, NotificationAttempt, OutboxEvent, PersonalReminder, ProjectMember, Task,
    TaskEvent, User, WeeklyReport, now, utc,
)
from taskbot_api.reporting import TASHKENT, report_projects
from taskbot_api.registration import requested_access
from taskbot_api.security import role_codes
from taskbot_api.task_service import OVERDUE_STATUSES


log = logging.getLogger(__name__)
DEADLINE_KINDS = {"DUE_24H", "DUE_2H", "OVERDUE"}
REACTION_KINDS = {"NO_REACTION", "NO_RESPONSE_ALERT"}
EVENT_TYPES = {"CREATED", "UPDATED", "QA_REVIEWER_CHANGED", "SUBMIT_REVIEW",
               "REQUEST_REWORK", "QA_REVIEW_DONE", "APPROVE", "ADMIN_APPROVE",
               "CANCEL", "BLOCK", "ACCEPT", "REJECT", "START", "RESUME", "COMMENTED",
               "DEPENDENCY_ADDED", "DEADLINE_REQUESTED", "DEADLINE_APPROVED",
               "DEADLINE_REJECTED", "SPRINT_CHANGED"}


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
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        insert = pg_insert
    elif dialect == "sqlite":
        insert = sqlite_insert
    else:
        raise RuntimeError("Unsupported notification database dialect")
    statement = insert(Notification).values(
        id=uuid4(), recipient_user_id=recipient_id, subject_user_id=subject_user_id,
        task_id=task_id, source_event_id=source_event_id, kind=kind,
        dedupe_key=key, body=body[:4000], scheduled_at=scheduled_at,
        due_date_snapshot=due_date_snapshot, state="PENDING", attempts=0,
        created_at=now()).on_conflict_do_nothing(index_elements=[Notification.dedupe_key])
    result = await db.execute(statement)
    return result.rowcount == 1


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
        targets = [("TASK_DONE", user("assignee_id")),
                   ("TASK_DONE", user("created_by"))]
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
                   # Legacy comment events have no participant snapshot. Do
                   # not infer recipients from a later assignment or QA change.
                   ("TASK_COMMENT", user("assignee_id") if "assignee_id" in after else None),
                   ("TASK_COMMENT", user("qa_reviewer_id") if "qa_reviewer_id" in after else None)]
    elif kind == "DEADLINE_REQUESTED":
        targets = [("DEADLINE_REQUEST", user("created_by"))]
    elif kind in {"DEADLINE_APPROVED", "DEADLINE_REJECTED"}:
        targets = [("DEADLINE_DECISION", user("assignee_id"))]
    seen = set()
    result = []
    for notification_kind, recipient in targets:
        if recipient is None or recipient == event.actor_user_id or recipient in seen:
            continue
        seen.add(recipient)
        result.append((notification_kind, recipient))
    return result


async def event_message(db: AsyncSession, task: Task, event: TaskEvent, kind: str) -> str:
    if kind == "DEADLINE_REQUEST":
        return (f"Запрос продления {task.task_key}: {task.title}\n"
                f"Предложенный срок: {event.after_data.get('proposed_due_date')}\n"
                f"Причина: {str(event.after_data.get('reason', ''))[:1000]}\n"
                f"Прогресс: {str(event.after_data.get('progress_note', ''))[:1000]}")
    if kind == "DEADLINE_DECISION":
        decision = "одобрен" if event.type == "DEADLINE_APPROVED" else "отклонён"
        return (f"Запрос продления {task.task_key} {decision}.\n"
                f"Текущий срок: {task.current_due_date}" +
                (f"\nПричина: {str(event.after_data['decision_reason'])[:1000]}"
                 if event.after_data.get("decision_reason") else ""))
    if kind == "ASSIGNED":
        author = await db.get(User, task.created_by)
        return (f"Новая задача {task.task_key} от {author.name if author else 'автора'}\n"
                f"{task.title}\n{task.description[:2500]}\n"
                f"Дедлайн: {task.current_due_date}")
    message = f"{task.task_key}: {kind.replace('_', ' ').lower()} — {task.title}"
    reason = event.after_data.get("comment") or event.after_data.get("reason")
    if reason:
        message += f"\nКомментарий: {str(reason)[:1000]}"
    return message


async def record_outbox_failure(factory: async_sessionmaker[AsyncSession], item_id: UUID,
                                *, at: datetime, error: Exception, max_attempts: int,
                                retry_base_seconds: int) -> None:
    """Record a per-event failure after the fan-out transaction has rolled back."""
    retryable = not isinstance(error, (IntegrityError, ValueError, TypeError, AttributeError))
    code = type(error).__name__[:80]
    async with factory() as db:
        async with db.begin():
            item = await db.get(OutboxEvent, item_id, with_for_update=True)
            if item is None or item.state != "UNPROCESSED":
                return
            item.attempts += 1
            item.last_error_code = code
            if not retryable or item.attempts >= max_attempts:
                item.state = "FAILED"
            else:
                delay = min(3600, retry_base_seconds * 2 ** (item.attempts - 1))
                item.available_at = at + timedelta(seconds=delay)
            log.warning("Outbox event id=%s state=%s error=%s attempts=%s",
                        item.id, item.state, code, item.attempts)


async def process_outbox(factory: async_sessionmaker[AsyncSession], *, limit: int = 50,
                         at: datetime | None = None, max_attempts: int = 5,
                         retry_base_seconds: int = 30) -> int:
    processed = 0
    stamp = at or now()
    for _ in range(limit):
        item_id = None
        succeeded = False
        try:
            async with factory() as db:
                async with db.begin():
                    item = await db.scalar(select(OutboxEvent).where(
                        OutboxEvent.state == "UNPROCESSED", OutboxEvent.available_at <= stamp)
                        .order_by(OutboxEvent.available_at, OutboxEvent.id)
                        .with_for_update(skip_locked=True).limit(1))
                    if item is None:
                        break
                    item_id = item.id
                    event = await db.get(TaskEvent, item.source_event_id)
                    task = await db.get(Task, event.task_id) if event else None
                    if (event is None or task is None or item.aggregate_type != "task" or
                            item.aggregate_id != str(task.id) or item.kind != "TASK_EVENT" or
                            not isinstance(item.payload, dict) or
                            item.payload.get("task_id") != str(task.id) or
                            item.payload.get("event_type") != event.type or
                            event.type not in EVENT_TYPES or
                            not isinstance(event.before_data, dict) or
                            not isinstance(event.after_data, dict)):
                        item.state = "FAILED"
                        item.attempts += 1
                        item.last_error_code = "INVALID_EVENT"
                        log.warning("Invalid outbox event id=%s", item.id)
                        continue
                    try:
                        targets = event_recipients(event, task)
                    except (TypeError, ValueError, AttributeError):
                        item.state = "FAILED"
                        item.attempts += 1
                        item.last_error_code = "INVALID_RECIPIENT_SNAPSHOT"
                        log.warning("Invalid event recipient snapshot outbox_id=%s", item.id)
                        continue
                    for kind, recipient_id in targets:
                        recipient = await db.get(User, recipient_id)
                        if not recipient or recipient.status != "ACTIVE" or not await project_access(
                                db, recipient, task.project_id):
                            continue
                        message = await event_message(db, task, event, kind)
                        await put_notification(db, recipient_id=recipient_id, kind=kind,
                                               key=f"event:{event.id}:user:{recipient_id}",
                                               body=message, scheduled_at=stamp,
                                               task_id=task.id, source_event_id=event.id)
                    item.state = "PROCESSED"
                    item.processed_at = stamp
                    succeeded = True
        except Exception as exc:
            if item_id is None:
                raise
            await record_outbox_failure(factory, item_id, at=stamp, error=exc,
                                        max_attempts=max_attempts,
                                        retry_base_seconds=retry_base_seconds)
        else:
            if succeeded:
                processed += 1
    return processed


async def latest_assignment_event(db: AsyncSession, task_id: UUID) -> TaskEvent | None:
    events = (await db.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id,
                                                      TaskEvent.type.in_(["CREATED", "UPDATED"]))
                               .order_by(TaskEvent.sequence.desc()))).all()
    return next((event for event in events
                 if event.before_data.get("assignee_id") != event.after_data.get("assignee_id")), None)


async def latest_event_id(db: AsyncSession, task_id: UUID, types: set[str]) -> UUID | None:
    return await db.scalar(select(TaskEvent.id).where(TaskEvent.task_id == task_id,
                                                      TaskEvent.type.in_(types))
                           .order_by(TaskEvent.sequence.desc()).limit(1))


async def still_relevant(db: AsyncSession, notification: Notification, at: datetime) -> bool:
    recipient = await db.get(User, notification.recipient_user_id)
    if not recipient:
        return False
    if notification.kind == "REGISTRATION_REJECTED":
        return (notification.task_id is None and notification.subject_user_id == recipient.id
                and recipient.status == "REJECTED")
    if recipient.status != "ACTIVE":
        return False
    if notification.kind == "REGISTRATION_APPROVED":
        return (notification.task_id is None and notification.subject_user_id == recipient.id
                and bool(await role_codes(db, recipient.id)))
    if notification.kind == "REGISTRATION_REQUEST":
        applicant = await db.get(User, notification.subject_user_id)
        return (notification.task_id is None and applicant is not None and
                applicant.status == "PENDING" and
                "SUPER_ADMIN" in await role_codes(db, recipient.id) and
                await requested_access(db, applicant.id) is not None)
    if notification.kind == "PERSONAL_REMINDER":
        try:
            prefix, reminder_id, version = notification.dedupe_key.split(":")
            if prefix != "personal_reminder":
                return False
            reminder = await db.get(PersonalReminder, UUID(reminder_id))
            if (reminder is None or reminder.owner_user_id != recipient.id or
                    reminder.cancelled_at is not None or reminder.version != int(version) or
                    reminder.task_id != notification.task_id or
                    utc(reminder.remind_at) > at):
                return False
            if reminder.task_id is None:
                return True
            task = await db.get(Task, reminder.task_id)
            return task is not None and await project_access(db, recipient, task.project_id)
        except (ValueError, TypeError):
            return False
    if notification.task_id is None:
        if notification.kind == "WEEKLY_REPORT":
            if not {"SUPER_ADMIN", "TEAM_LEAD", "PM"} & await role_codes(db, recipient.id):
                return False
            try:
                week = datetime.fromisoformat(notification.dedupe_key.split(":")[1]).date()
            except (IndexError, ValueError):
                return False
            report = await db.scalar(select(WeeklyReport).where(
                WeeklyReport.recipient_user_id == recipient.id,
                WeeklyReport.week_start == week))
            if report is None or not isinstance(report.payload, dict) or not isinstance(
                    report.payload.get("projects"), list):
                return False
            visible_ids = {str(project.id) for project in await report_projects(db, recipient)}
            return all(isinstance(row, dict) and row.get("project_id") in visible_ids
                       for row in report.payload["projects"])
        return False
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
        if notification.kind == "REVIEW_REQUESTED" and recipient.id != task.created_by:
            return False
        if notification.kind == "QA_REVIEW_REQUESTED" and (
                task.qa_review_status != "PENDING" or task.qa_reviewer_id != recipient.id or
                "QA" not in await role_codes(db, recipient.id)):
            return False
        latest_review = await latest_event_id(db, task.id, {"SUBMIT_REVIEW"})
        return latest_review == notification.source_event_id
    if notification.kind == "REWORK":
        latest_rework = await latest_event_id(db, task.id, {"REQUEST_REWORK"})
        return (task.status == "REWORK" and task.assignee_id == recipient.id and
                latest_rework == notification.source_event_id)
    if notification.kind == "TASK_BLOCKED":
        latest_block = await latest_event_id(db, task.id, {"BLOCK"})
        return (task.status == "BLOCKED" and recipient.id == task.created_by and
                latest_block == notification.source_event_id)
    if notification.kind == "QA_ASSIGNED":
        latest_qa_change = await latest_event_id(db, task.id, {"QA_REVIEWER_CHANGED"})
        return (task.status not in {"DONE", "CANCELLED"} and
                task.qa_reviewer_id == recipient.id and
                "QA" in await role_codes(db, recipient.id) and
                latest_qa_change == notification.source_event_id)
    event = await db.get(TaskEvent, notification.source_event_id) if notification.source_event_id else None
    if event is None or event.task_id != task.id:
        return False
    if notification.kind == "TASK_UPDATED":
        return (event.type == "UPDATED" and task.status not in {"DONE", "CANCELLED"} and
                recipient.id in {task.created_by, task.assignee_id} and
                await latest_event_id(db, task.id, {"UPDATED"}) == event.id)
    if notification.kind == "TASK_STATUS":
        return (event.type == "REQUEST_REWORK" and task.status == "REWORK" and
                recipient.id == task.created_by and
                await latest_event_id(db, task.id, {"REQUEST_REWORK"}) == event.id)
    if notification.kind == "QA_DONE":
        return (event.type == "QA_REVIEW_DONE" and task.status == "REVIEW" and
                task.qa_review_status == "DONE" and recipient.id == task.created_by and
                await latest_event_id(db, task.id, {"QA_REVIEW_DONE"}) == event.id)
    if notification.kind == "TASK_DONE":
        return (event.type in {"APPROVE", "ADMIN_APPROVE"} and task.status == "DONE" and
                recipient.id in {task.assignee_id, task.created_by} and
                await latest_event_id(db, task.id, {"APPROVE", "ADMIN_APPROVE"}) == event.id)
    if notification.kind == "TASK_CANCELLED":
        return (event.type == "CANCEL" and task.status == "CANCELLED" and
                recipient.id in {task.assignee_id, task.qa_reviewer_id, task.created_by} and
                await latest_event_id(db, task.id, {"CANCEL"}) == event.id)
    if notification.kind == "ASSIGNMENT_RESPONSE":
        return (event.type in {"ACCEPT", "REJECT", "START", "RESUME"} and
                isinstance(event.after_data, dict) and recipient.id == task.created_by and
                task.status == event.after_data.get("status") and
                await latest_event_id(db, task.id, {"ACCEPT", "REJECT", "START", "RESUME"}) == event.id)
    if notification.kind == "QA_UNASSIGNED":
        return (event.type == "QA_REVIEWER_CHANGED" and task.status not in {"DONE", "CANCELLED"} and
                event.before_data.get("qa_reviewer_id") == str(recipient.id) and
                task.qa_reviewer_id != recipient.id and
                await latest_event_id(db, task.id, {"QA_REVIEWER_CHANGED"}) == event.id)
    if notification.kind == "TASK_COMMENT":
        if (event.type != "COMMENTED" or task.status in {"DONE", "CANCELLED"} or
                recipient.id not in {task.created_by, task.assignee_id, task.qa_reviewer_id}):
            return False
        if not isinstance(event.after_data, dict):
            return False
        participants_at_comment = {
            event.after_data.get("created_by", str(task.created_by)),
            event.after_data.get("assignee_id"),
            event.after_data.get("qa_reviewer_id"),
        }
        if str(recipient.id) not in participants_at_comment:
            return False
        return (recipient.id != task.qa_reviewer_id or recipient.id == task.created_by or
                "QA" in await role_codes(db, recipient.id))
    if notification.kind in {"DEADLINE_REQUEST", "DEADLINE_DECISION"}:
        if not isinstance(event.after_data, dict):
            return False
        try:
            request_id = UUID(event.after_data["deadline_request_id"])
        except (KeyError, ValueError, TypeError):
            return False
        deadline_request = await db.get(DeadlineRequest, request_id)
        if deadline_request is None or deadline_request.task_id != task.id:
            return False
        if notification.kind == "DEADLINE_REQUEST":
            return (event.type == "DEADLINE_REQUESTED" and
                    deadline_request.status == "PENDING" and task.status not in {"DONE", "CANCELLED", "REJECTED"} and
                    task.assignee_id == deadline_request.requested_by and
                    recipient.id == task.created_by)
        return (event.type in {"DEADLINE_APPROVED", "DEADLINE_REJECTED"} and
                deadline_request.status == event.type.removeprefix("DEADLINE_") and
                recipient.id == deadline_request.requested_by)
    return False


async def claim_notification(factory: async_sessionmaker[AsyncSession], *, at: datetime,
                             lease_seconds: int, max_attempts: int | None = None,
                             ) -> tuple[UUID, int, str, bool, InlineKeyboardMarkup | None, int] | None:
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
            if row.state == "SENDING":
                # An expired claim is an attempted delivery with an unknown
                # outcome. Its owner can no longer finalize a newer claim.
                row.attempts = max(row.attempts, 1)
                exhausted = max_attempts is not None and row.attempts >= max_attempts
                db.add(NotificationAttempt(notification_id=row.id, attempted_at=at,
                                           result="FAILED" if exhausted else "RETRY",
                                           error_code="LEASE_EXPIRED"))
                if exhausted:
                    row.state = "FAILED"
                    row.lease_until = None
                    row.last_error_code = "LEASE_EXPIRED"
                    return row.id, 0, "", False, None, 0
            if max_attempts is not None and row.attempts >= max_attempts:
                row.state = "FAILED"
                row.lease_until = None
                row.last_error_code = "MAX_ATTEMPTS"
                return row.id, 0, "", False, None, 0
            if not await still_relevant(db, row, at):
                row.state = "SKIPPED"
                row.lease_until = None
                return row.id, 0, "", False, None, 0
            recipient = await db.get(User, row.recipient_user_id)
            keyboard = None
            if row.kind == "ASSIGNED" and row.task_id:
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="Принять", callback_data=f"task:accept:{row.task_id}"),
                    InlineKeyboardButton(text="Отказаться", callback_data=f"task:reject:{row.task_id}"),
                ]])
            elif row.kind == "REGISTRATION_REQUEST" and row.subject_user_id:
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="Одобрить", callback_data=f"reg:approve:{row.subject_user_id}"),
                    InlineKeyboardButton(text="Отклонить", callback_data=f"reg:reject:{row.subject_user_id}"),
                ]])
            row.state = "SENDING"
            row.attempts += 1
            row.lease_until = at + timedelta(seconds=lease_seconds)
            return row.id, recipient.telegram_chat_id, row.body, True, keyboard, row.attempts


async def revalidate_claim(factory: async_sessionmaker[AsyncSession], notification_id: UUID,
                           claim_attempt: int, *, at: datetime) -> tuple[int, str] | None:
    """Refresh authorization and task text just before calling the transport."""
    async with factory() as db:
        async with db.begin():
            row = await db.get(Notification, notification_id, with_for_update=True)
            if (row is None or row.state != "SENDING" or row.attempts != claim_attempt or
                    row.lease_until is None or utc(row.lease_until) <= at):
                return None
            if not await still_relevant(db, row, at):
                row.state = "SKIPPED"
                row.lease_until = None
                return None
            if (row.task_id and row.source_event_id and
                    row.kind not in REACTION_KINDS):
                task = await db.get(Task, row.task_id)
                event = await db.get(TaskEvent, row.source_event_id)
                row.body = (await event_message(db, task, event, row.kind))[:4000]
            recipient = await db.get(User, row.recipient_user_id)
            return recipient.telegram_chat_id, row.body


async def finish_notification(factory: async_sessionmaker[AsyncSession], notification_id: UUID,
                              *, at: datetime, outcome: str, error_code: str | None,
                              claim_attempt: int, max_attempts: int, retry_base_seconds: int,
                              retry_after: int | None = None) -> None:
    async with factory() as db:
        async with db.begin():
            row = await db.get(Notification, notification_id, with_for_update=True)
            if row is None or row.state != "SENDING" or row.attempts != claim_attempt:
                return
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
                    delay = (max(1, retry_after) if retry_after is not None else
                             min(3600, retry_base_seconds * 2 ** (row.attempts - 1)))
                    row.scheduled_at = at + timedelta(seconds=delay)
            db.add(NotificationAttempt(notification_id=row.id, attempted_at=at,
                                       result=row.state, error_code=error_code))


async def deliver_notifications(factory: async_sessionmaker[AsyncSession],
                                transport: MessageTransport, settings: Settings,
                                *, at: datetime | None = None) -> int:
    if not settings.notification_delivery_enabled:
        return 0
    processed = 0
    for _ in range(settings.worker_batch_size):
        stamp = at if at is not None else now()
        claimed = await claim_notification(factory, at=stamp,
                                           lease_seconds=settings.worker_lease_seconds,
                                           max_attempts=settings.worker_max_attempts)
        if claimed is None:
            break
        notification_id, chat_id, body, should_send, keyboard, claim_attempt = claimed
        if not should_send:
            processed += 1
            continue
        ready = await revalidate_claim(factory, notification_id, claim_attempt,
                                       at=at if at is not None else now())
        if ready is None:
            processed += 1
            continue
        chat_id, body = ready
        outcome, code, retry_after = "SENT", None, None
        try:
            await asyncio.wait_for(transport.send(chat_id, body, keyboard),
                                   timeout=max(0.1, settings.worker_lease_seconds / 2))
            if settings.notification_transport == "dry_run":
                outcome = "SIMULATED"
        except TelegramRetryAfter as exc:
            outcome, code, retry_after = "RETRY", "TELEGRAM_RETRY_AFTER", int(exc.retry_after)
        except (TelegramForbiddenError, TelegramBadRequest):
            outcome, code = "PERMANENT", "TELEGRAM_REJECTED"
        except Exception as exc:
            outcome, code = "RETRY", type(exc).__name__[:80]
            log.warning("Notification delivery failed: %s", code)
        await finish_notification(factory, notification_id,
                                  at=at if at is not None else now(), outcome=outcome,
                                  error_code=code, max_attempts=settings.worker_max_attempts,
                                  claim_attempt=claim_attempt,
                                  retry_base_seconds=settings.worker_retry_base_seconds,
                                  retry_after=retry_after)
        processed += 1
    return processed
