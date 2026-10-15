"""Phase 2 HTTP surface: login, fixed roles, users and project membership."""

import json
import hmac
from ipaddress import ip_address
from typing import Awaitable, Callable
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from taskbot_api.models import (
    ApiIdempotency, AuditLog, LoginChallenge, LoginProof, Project, ProjectMember,
    Task, User, UserRole, WebSession, now, utc,
)
from taskbot_api.security import (
    CHALLENGE_COOKIE, COOKIE_NAME, CurrentUser, Db, TelegramProof, digest,
    new_session, new_token, require_super_admin, role_codes, verify_telegram_proof,
)
from datetime import timedelta


router = APIRouter(prefix="/api/v1")
dev_router = APIRouter(prefix="/api/v1", include_in_schema=False)
NON_ADMIN_ROLES = {"PM", "TEAM_LEAD", "SUBJECT_DEVELOPER", "QA"}


class DevLoginProof(BaseModel):
    model_config = ConfigDict(extra="forbid")
    secret: SecretStr


def local_dev_request(request: Request, origin: str) -> bool:
    if request.headers.get("Origin") != origin or request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return False
    if any(header in request.headers for header in ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto")):
        return False
    if request.client is None:
        return False
    try:
        return ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def user_data(user: User, roles: set[str] | None = None, *, private: bool = False) -> dict:
    result = {"id": str(user.id), "name": user.name, "status": user.status,
              "roles": sorted(roles or ()), "version": user.version}
    if private:
        result.update(telegram_user_id=user.telegram_user_id, phone=user.phone)
    return result


def project_data(project: Project) -> dict:
    return {"id": str(project.id), "name": project.name, "description": project.description,
            "owner_user_id": str(project.owner_user_id), "version": project.version}


def audit(db: AsyncSession, actor: User | None, action: str, target_type: str,
          target_id: str, details: dict | None = None) -> None:
    db.add(AuditLog(actor_user_id=actor.id if actor else None, action=action,
                    target_type=target_type, target_id=target_id, details=details or {}))


async def mutation(request: Request, db: AsyncSession, actor: User, payload: BaseModel,
                   operation: str, perform: Callable[[], Awaitable[tuple[int, dict]]]) -> JSONResponse:
    key = request.headers.get("Idempotency-Key", "")
    if not 8 <= len(key) <= 120:
        raise HTTPException(400, "IDEMPOTENCY_KEY_REQUIRED")
    fingerprint = digest(json.dumps(payload.model_dump(mode="json"), sort_keys=True) + operation)
    actor_id = actor.id

    async def replay() -> JSONResponse | None:
        prior = await db.scalar(select(ApiIdempotency).where(
            ApiIdempotency.actor_user_id == actor_id, ApiIdempotency.key == key))
        if prior is None:
            return None
        if prior.operation != operation or prior.payload_digest != fingerprint:
            raise HTTPException(409, "IDEMPOTENCY_KEY_CONFLICT")
        return JSONResponse(prior.response_body, status_code=prior.response_status)

    existing = await replay()
    if existing is not None:
        return existing
    try:
        # Dependency lookups may already have opened an implicit read transaction.
        await db.commit()
        async with db.begin():
            db.add(ApiIdempotency(actor_user_id=actor_id, key=key, operation=operation,
                                  payload_digest=fingerprint, response_body={}, response_status=200))
            await db.flush()
            status, body = await perform()
            record = await db.scalar(select(ApiIdempotency).where(
                ApiIdempotency.actor_user_id == actor_id, ApiIdempotency.key == key))
            record.response_body, record.response_status = body, status
        return JSONResponse(body, status_code=status)
    except IntegrityError:
        await db.rollback()
        existing = await replay()
        if existing is not None:
            return existing
        raise


@router.get("/auth/telegram/challenge")
async def login_challenge(request: Request, db: Db) -> Response:
    settings = request.app.state.settings
    if not settings.web_origin or not settings.bot_token:
        raise HTTPException(503, "TELEGRAM_LOGIN_NOT_CONFIGURED")
    if request.headers.get("Origin") != settings.web_origin:
        raise HTTPException(403, "ORIGIN_INVALID")
    token = new_token()
    db.add(LoginChallenge(digest=digest(token), expires_at=now() + timedelta(minutes=5)))
    await db.commit()
    response = JSONResponse({"ready": True})
    response.set_cookie(CHALLENGE_COOKIE, token, httponly=True, secure=settings.session_cookie_secure,
                        samesite="lax", max_age=300, path="/api/v1/auth")
    return response


@router.post("/auth/telegram")
async def telegram_login(proof: TelegramProof, request: Request, db: Db) -> Response:
    settings = request.app.state.settings
    if not settings.web_origin or request.headers.get("Origin") != settings.web_origin:
        raise HTTPException(403, "ORIGIN_INVALID")
    if not settings.bot_token:
        raise HTTPException(503, "TELEGRAM_LOGIN_NOT_CONFIGURED")
    challenge_token = request.cookies.get(CHALLENGE_COOKIE, "")
    if not challenge_token:
        raise HTTPException(403, "LOGIN_CHALLENGE_REQUIRED")
    challenge = await db.get(LoginChallenge, digest(challenge_token))
    if challenge is None or utc(challenge.expires_at) <= now():
        raise HTTPException(403, "LOGIN_CHALLENGE_INVALID")
    verify_telegram_proof(proof, settings.bot_token.get_secret_value(), settings.login_max_age_seconds)
    user = await db.scalar(select(User).where(User.telegram_user_id == proof.id))
    if user is None or user.status != "ACTIVE":
        raise HTTPException(403, "ACCOUNT_INACTIVE")
    session, token, csrf = new_session(user, settings.session_hours)
    try:
        db.add(LoginProof(digest=digest(proof.hash)))
        db.add(session)
        await db.delete(challenge)
        audit(db, user, "auth.login", "user", str(user.id))
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "TELEGRAM_PROOF_REPLAYED") from None
    response = JSONResponse({"user": user_data(user, await role_codes(db, user.id)), "csrf_token": csrf})
    response.set_cookie(COOKIE_NAME, token, httponly=True, secure=settings.session_cookie_secure,
                        samesite="lax", max_age=settings.session_hours * 3600, path="/")
    response.delete_cookie(CHALLENGE_COOKIE, path="/api/v1/auth")
    return response


