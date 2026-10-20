"""Project teams and calendar-date sprints."""

from datetime import date
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select

from taskbot_api.models import Project, ProjectMember, ProjectTeam, ProjectTeamMember, Sprint, Task, User, now, utc
from taskbot_api.routes import audit, can_view_project, mutation
from taskbot_api.security import CurrentUser, Db, role_codes
from taskbot_api.task_service import add_event, as_json, visible_task

router = APIRouter(prefix="/api/v1", tags=["planning"])


class TeamCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=160)


class TeamMembers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    members: list[UUID]
    expected_version: int = Field(ge=1)


class SprintCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=160)
    starts_on: date
    ends_on: date


class SprintEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    starts_on: date | None = None
    ends_on: date | None = None


class TaskSprintChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sprint_id: UUID | None
    expected_version: int = Field(ge=1)


async def planning_access(db: Db, actor: CurrentUser, project_id: UUID) -> None:
    roles = await role_codes(db, actor.id)
    if "SUPER_ADMIN" in roles:
        return
    if "PM" in roles and await db.get(ProjectMember, (project_id, actor.id)) is not None:
        return
    raise HTTPException(403, "FORBIDDEN")


def team_json(row: ProjectTeam) -> dict:
    return {"id": str(row.id), "project_id": str(row.project_id),
            "name": row.name, "version": row.version}


def sprint_json(row: Sprint) -> dict:
    return {"id": str(row.id), "project_id": str(row.project_id), "name": row.name,
            "starts_on": row.starts_on.isoformat(), "ends_on": row.ends_on.isoformat(),
            "version": row.version}


async def view_project(db: Db, actor: CurrentUser, project_id: UUID) -> None:
    if await db.get(Project, project_id) is None or not await can_view_project(db, actor, project_id):
        raise HTTPException(404, "NOT_FOUND")


@router.get("/projects/{project_id}/teams")
async def teams(project_id: UUID, db: Db, actor: CurrentUser) -> list[dict]:
    await view_project(db, actor, project_id)
    rows = (await db.scalars(select(ProjectTeam).where(ProjectTeam.project_id == project_id)
                             .order_by(ProjectTeam.name, ProjectTeam.id))).all()
    return [team_json(row) for row in rows]


@router.post("/projects/{project_id}/teams")
async def create_team(project_id: UUID, body: TeamCreate, request: Request,
                      db: Db, actor: CurrentUser) -> Response:
    async def perform() -> tuple[int, dict]:
        await view_project(db, actor, project_id)
        await planning_access(db, actor, project_id)
        if not body.name.strip():
            raise HTTPException(422, "TEAM_NAME_REQUIRED")
        row = ProjectTeam(project_id=project_id, name=body.name.strip(), created_at=now(), version=1)
        db.add(row)
        await db.flush()
        audit(db, actor, "team.created", "project_team", str(row.id))
        return 201, team_json(row)
    return await mutation(request, db, actor, body, f"project:team:create:{project_id}", perform)


@router.get("/teams/{team_id}/members")
async def team_members(team_id: UUID, db: Db, actor: CurrentUser) -> list[str]:
    row = await db.get(ProjectTeam, team_id)
    if row is None:
        raise HTTPException(404, "NOT_FOUND")
    await view_project(db, actor, row.project_id)
    ids = (await db.scalars(select(ProjectTeamMember.user_id).where(ProjectTeamMember.team_id == team_id)
                            .order_by(ProjectTeamMember.user_id))).all()
    return [str(value) for value in ids]


@router.put("/teams/{team_id}/members")
async def change_team_members(team_id: UUID, body: TeamMembers, request: Request,
                              db: Db, actor: CurrentUser) -> Response:
    async def perform() -> tuple[int, dict]:
        row = await db.get(ProjectTeam, team_id, with_for_update=True)
        if row is None:
            raise HTTPException(404, "NOT_FOUND")
        await view_project(db, actor, row.project_id)
        await planning_access(db, actor, row.project_id)
        if row.version != body.expected_version:
            raise HTTPException(409, "VERSION_CONFLICT")
        desired = set(body.members)
        allowed = set((await db.scalars(select(ProjectMember.user_id).join(
            User, User.id == ProjectMember.user_id).where(
            ProjectMember.project_id == row.project_id, User.status == "ACTIVE"))).all())
        if not desired <= allowed:
            raise HTTPException(422, "TEAM_MEMBERS_MUST_BE_ACTIVE_PROJECT_MEMBERS")
        before = set((await db.scalars(select(ProjectTeamMember.user_id).where(
            ProjectTeamMember.team_id == team_id))).all())
        await db.execute(delete(ProjectTeamMember).where(
            ProjectTeamMember.team_id == team_id,
            ProjectTeamMember.user_id.in_(before - desired)))
        for user_id in desired - before:
            db.add(ProjectTeamMember(team_id=team_id, user_id=user_id, joined_at=now()))
        row.version += 1
        audit(db, actor, "team.members_changed", "project_team", str(team_id),
              {"added": sorted(map(str, desired - before)),
               "removed": sorted(map(str, before - desired))})
        return 200, {"team": team_json(row), "members": sorted(map(str, desired))}
    return await mutation(request, db, actor, body, f"team:members:{team_id}", perform)


