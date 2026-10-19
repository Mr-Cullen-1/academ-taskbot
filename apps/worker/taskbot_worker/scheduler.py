"""Idempotent calendar jobs backed by notification rows."""

from datetime import datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from taskbot_api.models import Notification, ProjectMember, Task, User, UserRole, WeeklyReport, now, utc
from taskbot_api.config import Settings
from taskbot_api.reporting import (
    TASHKENT, build_weekly_report, report_due, report_message, report_week,
)
from taskbot_api.task_service import OVERDUE_STATUSES
from taskbot_worker.delivery import latest_assignment_event, project_access, put_notification


def due_boundary(task: Task) -> datetime:
    return datetime.combine(task.current_due_date + timedelta(days=1), time.min,
                            TASHKENT).astimezone(timezone.utc)


def add_work_minutes(start: datetime, minutes: int, settings: Settings) -> datetime:
    days = {int(day) for day in settings.workdays.split(",")}
    local = start.astimezone(TASHKENT)
    remaining = minutes
    while True:
        if local.weekday() not in days or local.time() >= settings.workday_end:
            local = datetime.combine(local.date() + timedelta(days=1), settings.workday_start, TASHKENT)
            continue
        if local.time() < settings.workday_start:
            local = datetime.combine(local.date(), settings.workday_start, TASHKENT)
        end = datetime.combine(local.date(), settings.workday_end, TASHKENT)
        available = int((end - local).total_seconds() // 60)
        if remaining <= available:
            return (local + timedelta(minutes=remaining)).astimezone(timezone.utc)
        remaining -= available
        local = datetime.combine(local.date() + timedelta(days=1), settings.workday_start, TASHKENT)


async def schedule_deadlines(factory: async_sessionmaker[AsyncSession], *,
                             at: datetime | None = None,
                             reminder_targets: tuple[str, ...] = ("assignee", "author")) -> int:
    stamp = at or now()
    queued = 0
    async with factory() as db:
        async with db.begin():
            tasks = (await db.scalars(select(Task))).all()
            for task in tasks:
                # A prior date remains in the log, but no queued stale reminder may be sent.
                criteria = [Notification.task_id == task.id,
                            Notification.kind.in_(["DUE_24H", "DUE_2H", "OVERDUE"]),
                            Notification.state.in_(["PENDING", "RETRY", "SENDING"])]
                if task.status in OVERDUE_STATUSES:
                    criteria.append(Notification.due_date_snapshot != task.current_due_date)
                stale = (await db.scalars(select(Notification).where(*criteria))).all()
                for row in stale:
                    row.state = "SKIPPED"
                    row.lease_until = None
                if task.status not in OVERDUE_STATUSES:
                    continue
                cutoff = due_boundary(task)
                targets = []
                if task.assignee_id:
                    targets.append(("assignee", task.assignee_id))
                targets.append(("author", task.created_by))
                for kind, trigger in (("DUE_24H", cutoff - timedelta(hours=24)),
                                      ("DUE_2H", cutoff - timedelta(hours=2)),
                                      ("OVERDUE", cutoff)):
                    if kind != "OVERDUE" and (stamp >= cutoff or utc(task.created_at) > trigger):
                        # A task created inside the reminder window has no past reminder.
                        continue
                    for role, user_id in targets:
                        if kind != "OVERDUE" and role not in reminder_targets:
                            continue
                        user = await db.get(User, user_id)
                        if user is None or user.status != "ACTIVE" or not await project_access(
                                db, user, task.project_id):
                            continue
                        scheduled = max(stamp, trigger)
                        if kind == "OVERDUE" and stamp < cutoff:
                            scheduled = cutoff
                        label = {"DUE_24H": "До срока осталось 24 часа",
                                 "DUE_2H": "До срока осталось 2 часа",
                                 "OVERDUE": "Срок задачи истёк"}[kind]
                        queued += await put_notification(
                            db, recipient_id=user_id, task_id=task.id, kind=kind,
                            key=f"deadline:{kind}:{task.id}:{task.current_due_date}:{user_id}",
                            body=f"{task.task_key}: {label}. Срок: {task.current_due_date}.",
                            scheduled_at=scheduled, due_date_snapshot=task.current_due_date)
    return queued


async def schedule_reaction_reminders(factory: async_sessionmaker[AsyncSession],
                                      settings: Settings, *, at: datetime | None = None) -> int:
    stamp = at or now()
    queued = 0
    async with factory() as db:
        async with db.begin():
            tasks = (await db.scalars(select(Task).where(Task.status == "OPEN",
                                                         Task.assignee_id.is_not(None)))).all()
            for task in tasks:
                assignment = await latest_assignment_event(db, task.id)
                if assignment is None:
                    continue
                recipient = await db.get(User, task.assignee_id)
                if recipient is None or recipient.status != "ACTIVE" or not await project_access(
                        db, recipient, task.project_id):
                    continue
                trigger = add_work_minutes(utc(assignment.occurred_at), settings.reaction_work_minutes, settings)
                queued += await put_notification(
                    db, recipient_id=recipient.id, subject_user_id=recipient.id,
                    task_id=task.id, source_event_id=assignment.id, kind="NO_REACTION",
                    key=f"reaction:{assignment.id}:reminder:{recipient.id}",
                    body=f"{task.task_key}: вы ещё не приняли назначенную задачу.",
                    scheduled_at=max(stamp, trigger))
                alert_at = utc(assignment.occurred_at) + timedelta(hours=1)
                lead_ids = (await db.scalars(
                    select(User.id).join(UserRole, UserRole.user_id == User.id)
                    .where(User.status == "ACTIVE", UserRole.code == "TEAM_LEAD")
                )).all()
                for target_id in set([task.created_by, *lead_ids]):
                    target = await db.get(User, target_id)
                    if target is None or target.status != "ACTIVE" or not await project_access(
                            db, target, task.project_id):
                        continue
                    queued += await put_notification(
                        db, recipient_id=target_id, subject_user_id=recipient.id,
                        task_id=task.id, source_event_id=assignment.id, kind="NO_RESPONSE_ALERT",
                        key=f"reaction:{assignment.id}:alert:{target_id}",
                        body=f"{task.task_key}: исполнитель не ответил на назначение в течение часа.",
                        scheduled_at=max(stamp, alert_at))
    return queued


async def schedule_weekly_reports(factory: async_sessionmaker[AsyncSession], *,
                                  at: datetime | None = None) -> int:
    stamp = at or now()
    if not report_due(stamp):
        return 0
    week_start, _, _ = report_week(stamp)
    queued = 0
    async with factory() as db:
        async with db.begin():
            recipients = (await db.scalars(select(User).join(UserRole, UserRole.user_id == User.id)
                                           .where(User.status == "ACTIVE",
                                                  UserRole.code.in_(["SUPER_ADMIN", "TEAM_LEAD"]))
                                           .distinct())).all()
            for recipient in recipients:
                saved = await db.scalar(select(WeeklyReport).where(
                    WeeklyReport.recipient_user_id == recipient.id,
                    WeeklyReport.week_start == week_start))
                if saved is not None:
                    continue
                payload = await build_weekly_report(db, recipient, at=stamp)
                try:
                    async with db.begin_nested():
                        db.add(WeeklyReport(recipient_user_id=recipient.id,
                                            week_start=week_start, generated_at=stamp,
                                            payload=payload))
                        await db.flush()
                except IntegrityError:
                    continue
                queued += await put_notification(
                    db, recipient_id=recipient.id, kind="WEEKLY_REPORT",
                    key=f"weekly:{week_start}:{recipient.id}",
                    body=report_message(payload), scheduled_at=stamp)
    return queued