@dev_router.post("/auth/dev")
async def local_dev_login(proof: DevLoginProof, request: Request, db: Db) -> Response:
    settings = request.app.state.settings
    if not settings.dev_login_enabled or settings.app_env != "development":
        raise HTTPException(404, "NOT_FOUND")
    if not local_dev_request(request, settings.web_origin):
        raise HTTPException(403, "LOCAL_REQUEST_REQUIRED")
    if not hmac.compare_digest(proof.secret.get_secret_value(),
                               settings.dev_login_secret.get_secret_value()):
        raise HTTPException(403, "DEV_LOGIN_INVALID")
    user = await db.scalar(select(User).where(User.telegram_user_id == settings.dev_login_telegram_id))
    if user is None or user.status != "ACTIVE" or "SUPER_ADMIN" not in await role_codes(db, user.id):
        raise HTTPException(403, "DEV_ADMIN_UNAVAILABLE")
    session, token, csrf = new_session(user, settings.session_hours)
    db.add(session)
    audit(db, user, "auth.dev_login", "user", str(user.id))
    await db.commit()
    response = JSONResponse({"user": user_data(user, await role_codes(db, user.id)), "csrf_token": csrf})
    response.set_cookie(COOKIE_NAME, token, httponly=True, secure=settings.session_cookie_secure,
                        samesite="lax", max_age=settings.session_hours * 3600, path="/")
    return response


@router.post("/auth/logout")
async def logout(request: Request, db: Db, user: CurrentUser) -> Response:
    request.state.web_session.revoked_at = now()
    audit(db, user, "auth.logout", "user", str(user.id))
    await db.commit()
    response = Response(status_code=204)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@router.get("/users/me")
async def me(db: Db, user: CurrentUser) -> dict:
    return user_data(user, await role_codes(db, user.id), private=True)


@router.get("/users")
async def users(db: Db, user: CurrentUser, status: str | None = None,
                cursor: UUID | None = None, limit: int = 100) -> list[dict]:
    await require_super_admin(db, user)
    if not 1 <= limit <= 100:
        raise HTTPException(422, "INVALID_PAGE_LIMIT")
    stmt = select(User).order_by(User.id).limit(limit)
    if cursor is not None:
        stmt = stmt.where(User.id > cursor)
    if status:
        if status not in {"PENDING", "ACTIVE", "REJECTED", "BLOCKED"}:
            raise HTTPException(422, "INVALID_STATUS")
        stmt = stmt.where(User.status == status)
    rows = (await db.scalars(stmt)).all()
    return [user_data(row, await role_codes(db, row.id), private=True) for row in rows]


