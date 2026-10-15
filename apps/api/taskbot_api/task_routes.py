"""Phase 3 task HTTP operations. Status is never accepted by PATCH."""

from datetime import date
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from taskbot_api.models import ProjectMember, Task, TaskComment, TaskEvent
from taskbot_api.routes import can_view_project, mutation
from taskbot_api.security import CurrentUser, Db, role_codes
from taskbot_api.task_service import (
    PRIORITIES, TYPES, add_comment, as_json, available_actions, comment_json,
    create_task, event_json, is_overdue, set_qa_reviewer, transition, update_task,
    visible_task,
)


router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


async def task_response(db, actor, task: Task) -> dict:
    return {**as_json(task), "overdue": is_overdue(task),
            "available_actions": await available_actions(db, actor, task)}


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=30000)
    assignee_id: UUID | None = None
    qa_reviewer_id: UUID | None = None
    priority: str = "MEDIUM"
    type: str = "DEVELOPMENT"
    due_date: date

    @model_validator(mode="after")
    def validate_fields(self):
        if self.priority not in PRIORITIES or self.type not in TYPES:
            raise ValueError("unsupported task priority or type")
        return self


@router.post("")
async def create(body: TaskCreate, request: Request, db: Db, actor: CurrentUser) -> Response:
    async def perform():
        task = await create_task(db, actor, **body.model_dump(), request_id=request.headers.get("Idempotency-Key"))
        return 201, await task_response(db, actor, task)
    return await mutation(request, db, actor, body, "tasks:create", perform)


@router.get("")
async def list_tasks(db: Db, actor: CurrentUser, project_id: UUID | None = None,
                     status: str | None = None, assignee_id: UUID | None = None,
                     created_by: UUID | None = None, qa_reviewer_id: UUID | None = None,
                     priority: str | None = None, cursor: UUID | None = None,
                     limit: int = 100) -> list[dict]:
    if not 1 <= limit <= 100:
        raise HTTPException(422, "INVALID_PAGE_LIMIT")
    stmt = select(Task).order_by(Task.id).limit(limit)
    if "SUPER_ADMIN" not in await role_codes(db, actor.id):
        stmt = stmt.join(ProjectMember, ProjectMember.project_id == Task.project_id).where(
            ProjectMember.user_id == actor.id)
    for column, value in ((Task.project_id, project_id), (Task.status, status),
                          (Task.assignee_id, assignee_id), (Task.created_by, created_by),
                          (Task.qa_reviewer_id, qa_reviewer_id), (Task.priority, priority)):
        if value is not None:
            stmt = stmt.where(column == value)
    if cursor:
        stmt = stmt.where(Task.id > cursor)
    return [await task_response(db, actor, row) for row in (await db.scalars(stmt)).all()]


@router.get("/{task_id}")
async def get_task(task_id: UUID, db: Db, actor: CurrentUser) -> dict:
    return await task_response(db, actor, await visible_task(db, actor, task_id))


class TaskEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=30000)
    assignee_id: UUID | None = None
    priority: str | None = None
    type: str | None = None

    @model_validator(mode="after")
    def validate_fields(self):
        if self.priority is not None and self.priority not in PRIORITIES:
            raise ValueError("unsupported priority")
        if self.type is not None and self.type not in TYPES:
            raise ValueError("unsupported type")
        return self


@router.patch("/{task_id}")
async def edit_task(task_id: UUID, body: TaskEdit, request: Request,
                    db: Db, actor: CurrentUser) -> Response:
    async def perform():
        task = await visible_task(db, actor, task_id, lock=True)
        changes = body.model_dump(exclude_unset=True, exclude={"expected_version"})
        updated = await update_task(db, actor, task, expected_version=body.expected_version,
                                    changes=changes, request_id=request.headers.get("Idempotency-Key"))
        return 200, await task_response(db, actor, updated)
    return await mutation(request, db, actor, body, f"task:edit:{task_id}", perform)


class QaReviewerChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    qa_reviewer_id: UUID | None
    expected_version: int = Field(ge=1)


@router.put("/{task_id}/qa-reviewer")
async def change_qa(task_id: UUID, body: QaReviewerChange, request: Request,
                    db: Db, actor: CurrentUser) -> Response:
    async def perform():
        task = await visible_task(db, actor, task_id, lock=True)
        updated = await set_qa_reviewer(db, actor, task, body.qa_reviewer_id,
                                        body.expected_version, request.headers.get("Idempotency-Key"))
        return 200, await task_response(db, actor, updated)
    return await mutation(request, db, actor, body, f"task:qa-reviewer:{task_id}", perform)


class TaskAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=4000)
    comment: str | None = Field(default=None, max_length=4000)
    result: str | None = Field(default=None, max_length=10000)
    author_unavailable_confirmed: bool = False


async def action(task_id: UUID, action: str, body: TaskAction, request: Request,
                 db: Db, actor: CurrentUser) -> Response:
    if action not in {"accept", "reject", "start", "block", "resume", "submit-review",
                      "approve", "admin-approve", "request-rework", "qa-review/done", "cancel"}:
        raise HTTPException(404, "UNKNOWN_ACTION")
    async def perform():
        task = await visible_task(db, actor, task_id, lock=True)
        updated = await transition(db, actor, task, action, body.expected_version,
                                   reason=body.reason, comment=body.comment, result=body.result,
                                   author_unavailable_confirmed=body.author_unavailable_confirmed,
                                   request_id=request.headers.get("Idempotency-Key"))
        return 200, await task_response(db, actor, updated)
    return await mutation(request, db, actor, body, f"task:{action}:{task_id}", perform)


class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=10000)


@router.post("/{task_id}/comments")
async def post_comment(task_id: UUID, body: CommentCreate, request: Request,
                       db: Db, actor: CurrentUser) -> Response:
    async def perform():
        task = await visible_task(db, actor, task_id, lock=True)
        comment = await add_comment(db, actor, task, body.body,
                                    request.headers.get("Idempotency-Key"))
        return 201, comment_json(comment)
    return await mutation(request, db, actor, body, f"task:comment:{task_id}", perform)


@router.get("/{task_id}/comments")
async def comments(task_id: UUID, db: Db, actor: CurrentUser) -> list[dict]:
    await visible_task(db, actor, task_id)
    rows = (await db.scalars(select(TaskComment).where(TaskComment.task_id == task_id)
                             .order_by(TaskComment.created_at, TaskComment.id).limit(100))).all()
    return [comment_json(row) for row in rows]


@router.get("/{task_id}/events")
async def events(task_id: UUID, db: Db, actor: CurrentUser) -> list[dict]:
    await visible_task(db, actor, task_id)
    rows = (await db.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id)
                             .order_by(TaskEvent.sequence).limit(200))).all()
    return [event_json(row) for row in rows]


@router.get("/{task_id}/qa-reviews")
async def qa_reviews(task_id: UUID, db: Db, actor: CurrentUser) -> list[dict]:
    history = await events(task_id, db, actor)
    return [event for event in history
            if event["type"] in {"REQUEST_REWORK", "QA_REVIEW_DONE"}
            and event["before_data"].get("qa_review_status")
            != event["after_data"].get("qa_review_status")]


# Keep the action catch-all after the concrete comment and history endpoints.
router.add_api_route("/{task_id}/{action:path}", action, methods=["POST"])
