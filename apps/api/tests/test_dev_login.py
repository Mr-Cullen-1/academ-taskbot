"""Local login uses disposable SQLite and never contacts the real database."""

import asyncio
from datetime import date, timedelta
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from taskbot_api.config import Settings
from taskbot_api.db import Base, session_factory
from taskbot_api.main import create_app
from taskbot_api.models import AuditLog, TaskKeySequence, User, UserRole, WebSession


ORIGIN = "http://localhost:3000"
SECRET = "local-test-secret-with-at-least-32-characters"
ADMIN_ID = 9001


def settings(**overrides):
    values = dict(database_url=None, web_origin=ORIGIN, session_cookie_secure=False,
                  app_env="development", dev_login_enabled=True,
                  dev_login_telegram_id=ADMIN_ID, dev_login_secret=SecretStr(SECRET))
    values.update(overrides)
    return Settings(_env_file=None, **values)


async def setup(role="SUPER_ADMIN", status="ACTIVE"):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as db:
        async with db.begin():
            user = User(telegram_user_id=ADMIN_ID, telegram_chat_id=ADMIN_ID,
                        phone="+998900000001", name="Local Admin", status=status)
            db.add(user)
            await db.flush()
            db.add(UserRole(user_id=user.id, code=role,
                            super_admin_slot=1 if role == "SUPER_ADMIN" else None))
            db.add(TaskKeySequence(id=1, next_number=1001))
    return engine, factory


def test_dev_login_issues_normal_session_and_respects_logout():
    async def scenario():
        engine, factory = await setup()
        app = create_app(settings(), engine_override=engine)
        try:
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 10001))
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
                    assert (await client.get("/api/v1/users/me")).status_code == 401
                    response = await client.post("/api/v1/auth/dev", json={"secret": SECRET},
                                                 headers={"Origin": ORIGIN})
                    assert response.status_code == 200
                    assert response.json()["user"]["roles"] == ["SUPER_ADMIN"]
                    assert SECRET not in response.text
                    assert "httponly" in response.headers["set-cookie"].lower()
                    csrf = response.json()["csrf_token"]
                    assert (await client.get("/api/v1/users/me")).json()["telegram_user_id"] == ADMIN_ID
                    assert (await client.get("/api/v1/users")).status_code == 200
                    assert (await client.post("/api/v1/projects", json={"name": "No CSRF"},
                        headers={"Idempotency-Key": str(uuid4())})).status_code == 403
                    assert (await client.post("/api/v1/auth/logout",
                                              headers={"X-CSRF-Token": csrf})).status_code == 204
                    assert (await client.get("/api/v1/users/me")).status_code == 401
                async with factory() as db:
                    assert len((await db.scalars(select(WebSession))).all()) == 1
                    assert (await db.scalar(select(AuditLog).where(
                        AuditLog.action == "auth.dev_login"))) is not None
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_super_admin_can_create_project_then_create_task_in_it():
    async def scenario():
        engine, _ = await setup()
        app = create_app(settings(), engine_override=engine)
        try:
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                            base_url="http://localhost:8000") as client:
                    signed = await client.post("/api/v1/auth/dev", json={"secret": SECRET},
                                               headers={"Origin": ORIGIN})
                    csrf = signed.json()["csrf_token"]
                    assert (await client.get("/api/v1/projects")).json() == []
                    project = await client.post("/api/v1/projects", json={
                        "name": "Учебный проект", "description": "Подготовка уроков",
                    }, headers={"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid4())})
                    assert project.status_code == 201
                    created = project.json()
                    assert created["name"] == "Учебный проект"
                    assert created["description"] == "Подготовка уроков"
                    assert [row["id"] for row in (await client.get("/api/v1/projects")).json()] == [created["id"]]
                    task = await client.post("/api/v1/tasks", json={
                        "project_id": created["id"], "title": "Первый урок",
                        "description": "Проверка связки",
                        "due_date": (date.today() + timedelta(days=30)).isoformat(),
                    }, headers={"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid4())})
                    assert task.status_code == 201, task.text
                    assert task.json()["project_id"] == created["id"]
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_dev_login_rejects_wrong_secret_origin_remote_proxy_and_client_id():
    async def scenario():
        engine, factory = await setup()
        app = create_app(settings(), engine_override=engine)
        try:
            async with app.router.lifespan_context(app):
                local = httpx.ASGITransport(app=app, client=("127.0.0.1", 10001))
                remote = httpx.ASGITransport(app=app, client=("203.0.113.10", 10001))
                async with httpx.AsyncClient(transport=local, base_url="http://localhost:8000") as client:
                    path = "/api/v1/auth/dev"
                    assert (await client.post(path, json={"secret": "wrong"},
                                              headers={"Origin": ORIGIN})).status_code == 403
                    assert (await client.post(path, json={"secret": SECRET},
                                              headers={"Origin": "https://example.com"})).status_code == 403
                    assert (await client.post(path, json={"secret": SECRET},
                                              headers={"Origin": ORIGIN, "Host": "taskbot.example"})).status_code == 403
                    assert (await client.post(path, json={"secret": SECRET},
                                              headers={"Origin": ORIGIN, "X-Forwarded-For": "127.0.0.1"})).status_code == 403
                    assert (await client.post(path, json={"secret": SECRET, "telegram_id": ADMIN_ID},
                                              headers={"Origin": ORIGIN})).status_code == 422
                async with httpx.AsyncClient(transport=remote, base_url="http://localhost:8000") as client:
                    assert (await client.post("/api/v1/auth/dev", json={"secret": SECRET},
                                              headers={"Origin": ORIGIN})).status_code == 403
                async with factory() as db:
                    assert (await db.scalars(select(WebSession))).all() == []
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("role,status", [("PM", "ACTIVE"), ("SUPER_ADMIN", "PENDING"),
                                           ("SUPER_ADMIN", "BLOCKED")])
def test_dev_login_requires_existing_active_super_admin(role, status):
    async def scenario():
        engine, _ = await setup(role, status)
        app = create_app(settings(), engine_override=engine)
        try:
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                            base_url="http://localhost:8000") as client:
                    response = await client.post("/api/v1/auth/dev", json={"secret": SECRET},
                                                 headers={"Origin": ORIGIN})
                    assert response.status_code == 403
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_dev_route_absent_by_default_and_production_configuration_rejected():
    async def scenario():
        engine, _ = await setup()
        app = create_app(Settings(_env_file=None, database_url=None, web_origin=ORIGIN,
                                  session_cookie_secure=False), engine_override=engine)
        try:
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                            base_url="http://localhost:8000") as client:
                    assert (await client.post("/api/v1/auth/dev", json={"secret": SECRET},
                                              headers={"Origin": ORIGIN})).status_code == 404
                    assert "/api/v1/auth/dev" not in (await client.get("/openapi.json")).json()["paths"]
        finally:
            await engine.dispose()
    asyncio.run(scenario())
    for changes in ({"app_env": "production"},
                    {"web_origin": "https://taskbot.example"},
                    {"session_cookie_secure": True},
                    {"dev_login_secret": SecretStr("too-short")},
                    {"dev_login_telegram_id": None}):
        with pytest.raises(ValidationError, match="dev login requires"):
            settings(**changes)
