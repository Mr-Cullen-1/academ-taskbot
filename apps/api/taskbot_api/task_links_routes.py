"""Task watchers and same-project dependency links."""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from taskbot_api.models import Project, ProjectMember, Task, TaskDependency, TaskWatcher, now
from taskbot_api.routes import audit, mutation
from taskbot_api.security import CurrentUser, Db, role_codes
from taskbot_api.task_service import add_event, visible_task

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


class WatcherAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: UUID


class DependencyAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")
    depends_on_task_id: UUID


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.get("/{task_id}/watchers")
async def watchers(task_id: UUID, db: Db, actor: CurrentUser) -> list[dict]:
    await visible_task(db, actor, task_id)
    rows = (await db.scalars(select(TaskWatcher).where(TaskWatcher.task_id == task_id)
                             .order_by(TaskWatcher.created_at, TaskWatcher.user_id))).all()
    return [{"user_id": str(row.user_id), "created_at": row.created_at.isoformat()} for row in rows]


@router.post("/{task_id}/watchers")
async def add_watcher(task_id: UUID, body: WatcherAdd, request: Request,
                      db: Db, actor: CurrentUser) -> Response:
    async def perform() -> tuple[int, dict]:
        task = await visible_task(db, actor, task_id, lock=True)
        if body.user_id != actor.id or await db.get(ProjectMember, (task.project_id, actor.id)) is None:
            raise HTTPException(403, "FORBIDDEN")
        existing = await db.get(TaskWatcher, (task_id, actor.id))
        if existing is not None:
            return 200, {"task_id": str(task_id), "user_id": str(actor.id)}
        db.add(TaskWatcher(task_id=task_id, user_id=actor.id, created_at=now()))
        audit(db, actor, "WATCH_TASK", "task", str(task_id))
        return 201, {"task_id": str(task_id), "user_id": str(actor.id)}

    return await mutation(request, db, actor, body, f"task:watch:{task_id}", perform)


@router.delete("/{task_id}/watchers/{user_id}")
async def remove_watcher(task_id: UUID, user_id: UUID, request: Request,
                         db: Db, actor: CurrentUser) -> Response:
    async def perform() -> tuple[int, dict]:
        task = await visible_task(db, actor, task_id, lock=True)
        if user_id != actor.id or await db.get(ProjectMember, (task.project_id, actor.id)) is None:
            raise HTTPException(403, "FORBIDDEN")
        existing = await db.get(TaskWatcher, (task_id, actor.id))
        if existing is None:
            raise HTTPException(404, "NOT_FOUND")
        await db.delete(existing)
        audit(db, actor, "UNWATCH_TASK", "task", str(task_id))
        return 200, {"removed": True}

    return await mutation(request, db, actor, EmptyBody(), f"task:unwatch:{task_id}:{user_id}", perform)


@router.get("/{task_id}/dependencies")
async def dependencies(task_id: UUID, db: Db, actor: CurrentUser) -> list[dict]:
    await visible_task(db, actor, task_id)
    ids = (await db.scalars(select(TaskDependency.depends_on_task_id).where(
        TaskDependency.task_id == task_id).order_by(TaskDependency.depends_on_task_id))).all()
    return [{"depends_on_task_id": str(value)} for value in ids]


@router.post("/{task_id}/dependencies")
async def add_dependency(task_id: UUID, body: DependencyAdd, request: Request,
                         db: Db, actor: CurrentUser) -> Response:
    async def perform() -> tuple[int, dict]:
        task = await visible_task(db, actor, task_id)
        roles = await role_codes(db, actor.id)
        if "PM" not in roles or await db.get(ProjectMember, (task.project_id, actor.id)) is None:
            raise HTTPException(403, "FORBIDDEN")
        if task.status in {"DONE", "CANCELLED"}:
            raise HTTPException(409, "TASK_TERMINAL")
        # Lock the shared project row before the task to serialize graph edits.
        await db.get(Project, task.project_id, with_for_update=True)
        task = await visible_task(db, actor, task_id, lock=True)
        target = await db.get(Task, body.depends_on_task_id)
        if target is None or target.project_id != task.project_id:
            raise HTTPException(422, "DEPENDENCY_MUST_SHARE_PROJECT")
        if target.id == task.id:
            raise HTTPException(422, "DEPENDENCY_CYCLE")
        existing = await db.get(TaskDependency, (task.id, target.id))
        if existing is not None:
            return 200, {"task_id": str(task.id), "depends_on_task_id": str(target.id)}
        rows = (await db.execute(select(TaskDependency.task_id, TaskDependency.depends_on_task_id)
                                 .join(Task, Task.id == TaskDependency.task_id)
                                 .where(Task.project_id == task.project_id))).all()
        links: dict[UUID, set[UUID]] = {}
        for source, destination in rows:
            links.setdefault(source, set()).add(destination)
        frontier = [target.id]
        seen = set()
        while frontier:
            current = frontier.pop()
            if current == task.id:
                raise HTTPException(422, "DEPENDENCY_CYCLE")
            if current in seen:
                continue
            seen.add(current)
            frontier.extend(links.get(current, ()))
        db.add(TaskDependency(task_id=task.id, depends_on_task_id=target.id, created_at=now()))
        task.version += 1
        task.updated_at = now()
        await add_event(db, task, actor, "DEPENDENCY_ADDED", {},
                        {"depends_on_task_id": str(target.id)},
                        request.headers.get("Idempotency-Key"))
        return 201, {"task_id": str(task.id), "depends_on_task_id": str(target.id)}

    return await mutation(request, db, actor, body, f"task:dependency:{task_id}", perform)
