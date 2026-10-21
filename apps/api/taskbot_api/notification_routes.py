"""Private delivery log and Super Admin/Team Lead weekly report API."""

from datetime import date

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import and_, or_, select

from taskbot_api.models import Notification, Task, now, utc
from taskbot_api.reporting import report_projects, report_week, visible_saved_report
from taskbot_api.routes import can_view_project, mutation
from taskbot_api.security import CurrentUser, Db


router = APIRouter(prefix="/api/v1", tags=["notifications", "reports"])


class EmptyBody(BaseModel):
    pass


def notification_json(row: Notification) -> dict:
    return {"id": str(row.id), "task_id": str(row.task_id) if row.task_id else None,
            "kind": row.kind, "body": row.body, "state": row.state,
            "scheduled_at": utc(row.scheduled_at).isoformat(),
            "sent_at": utc(row.sent_at).isoformat() if row.sent_at else None,
            "read_at": utc(row.read_at).isoformat() if row.read_at else None}


async def can_view_notification(db: Db, actor: CurrentUser, row: Notification) -> bool:
    if row.task_id is None:
        return True
    task = await db.get(Task, row.task_id)
    return task is not None and await can_view_project(db, actor, task.project_id)


@router.get("/notifications")
async def my_notifications(db: Db, actor: CurrentUser, limit: int = 50) -> list[dict]:
    if not 1 <= limit <= 100:
        raise HTTPException(422, "INVALID_PAGE_LIMIT")
    visible = []
    cursor = None
    while len(visible) < limit:
        query = select(Notification).where(Notification.recipient_user_id == actor.id)
        if cursor is not None:
            created_at, notification_id = cursor
            query = query.where(or_(Notification.created_at < created_at,
                                    and_(Notification.created_at == created_at,
                                         Notification.id < notification_id)))
        rows = (await db.scalars(query.order_by(Notification.created_at.desc(),
                                               Notification.id.desc()).limit(limit))).all()
        for row in rows:
            if await can_view_notification(db, actor, row):
                visible.append(notification_json(row))
                if len(visible) == limit:
                    break
        if len(rows) < limit:
            break
        cursor = rows[-1].created_at, rows[-1].id
    return visible


@router.post("/notifications/{notification_id}/read")
async def read_notification(notification_id: UUID, request: Request,
                            db: Db, actor: CurrentUser) -> Response:
    # Check before mutation() so an idempotency replay cannot return an old body
    # after access to the task has been revoked.
    existing = await db.get(Notification, notification_id)
    if (existing is None or existing.recipient_user_id != actor.id or
            not await can_view_notification(db, actor, existing)):
        raise HTTPException(404, "NOT_FOUND")

    async def perform() -> tuple[int, dict]:
        row = await db.get(Notification, notification_id, with_for_update=True)
        if (row is None or row.recipient_user_id != actor.id or
                not await can_view_notification(db, actor, row)):
            raise HTTPException(404, "NOT_FOUND")
        if row.read_at is None:
            row.read_at = now()
        return 200, notification_json(row)

    response = await mutation(request, db, actor, EmptyBody(),
                              f"notification:read:{notification_id}", perform)
    # Replay can return a stored body without calling perform(). Use a fresh
    # session so a revoked project membership cannot survive in db's identity map.
    async with request.app.state.session_factory() as current_db:
        current = await current_db.get(Notification, notification_id)
        if (current is None or current.recipient_user_id != actor.id or
                not await can_view_notification(current_db, actor, current)):
            raise HTTPException(404, "NOT_FOUND")
    return response


@router.get("/reports/weekly")
async def weekly_report(db: Db, actor: CurrentUser, week_start: date | None = None) -> dict:
    projects = await report_projects(db, actor)
    current_week_start, _, _ = report_week(now())
    selected = week_start or current_week_start
    if selected.weekday() != 0 or selected > current_week_start:
        raise HTTPException(422, "INVALID_REPORT_WEEK")
    saved = await visible_saved_report(db, actor, selected)
    if saved:
        visible_ids = {str(project.id) for project in projects}
        filtered = [project for project in saved.payload["projects"]
                    if project["project_id"] in visible_ids]
        totals = {key: sum(project[key] for project in filtered)
                  for key in saved.payload["totals"]}
        return {**saved.payload, "projects": filtered, "totals": totals}
    raise HTTPException(404, "REPORT_NOT_GENERATED")