@router.get("/projects/{project_id}/sprints")
async def sprints(project_id: UUID, db: Db, actor: CurrentUser) -> list[dict]:
    await view_project(db, actor, project_id)
    rows = (await db.scalars(select(Sprint).where(Sprint.project_id == project_id)
                             .order_by(Sprint.starts_on, Sprint.id))).all()
    return [sprint_json(row) for row in rows]


@router.post("/projects/{project_id}/sprints")
async def create_sprint(project_id: UUID, body: SprintCreate, request: Request,
                        db: Db, actor: CurrentUser) -> Response:
    async def perform() -> tuple[int, dict]:
        await view_project(db, actor, project_id)
        await planning_access(db, actor, project_id)
        if not body.name.strip() or body.ends_on < body.starts_on:
            raise HTTPException(422, "INVALID_SPRINT")
        row = Sprint(project_id=project_id, name=body.name.strip(),
                     starts_on=body.starts_on, ends_on=body.ends_on,
                     created_at=now(), version=1)
        db.add(row)
        await db.flush()
        audit(db, actor, "sprint.created", "sprint", str(row.id))
        return 201, sprint_json(row)
    return await mutation(request, db, actor, body, f"project:sprint:create:{project_id}", perform)


@router.patch("/sprints/{sprint_id}")
async def edit_sprint(sprint_id: UUID, body: SprintEdit, request: Request,
                      db: Db, actor: CurrentUser) -> Response:
    async def perform() -> tuple[int, dict]:
        row = await db.get(Sprint, sprint_id, with_for_update=True)
        if row is None:
            raise HTTPException(404, "NOT_FOUND")
        await view_project(db, actor, row.project_id)
        await planning_access(db, actor, row.project_id)
        if row.version != body.expected_version:
            raise HTTPException(409, "VERSION_CONFLICT")
        name = body.name.strip() if body.name is not None else row.name
        starts = body.starts_on or row.starts_on
        ends = body.ends_on or row.ends_on
        if not name or ends < starts:
            raise HTTPException(422, "INVALID_SPRINT")
        row.name, row.starts_on, row.ends_on = name, starts, ends
        row.version += 1
        audit(db, actor, "sprint.updated", "sprint", str(row.id))
        return 200, sprint_json(row)
    return await mutation(request, db, actor, body, f"sprint:edit:{sprint_id}", perform)


@router.put("/tasks/{task_id}/sprint")
async def assign_sprint(task_id: UUID, body: TaskSprintChange, request: Request,
                        db: Db, actor: CurrentUser) -> Response:
    async def perform() -> tuple[int, dict]:
        task = await visible_task(db, actor, task_id, lock=True)
        await planning_access(db, actor, task.project_id)
        if task.status in {"DONE", "CANCELLED"}:
            raise HTTPException(409, "TASK_TERMINAL")
        if task.version != body.expected_version:
            raise HTTPException(409, "VERSION_CONFLICT")
        if body.sprint_id:
            sprint = await db.get(Sprint, body.sprint_id)
            if sprint is None or sprint.project_id != task.project_id:
                raise HTTPException(422, "SPRINT_MUST_SHARE_PROJECT")
        if task.sprint_id != body.sprint_id:
            before = as_json(task)
            task.sprint_id = body.sprint_id
            task.version += 1
            task.updated_at = now()
            await add_event(db, task, actor, "SPRINT_CHANGED", before, as_json(task),
                            request.headers.get("Idempotency-Key"))
        return 200, as_json(task)
    return await mutation(request, db, actor, body, f"task:sprint:{task_id}", perform)