@router.get("/users/pending")
async def pending_users(db: Db, user: CurrentUser) -> list[dict]:
    return await users(db, user, "PENDING")


class StatusChange(BaseModel):
    status: str
    expected_version: int = Field(ge=1)
    roles: list[str] | None = None
    reason: str | None = Field(default=None, max_length=500)


@router.patch("/users/{user_id}/status")
async def change_status(user_id: UUID, body: StatusChange, request: Request,
                        db: Db, actor: CurrentUser) -> Response:
    await require_super_admin(db, actor)

    async def perform() -> tuple[int, dict]:
        target = await db.get(User, user_id, with_for_update=True)
        if target is None:
            raise HTTPException(404, "NOT_FOUND")
        if target.version != body.expected_version:
            raise HTTPException(409, "VERSION_CONFLICT")
        allowed = {("PENDING", "ACTIVE"), ("PENDING", "REJECTED"),
                   ("ACTIVE", "BLOCKED"), ("BLOCKED", "ACTIVE")}
        if (target.status, body.status) not in allowed:
            raise HTTPException(409, "INVALID_STATUS_TRANSITION")
        roles = await role_codes(db, target.id)
        if "SUPER_ADMIN" in roles and body.status != "ACTIVE":
            raise HTTPException(409, "SOLE_SUPER_ADMIN")
        if target.status == "PENDING" and body.status == "ACTIVE":
            chosen = set(body.roles or [])
            if not chosen or chosen - NON_ADMIN_ROLES:
                raise HTTPException(422, "ACTIVE_ROLES_REQUIRED")
            for code in chosen:
                db.add(UserRole(user_id=target.id, code=code, granted_by=actor.id))
            roles = chosen
        elif body.roles is not None:
            raise HTTPException(422, "ROLES_ONLY_ON_APPROVAL")
        previous = target.status
        target.status, target.version, target.updated_at = body.status, target.version + 1, now()
        if body.status == "BLOCKED":
            await db.execute(update(WebSession).where(WebSession.user_id == target.id,
                WebSession.revoked_at.is_(None)).values(revoked_at=now()))
        audit(db, actor, "user.status_changed", "user", str(target.id),
              {"from": previous, "to": body.status, "reason": body.reason})
        return 200, user_data(target, roles, private=True)

    return await mutation(request, db, actor, body, f"status:{user_id}", perform)


class RoleChange(BaseModel):
    roles: list[str]
    expected_version: int = Field(ge=1)


@router.put("/users/{user_id}/roles")
async def change_roles(user_id: UUID, body: RoleChange, request: Request,
                       db: Db, actor: CurrentUser) -> Response:
    await require_super_admin(db, actor)

    async def perform() -> tuple[int, dict]:
        target = await db.get(User, user_id, with_for_update=True)
        if target is None:
            raise HTTPException(404, "NOT_FOUND")
        if target.status != "ACTIVE" or target.version != body.expected_version:
            raise HTTPException(409, "ACCOUNT_OR_VERSION_CONFLICT")
        desired = set(body.roles)
        if not desired or desired - NON_ADMIN_ROLES:
            raise HTTPException(422, "INVALID_ROLE_SET")
        existing = await role_codes(db, target.id)
        await db.execute(delete(UserRole).where(UserRole.user_id == target.id,
                                               UserRole.code != "SUPER_ADMIN"))
        for code in desired:
            db.add(UserRole(user_id=target.id, code=code, granted_by=actor.id))
        target.version += 1
        target.updated_at = now()
        audit(db, actor, "user.roles_changed", "user", str(target.id),
              {"from": sorted(existing), "to": sorted(desired | (existing & {"SUPER_ADMIN"}))})
        return 200, user_data(target, desired | (existing & {"SUPER_ADMIN"}), private=True)

    return await mutation(request, db, actor, body, f"roles:{user_id}", perform)


async def can_view_project(db: AsyncSession, user: User, project_id: UUID) -> bool:
    if "SUPER_ADMIN" in await role_codes(db, user.id):
        return True
    return await db.get(ProjectMember, (project_id, user.id)) is not None


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=10000)


