"""Private delivery log and Super Admin/Team Lead weekly report API."""

from datetime import date

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from taskbot_api.models import Notification, now, utc
from taskbot_api.reporting import report_projects, report_week, visible_saved_report
from taskbot_api.security import CurrentUser, Db


router = APIRouter(prefix="/api/v1", tags=["notifications", "reports"])


def notification_json(row: Notification) -> dict:
    return {"id": str(row.id), "task_id": str(row.task_id) if row.task_id else None,
            "kind": row.kind, "body": row.body, "state": row.state,
            "scheduled_at": utc(row.scheduled_at).isoformat(),
            "sent_at": utc(row.sent_at).isoformat() if row.sent_at else None}


@router.get("/notifications")
async def my_notifications(db: Db, actor: CurrentUser, limit: int = 50) -> list[dict]:
    if not 1 <= limit <= 100:
        raise HTTPException(422, "INVALID_PAGE_LIMIT")
    rows = (await db.scalars(select(Notification).where(Notification.recipient_user_id == actor.id)
                             .order_by(Notification.created_at.desc(), Notification.id.desc())
                             .limit(limit))).all()
    return [notification_json(row) for row in rows]


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
