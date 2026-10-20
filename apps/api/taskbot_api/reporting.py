"""Weekly project metrics. Stored runs keep API and Telegram views identical."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from taskbot_api.models import DeadlineRequest, Project, ProjectMember, Task, TaskEvent, User, WeeklyReport, now, utc
from taskbot_api.security import role_codes
from taskbot_api.task_service import is_overdue


TASHKENT = ZoneInfo("Asia/Tashkent")


def report_week(at: datetime) -> tuple[date, datetime, datetime]:
    local = at.astimezone(TASHKENT)
    this_monday = local.date() - timedelta(days=local.weekday())
    last_monday = this_monday - timedelta(days=7)
    begin = datetime.combine(last_monday, time.min, TASHKENT).astimezone(timezone.utc)
    end = datetime.combine(this_monday, time.min, TASHKENT).astimezone(timezone.utc)
    return last_monday, begin, end


def report_due(at: datetime) -> bool:
    local = at.astimezone(TASHKENT)
    monday = local.date() - timedelta(days=local.weekday())
    return local >= datetime.combine(monday, time(9), TASHKENT)


def empty_metrics() -> dict:
    return {"created": 0, "completed": 0, "accepted_by_author": 0,
            "admin_approved": 0, "current_overdue": 0,
            "awaiting_review_or_acceptance": 0, "awaiting_assignee_response": 0,
            "pending_deadline_requests": 0,
            "on_time_current": 0, "on_time_original": 0}


def on_time(completed: datetime, due: date) -> bool:
    cutoff = datetime.combine(due + timedelta(days=1), time.min, TASHKENT)
    return completed.astimezone(TASHKENT) < cutoff


async def report_projects(db: AsyncSession, recipient: User) -> list[Project]:
    if recipient.status != "ACTIVE":
        raise HTTPException(403, "FORBIDDEN")
    roles = await role_codes(db, recipient.id)
    if {"SUPER_ADMIN", "TEAM_LEAD"} & roles:
        statement = select(Project).order_by(Project.name, Project.id)
    elif "PM" in roles:
        statement = (select(Project).join(ProjectMember,
                                          ProjectMember.project_id == Project.id)
                     .where(ProjectMember.user_id == recipient.id)
                     .order_by(Project.name, Project.id))
    else:
        raise HTTPException(403, "FORBIDDEN")
    return (await db.scalars(statement)).all()


async def build_weekly_report(db: AsyncSession, recipient: User, *, at: datetime | None = None) -> dict:
    instant = at or now()
    week_start, begin, end = report_week(instant)
    projects = await report_projects(db, recipient)
    project_ids = [row.id for row in projects]
    tasks = (await db.scalars(select(Task).where(Task.project_id.in_(project_ids)))).all() if project_ids else []
    task_by_id = {task.id: task for task in tasks}
    pending = (await db.scalars(select(DeadlineRequest).where(
        DeadlineRequest.task_id.in_(task_by_id),
        DeadlineRequest.status == "PENDING"))).all() if tasks else []
    events = (await db.scalars(select(TaskEvent).where(
        TaskEvent.task_id.in_([t.id for t in tasks]),
        TaskEvent.type.in_(["APPROVE", "ADMIN_APPROVE"])
    ))).all() if tasks else []
    approvals = {event.task_id: event for event in events if begin <= utc(event.occurred_at) < end}
    by_project = {project.id: {"project_id": str(project.id), "name": project.name,
                               **empty_metrics()} for project in projects}
    for pending_request in pending:
        pending_task = task_by_id[pending_request.task_id]
        if pending_task.status not in {"DONE", "CANCELLED"}:
            by_project[pending_task.project_id]["pending_deadline_requests"] += 1
    for task in tasks:
        metrics = by_project[task.project_id]
        if begin <= utc(task.created_at) < end:
            metrics["created"] += 1
        if task.status == "REVIEW":
            metrics["awaiting_review_or_acceptance"] += 1
        if task.status == "OPEN" and task.assignee_id:
            metrics["awaiting_assignee_response"] += 1
        if is_overdue(task, today=instant.astimezone(TASHKENT).date()):
            metrics["current_overdue"] += 1
        if task.completed_at and begin <= utc(task.completed_at) < end:
            metrics["completed"] += 1
            approval = approvals.get(task.id)
            if approval:
                if approval.approval_mode == "AUTHOR":
                    metrics["accepted_by_author"] += 1
                elif approval.approval_mode == "ADMIN_OVERRIDE":
                    metrics["admin_approved"] += 1
                # The event captures the dates that were valid at acceptance.
                current_due = approval.after_data.get("current_due_date")
                original_due = approval.after_data.get("original_due_date")
                if current_due and on_time(utc(task.completed_at), date.fromisoformat(current_due)):
                    metrics["on_time_current"] += 1
                if original_due and on_time(utc(task.completed_at), date.fromisoformat(original_due)):
                    metrics["on_time_original"] += 1
    totals = empty_metrics()
    for metrics in by_project.values():
        for name in totals:
            totals[name] += metrics[name]
    return {"week_start": week_start.isoformat(), "week_end_exclusive": end.astimezone(TASHKENT).date().isoformat(),
            "generated_at": instant.isoformat(), "timezone": "Asia/Tashkent",
            "totals": totals, "projects": list(by_project.values()),
            "deadline_requests_supported": True}


def report_message(payload: dict) -> str:
    totals = payload["totals"]
    lines = [f"Отчёт Taskbot за {payload['week_start']} — {payload['week_end_exclusive']} (правая граница исключена)",
             f"Создано: {totals['created']}; завершено: {totals['completed']}; принято автором: {totals['accepted_by_author']}; исключительная приёмка: {totals['admin_approved']}.",
             f"Просрочено сейчас: {totals['current_overdue']}; ожидают проверки/приёмки: {totals['awaiting_review_or_acceptance']}.",
             f"В срок: {totals['on_time_current']} по текущей дате, {totals['on_time_original']} по исходной из {totals['completed']} завершённых."]
    for project in payload["projects"]:
        lines.append(f"{project['name']}: создано {project['created']}, завершено {project['completed']}, просрочено {project['current_overdue']}, на проверке {project['awaiting_review_or_acceptance']}.")
    body = "\n".join(lines)
    if len(body) > 4000:
        body = body[:3900] + "\nПолная разбивка доступна через API."
    return body


async def visible_saved_report(db: AsyncSession, recipient: User, week_start: date) -> WeeklyReport | None:
    await report_projects(db, recipient)
    return await db.scalar(select(WeeklyReport).where(WeeklyReport.recipient_user_id == recipient.id,
                                                    WeeklyReport.week_start == week_start))