@router.post("/projects")
async def create_project(body: ProjectCreate, request: Request,
                         db: Db, actor: CurrentUser) -> Response:
    if not (await role_codes(db, actor.id)) & {"SUPER_ADMIN", "PM"}:
        raise HTTPException(403, "FORBIDDEN")

    async def perform() -> tuple[int, dict]:
        project = Project(name=body.name.strip(), description=body.description,
                          owner_user_id=actor.id)
        db.add(project)
        await db.flush()
        db.add(ProjectMember(project_id=project.id, user_id=actor.id))
        audit(db, actor, "project.created", "project", str(project.id))
        return 201, project_data(project)

    return await mutation(request, db, actor, body, "projects:create", perform)


@router.get("/projects")
async def projects(db: Db, actor: CurrentUser, cursor: UUID | None = None,
                   limit: int = 100) -> list[dict]:
    if not 1 <= limit <= 100:
        raise HTTPException(422, "INVALID_PAGE_LIMIT")
    stmt = select(Project).order_by(Project.id).limit(limit)
    if cursor is not None:
        stmt = stmt.where(Project.id > cursor)
    if "SUPER_ADMIN" not in await role_codes(db, actor.id):
        stmt = stmt.join(ProjectMember).where(ProjectMember.user_id == actor.id)
    return [project_data(p) for p in (await db.scalars(stmt)).all()]


@router.get("/projects/{project_id}")
async def project(project_id: UUID, db: Db, actor: CurrentUser) -> dict:
    row = await db.get(Project, project_id)
    if row is None or not await can_view_project(db, actor, project_id):
        raise HTTPException(404, "NOT_FOUND")
    return project_data(row)


@router.get("/projects/{project_id}/members")
async def project_members(project_id: UUID, db: Db, actor: CurrentUser) -> list[dict]:
    if not await can_view_project(db, actor, project_id) or await db.get(Project, project_id) is None:
        raise HTTPException(404, "NOT_FOUND")
    rows = (await db.scalars(select(User).join(ProjectMember, User.id == ProjectMember.user_id)
                             .where(ProjectMember.project_id == project_id, User.status == "ACTIVE")
                             .order_by(User.name, User.id))).all()
    return [user_data(row, await role_codes(db, row.id)) for row in rows]


class MembersChange(BaseModel):
    members: list[UUID]
    expected_version: int = Field(ge=1)


@router.put("/projects/{project_id}/members")
async def change_members(project_id: UUID, body: MembersChange, request: Request,
                         db: Db, actor: CurrentUser) -> Response:
    await require_super_admin(db, actor)

    async def perform() -> tuple[int, dict]:
        row = await db.get(Project, project_id, with_for_update=True)
        if row is None:
            raise HTTPException(404, "NOT_FOUND")
        if row.version != body.expected_version:
            raise HTTPException(409, "VERSION_CONFLICT")
        members = set(body.members)
        if row.owner_user_id not in members:
            raise HTTPException(422, "OWNER_MUST_BE_MEMBER")
        active = set((await db.scalars(select(User.id).where(User.id.in_(members), User.status == "ACTIVE"))).all())
        if active != members:
            raise HTTPException(422, "MEMBERS_MUST_BE_ACTIVE")
        before = set((await db.scalars(select(ProjectMember.user_id).where(ProjectMember.project_id == project_id))).all())
        removed = before - members
        if removed:
            dependent = await db.scalar(select(Task.id).where(
                Task.project_id == project_id,
                Task.status.not_in(("DONE", "CANCELLED")),
                (Task.assignee_id.in_(removed) | Task.qa_reviewer_id.in_(removed)),
            ).limit(1))
            if dependent is not None:
                raise HTTPException(409, "MEMBER_HAS_ACTIVE_TASK")
        await db.execute(delete(ProjectMember).where(ProjectMember.project_id == project_id,
                                                    ProjectMember.user_id.in_(removed)))
        for member_id in members - before:
            db.add(ProjectMember(project_id=project_id, user_id=member_id))
        row.version += 1
        audit(db, actor, "project.members_changed", "project", str(row.id),
              {"added": sorted(map(str, members - before)), "removed": sorted(map(str, before - members))})
        return 200, {"project": project_data(row), "members": sorted(map(str, members))}

    return await mutation(request, db, actor, body, f"members:{project_id}", perform)
