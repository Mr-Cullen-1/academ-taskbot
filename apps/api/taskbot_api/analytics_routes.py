"""MVP task and assignee aggregates with project-scoped access."""

from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from taskbot_api.models import DeadlineRequest, Project, ProjectMember, Task, TaskEvent, User, utc
from taskbot_api.reporting import TASHKENT, empty_metrics, on_time, report_week
from taskbot_api.security import CurrentUser, Db, role_codes
from taskbot_api.task_service import is_overdue

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])


async def allowed_projects(db: Db, actor: CurrentUser, project_id: UUID | None) -> list[Project]:
    roles = await role_codes(db, actor.id)
    if "SUPER_ADMIN" in roles:
        stmt = select(Project)
    elif "PM" in roles:
        stmt = select(Project).join(ProjectMember, ProjectMember.project_id == Project.id).where(
            ProjectMember.user_id == actor.id)
    else:
        raise HTTPException(403, "FORBIDDEN")
    if project_id:
        stmt = stmt.where(Project.id == project_id)
    rows = (await db.scalars(stmt.order_by(Project.name, Project.id))).all()
    if project_id and not rows:
        raise HTTPException(404, "NOT_FOUND")
    return rows


def interval(start_on: date | None, end_on: date | None) -> tuple[date, date, datetime, datetime]:
    previous_monday, _, end = report_week(datetime.now(timezone.utc))
    start = start_on or previous_monday
    stop = end_on or end.astimezone(TASHKENT).date()
    if stop <= start or (stop - start).days > 366:
        raise HTTPException(422, "INVALID_REPORT_PERIOD")
    begin_at = datetime.combine(start, time.min, TASHKENT).astimezone(timezone.utc)
    end_at = datetime.combine(stop, time.min, TASHKENT).astimezone(timezone.utc)
    return start, stop, begin_at, end_at


async def aggregates(db: Db, projects: list[Project], *, begin: datetime, end: datetime,
                     assignee_id: UUID | None = None, status: str | None = None) -> dict:
    project_ids = [project.id for project in projects]
    stmt = select(Task).where(Task.project_id.in_(project_ids))
    if assignee_id:
        stmt = stmt.where(Task.assignee_id == assignee_id)
    if status:
        stmt = stmt.where(Task.status == status)
    tasks = (await db.scalars(stmt)).all() if project_ids else []
    ids = [task.id for task in tasks]
    events = (await db.scalars(select(TaskEvent).where(
        TaskEvent.task_id.in_(ids), TaskEvent.type.in_(["APPROVE", "ADMIN_APPROVE"]),
        TaskEvent.occurred_at >= begin, TaskEvent.occurred_at < end))).all() if ids else []
    approvals = {event.task_id: event for event in events}
    pending = (await db.scalars(select(DeadlineRequest).where(
        DeadlineRequest.task_id.in_(ids), DeadlineRequest.status == "PENDING"))).all() if ids else []
    pending_by_task = {item.task_id for item in pending}
    result: dict[UUID, dict] = {}
    today = datetime.now(TASHKENT).date()
    for task in tasks:
        group = result.setdefault(task.project_id, empty_metrics())
        if begin <= utc(task.created_at) < end:
            group["created"] += 1
        if task.status == "REVIEW":
            group["awaiting_review_or_acceptance"] += 1
        if task.status == "OPEN" and task.assignee_id:
            group["awaiting_assignee_response"] += 1
        if is_overdue(task, today=today):
            group["current_overdue"] += 1
        if task.id in pending_by_task and task.status not in {"DONE", "CANCELLED"}:
            group["pending_deadline_requests"] += 1
        if task.completed_at and begin <= utc(task.completed_at) < end:
            group["completed"] += 1
            event = approvals.get(task.id)
            if event:
                if event.approval_mode == "AUTHOR":
                    group["accepted_by_author"] += 1
                if event.approval_mode == "ADMIN_OVERRIDE":
                    group["admin_approved"] += 1
                current_due = event.after_data.get("current_due_date")
                original_due = event.after_data.get("original_due_date")
                if current_due and on_time(utc(task.completed_at), date.fromisoformat(current_due)):
                    group["on_time_current"] += 1
                if original_due and on_time(utc(task.completed_at), date.fromisoformat(original_due)):
                    group["on_time_original"] += 1
    totals = empty_metrics()
    for metrics in result.values():
        for field in totals:
            totals[field] += metrics[field]
    return {"totals": totals, "by_project": [
        {"project_id": str(project.id), "name": project.name,
         **result.get(project.id, empty_metrics())} for project in projects]}


@router.get("/tasks")
async def task_report(db: Db, actor: CurrentUser, project_id: UUID | None = None,
                      assignee_id: UUID | None = None, status: str | None = None,
                      start_on: date | None = None, end_on: date | None = None) -> dict:
    projects = await allowed_projects(db, actor, project_id)
    start, stop, begin, end = interval(start_on, end_on)
    current = await aggregates(db, projects, begin=begin, end=end,
                               assignee_id=assignee_id, status=status)
    previous = await aggregates(db, projects, begin=begin - (end - begin), end=begin,
                                assignee_id=assignee_id, status=status)
    return {"start_on": start.isoformat(), "end_on_exclusive": stop.isoformat(),
            "timezone": "Asia/Tashkent", **current,
            "previous_period": {"created": previous["totals"]["created"],
                                "completed": previous["totals"]["completed"]}}


@router.get("/users")
async def user_report(db: Db, actor: CurrentUser, project_id: UUID | None = None,
                      start_on: date | None = None, end_on: date | None = None) -> dict:
    projects = await allowed_projects(db, actor, project_id)
    start, stop, begin, end = interval(start_on, end_on)
    members = (await db.scalars(select(User).join(ProjectMember, ProjectMember.user_id == User.id)
                                .where(ProjectMember.project_id.in_([p.id for p in projects]))
                                .distinct().order_by(User.name, User.id))).all() if projects else []
    rows = []
    for member in members:
        data = await aggregates(db, projects, begin=begin, end=end, assignee_id=member.id)
        rows.append({"user_id": str(member.id), "name": member.name,
                     "metrics": data["totals"]})
    return {"start_on": start.isoformat(), "end_on_exclusive": stop.isoformat(),
            "timezone": "Asia/Tashkent", "users": rows}
