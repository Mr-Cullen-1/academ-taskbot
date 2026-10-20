"""Author/PM decisions on assignee deadline extension requests."""

from datetime import date
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from taskbot_api.models import DeadlineChange, DeadlineRequest, ProjectMember, Task, now, utc
from taskbot_api.routes import mutation
from taskbot_api.security import CurrentUser, Db, role_codes
from taskbot_api.task_service import add_event, as_json, visible_task

router = APIRouter(prefix="/api/v1", tags=["deadlines"])


class DeadlineRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposed_due_date: date
    reason: str = Field(min_length=1, max_length=4000)
    progress_note: str = Field(min_length=1, max_length=4000)
    expected_version: int = Field(ge=1)


class DeadlineDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=4000)


def request_json(row: DeadlineRequest) -> dict:
    return {"id": str(row.id), "task_id": str(row.task_id),
            "requested_by": str(row.requested_by),
            "proposed_due_date": row.proposed_due_date.isoformat(),
            "reason": row.reason, "progress_note": row.progress_note,
            "status": row.status,
            "decided_by": str(row.decided_by) if row.decided_by else None,
            "decided_at": utc(row.decided_at).isoformat() if row.decided_at else None,
            "decision_reason": row.decision_reason,
            "version": row.version}


@router.get("/tasks/{task_id}/deadline-requests")
async def list_requests(task_id: UUID, db: Db, actor: CurrentUser) -> list[dict]:
    await visible_task(db, actor, task_id)
    rows = (await db.scalars(select(DeadlineRequest).where(DeadlineRequest.task_id == task_id)
                             .order_by(DeadlineRequest.created_at, DeadlineRequest.id))).all()
    return [request_json(row) for row in rows]


@router.post("/tasks/{task_id}/deadline-requests")
async def request_deadline(task_id: UUID, body: DeadlineRequestCreate, request: Request,
                           db: Db, actor: CurrentUser) -> Response:
    async def perform() -> tuple[int, dict]:
        row = await create_request_record(db, actor, task_id, body,
                                          request.headers.get("Idempotency-Key"))
        return 201, request_json(row)

    return await mutation(request, db, actor, body, f"deadline:request:{task_id}", perform)


async def create_request_record(db: Db, actor: CurrentUser, task_id: UUID,
                                body: DeadlineRequestCreate, request_id: str | None) -> DeadlineRequest:
    task = await visible_task(db, actor, task_id, lock=True)
    if task.assignee_id != actor.id:
        raise HTTPException(403, "FORBIDDEN")
    if task.status in {"DONE", "CANCELLED", "REJECTED"}:
        raise HTTPException(409, "INVALID_STATUS_TRANSITION")
    if task.version != body.expected_version:
        raise HTTPException(409, "VERSION_CONFLICT")
    if (body.proposed_due_date <= task.current_due_date or
            not body.reason.strip() or not body.progress_note.strip()):
        raise HTTPException(422, "INVALID_DEADLINE_REQUEST")
    pending = await db.scalar(select(DeadlineRequest.id).where(
        DeadlineRequest.task_id == task_id, DeadlineRequest.status == "PENDING").limit(1))
    if pending is not None:
        raise HTTPException(409, "DEADLINE_REQUEST_ALREADY_PENDING")
    row = DeadlineRequest(task_id=task_id, requested_by=actor.id,
                          proposed_due_date=body.proposed_due_date,
                          reason=body.reason.strip(), progress_note=body.progress_note.strip(),
                          status="PENDING", created_at=now(), version=1)
    db.add(row)
    await db.flush()
    before = as_json(task)
    task.version += 1
    task.updated_at = now()
    await add_event(db, task, actor, "DEADLINE_REQUESTED", before,
                    {**as_json(task), "deadline_request_id": str(row.id),
                     "proposed_due_date": row.proposed_due_date.isoformat(),
                     "reason": row.reason, "progress_note": row.progress_note}, request_id)
    return row


async def decide(request_id: UUID, body: DeadlineDecision, request: Request,
                 db: Db, actor: CurrentUser, *, approve: bool) -> Response:
    async def perform() -> tuple[int, dict]:
        row = await decide_request_record(db, actor, request_id, body, approve=approve,
                                          request_idempotency=request.headers.get("Idempotency-Key"))
        return 200, request_json(row)

    verb = "approve" if approve else "reject"
    return await mutation(request, db, actor, body, f"deadline:{verb}:{request_id}", perform)


async def decide_request_record(db: Db, actor: CurrentUser, request_id: UUID,
                                body: DeadlineDecision, *, approve: bool,
                                request_idempotency: str | None) -> DeadlineRequest:
    reference = await db.get(DeadlineRequest, request_id)
    if reference is None:
        raise HTTPException(404, "NOT_FOUND")
    task = await visible_task(db, actor, reference.task_id, lock=True)
    roles = await role_codes(db, actor.id)
    can_decide = (task.created_by == actor.id or
                  ("PM" in roles and await db.get(ProjectMember, (task.project_id, actor.id)) is not None))
    if not can_decide:
        raise HTTPException(403, "FORBIDDEN")
    row = await db.get(DeadlineRequest, request_id, with_for_update=True)
    if row.status != "PENDING" or row.version != body.expected_version:
        raise HTTPException(409, "VERSION_CONFLICT")
    if task.status in {"DONE", "CANCELLED", "REJECTED"} or task.assignee_id != row.requested_by:
        raise HTTPException(409, "DEADLINE_REQUEST_STALE")
    before = as_json(task)
    stamp = now()
    if approve:
        if row.proposed_due_date <= task.current_due_date:
            raise HTTPException(409, "DEADLINE_NO_LONGER_EXTENDS")
        db.add(DeadlineChange(request_id=row.id, task_id=task.id,
                              previous_due_date=task.current_due_date,
                              new_due_date=row.proposed_due_date, approved_by=actor.id,
                              approved_at=stamp))
        task.current_due_date = row.proposed_due_date
        row.status = "APPROVED"
    else:
        row.status = "REJECTED"
    row.version += 1
    row.decided_by = actor.id
    row.decided_at = stamp
    row.decision_reason = body.reason.strip() if body.reason else None
    task.version += 1
    task.updated_at = stamp
    await add_event(db, task, actor,
                    "DEADLINE_APPROVED" if approve else "DEADLINE_REJECTED",
                    before, {**as_json(task), "deadline_request_id": str(row.id),
                             "decision_reason": row.decision_reason},
                    request_idempotency, occurred_at=stamp)
    return row


@router.post("/deadline-requests/{request_id}/approve")
async def approve_deadline(request_id: UUID, body: DeadlineDecision, request: Request,
                           db: Db, actor: CurrentUser) -> Response:
    return await decide(request_id, body, request, db, actor, approve=True)


@router.post("/deadline-requests/{request_id}/reject")
async def reject_deadline(request_id: UUID, body: DeadlineDecision, request: Request,
                          db: Db, actor: CurrentUser) -> Response:
    return await decide(request_id, body, request, db, actor, approve=False)
