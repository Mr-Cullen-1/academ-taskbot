import asyncio
import hashlib
import hmac
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import httpx
from aiogram import Bot
from aiogram.types import Chat, Message, Update, User as TelegramUser
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from taskbot_api.config import Settings
from taskbot_api.db import Base, session_factory
from taskbot_api.main import create_app
from taskbot_api.models import AuditLog, OutboxEvent, Project, ProjectMember, Task, TaskComment, TaskEvent, TaskKeySequence, User, UserRole
from taskbot_api.task_service import create_task, is_overdue
from taskbot_bot.__main__ import build_dispatcher


ORIGIN = "http://localhost:3000"
TOKEN = "123456:test-token-only"


async def fixture(*, enforce_foreign_keys=False):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    if enforce_foreign_keys:
        @event.listens_for(engine.sync_engine, "connect")
        def enable_foreign_keys(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as db:
        async with db.begin():
            users = {}
            for number, role, status in (
                (701, "SUPER_ADMIN", "ACTIVE"), (702, "PM", "ACTIVE"),
                (703, "SUBJECT_DEVELOPER", "ACTIVE"), (704, "SUBJECT_DEVELOPER", "ACTIVE"),
                (705, "QA", "ACTIVE"), (706, "QA", "ACTIVE"), (708, "QA", "ACTIVE"),
                (707, "SUBJECT_DEVELOPER", "BLOCKED"),
            ):
                user = User(telegram_user_id=number, telegram_chat_id=number, phone=f"+998{number}",
                            name=f"Person {number}", status=status)
                db.add(user)
                await db.flush()
                db.add(UserRole(user_id=user.id, code=role,
                                super_admin_slot=1 if role == "SUPER_ADMIN" else None))
                users[number] = user.id
            project = Project(name="Project A", owner_user_id=users[702], description="")
            other = Project(name="Project B", owner_user_id=users[701], description="")
            db.add_all([project, other])
            await db.flush()
            for number in (702, 703, 704, 705, 707, 708):
                db.add(ProjectMember(project_id=project.id, user_id=users[number]))
            db.add(ProjectMember(project_id=other.id, user_id=users[701]))
            db.add(TaskKeySequence(id=1, next_number=1001))
    app = create_app(Settings(_env_file=None, database_url=None, bot_token=TOKEN,
                              web_origin=ORIGIN, session_cookie_secure=False), engine_override=engine)
    return engine, app, factory, users, project.id, other.id


async def login(client, number):
    await client.get("/api/v1/auth/telegram/challenge", headers={"Origin": ORIGIN})
    data = {"id": number, "auth_date": int(time.time()), "first_name": "Test"}
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    data["hash"] = hmac.new(hashlib.sha256(TOKEN.encode()).digest(), check.encode(), hashlib.sha256).hexdigest()
    response = await client.post("/api/v1/auth/telegram", json=data, headers={"Origin": ORIGIN})
    assert response.status_code == 200
    return response.json()["csrf_token"]


def headers(csrf, key=None):
    return {"X-CSRF-Token": csrf, "Idempotency-Key": key or str(uuid4())}


async def new_task(client, csrf, project_id, assignee_id, qa_id=None):
    response = await client.post("/api/v1/tasks", headers=headers(csrf), json={
        "project_id": str(project_id), "title": "Build lesson", "description": "Work",
        "assignee_id": str(assignee_id), "qa_reviewer_id": str(qa_id) if qa_id else None,
        "priority": "HIGH", "type": "DEVELOPMENT", "due_date": "2027-01-01",
    })
    assert response.status_code == 201, response.text
    return response.json()


async def act(client, csrf, task, action, **kwargs):
    return await client.post(f"/api/v1/tasks/{task['id']}/{action}", headers=headers(csrf),
                             json={"expected_version": task["version"], **kwargs})


def test_task_creation_access_validation_and_edit():
    async def scenario():
        engine, app, factory, users, project, other = await fixture()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as author, \
                       httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as pm, \
                       httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as outsider, \
                       httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as admin:
                author_csrf = await login(author, 703)
                pm_csrf = await login(pm, 702)
                outsider_csrf = await login(outsider, 706)
                admin_csrf = await login(admin, 701)
                task = await new_task(author, author_csrf, project, users[704], users[705])
                assert task["task_key"] == "TASK-1001"
                assert task["status"] == "OPEN" and task["qa_review_status"] == "PENDING"
                assert task["created_by"] == str(users[703]) and task["assignee_id"] == str(users[704])
                assert task["original_due_date"] == "2027-01-01"
                assert (await author.post("/api/v1/tasks", headers=headers(author_csrf),
                    json={"project_id": str(project), "title": "Missing due date"})).status_code == 422
                assert (await outsider.get(f"/api/v1/tasks/{task['id']}")).status_code == 404
                assert (await outsider.get("/api/v1/tasks")).json() == []
                assert (await outsider.post("/api/v1/tasks", headers=headers(outsider_csrf),
                    json={"project_id": str(project), "title": "Foreign", "due_date": "2027-01-01"})).status_code == 404
                assert (await author.post("/api/v1/tasks", headers=headers(author_csrf),
                    json={"project_id": str(project), "title": "Invalid", "due_date": "2027-01-01", "assignee_id": str(users[706])})).status_code == 422
                assert (await author.post("/api/v1/tasks", headers=headers(author_csrf),
                    json={"project_id": str(project), "title": "Invalid", "due_date": "2027-01-01", "qa_reviewer_id": str(users[704])})).status_code == 422
                assert (await author.post("/api/v1/tasks", headers=headers(author_csrf),
                    json={"project_id": str(project), "title": "Invalid", "due_date": "2027-01-01", "assignee_id": str(users[705]),
                          "qa_reviewer_id": str(users[705])})).status_code == 422
                assert (await author.post("/api/v1/tasks", headers=headers(author_csrf),
                    json={"project_id": str(project), "title": "Invalid", "due_date": "2027-01-01", "assignee_id": str(users[707])})).status_code == 422
                assert (await author.patch(f"/api/v1/tasks/{task['id']}", headers=headers(author_csrf),
                    json={"expected_version": 1, "title": "Edited"})).status_code == 403
                assert (await pm.patch(f"/api/v1/tasks/{task['id']}", headers=headers(pm_csrf),
                    json={"expected_version": 1, "status": "DONE"})).status_code == 422
                assert (await pm.patch(f"/api/v1/tasks/{task['id']}", headers=headers(pm_csrf),
                    json={"expected_version": 1, "created_by": str(users[702])})).status_code == 422
                edited = await pm.patch(f"/api/v1/tasks/{task['id']}", headers=headers(pm_csrf),
                    json={"expected_version": 1, "title": "Edited"})
                assert edited.status_code == 200 and edited.json()["created_by"] == str(users[703])
                assert len((await author.get(f"/api/v1/tasks/{task['id']}/events")).json()) == 2
                async with factory() as db:
                    assert len((await db.scalars(select(OutboxEvent).where(OutboxEvent.aggregate_id == task["id"]))).all()) == 2
                assert len((await author.get("/api/v1/tasks", params={"project_id": str(project), "priority": "HIGH", "assignee_id": str(users[704])})).json()) == 1
                assert (await outsider.get(f"/api/v1/tasks/{task['id']}/events")).status_code == 404
                removal = await admin.put(f"/api/v1/projects/{project}/members", headers=headers(admin_csrf),
                    json={"expected_version": 1,
                          "members": [str(users[n]) for n in (702, 703, 705, 708)]})
                assert removal.status_code == 409
                assert (await pm.put(f"/api/v1/tasks/{task['id']}/qa-reviewer", headers=headers(pm_csrf),
                    json={"expected_version": 2, "qa_reviewer_id": str(users[708])})).status_code == 403
                changed_qa = await author.put(f"/api/v1/tasks/{task['id']}/qa-reviewer", headers=headers(author_csrf),
                    json={"expected_version": 2, "qa_reviewer_id": str(users[708])})
                assert changed_qa.status_code == 200 and changed_qa.json()["qa_reviewer_id"] == str(users[708])
                assert changed_qa.json()["qa_review_status"] == "PENDING"
                unassigned = await author.post("/api/v1/tasks", headers=headers(author_csrf),
                    json={"project_id": str(project), "title": "Unassigned", "due_date": "2027-01-01"})
                assert unassigned.status_code == 201 and unassigned.json()["assignee_id"] is None
                assert (await pm.patch(f"/api/v1/tasks/{unassigned.json()['id']}", headers=headers(pm_csrf),
                    json={"expected_version": 1, "assignee_id": str(users[706])})).status_code == 422
                assigned = await pm.patch(f"/api/v1/tasks/{unassigned.json()['id']}", headers=headers(pm_csrf),
                    json={"expected_version": 1, "assignee_id": str(users[704])})
                assert assigned.status_code == 200 and assigned.json()["status"] == "OPEN"
                assert assigned.json()["created_by"] == str(users[703])
        await engine.dispose()
    asyncio.run(scenario())


def test_workflow_qa_rework_repeat_and_author_approval():
    async def scenario():
        engine, app, factory, users, project, _ = await fixture()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as author, \
                       httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as worker, \
                       httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as qa, \
                       httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as other_qa:
                a, w, q, q2 = await login(author, 703), await login(worker, 704), await login(qa, 705), await login(other_qa, 708)
                task = await new_task(author, a, project, users[704], users[705])
                assert (await act(author, a, task, "accept")).status_code == 403
                task = (await act(worker, w, task, "accept")).json()
                assert task["status"] == "ACCEPTED"
                task = (await act(worker, w, task, "start")).json()
                assert (await act(worker, w, task, "block")).status_code == 422
                assert (await worker.get(f"/api/v1/tasks/{task['id']}")).json()["version"] == task["version"]
                async with factory() as db:
                    assert len((await db.scalars(select(OutboxEvent).where(OutboxEvent.aggregate_id == task["id"]))).all()) == task["version"]
                task = (await act(worker, w, task, "block", reason="Waiting for files")).json()
                assert task["status"] == "BLOCKED"
                task = (await act(worker, w, task, "resume")).json()
                task = (await act(worker, w, task, "submit-review", result="Lesson ready")).json()
                assert task["status"] == "REVIEW" and task["submitted_at"]
                assert (await act(other_qa, q2, task, "qa-review/done")).status_code == 403
                assert (await act(qa, q, task, "request-rework")).status_code == 422
                task = (await act(qa, q, task, "request-rework", comment="Fix examples")).json()
                assert task["status"] == "REWORK" and task["qa_review_status"] == "REWORK"
                assert len((await author.get(f"/api/v1/tasks/{task['id']}/comments")).json()) == 1
                task = (await act(worker, w, task, "start")).json()
                task = (await act(worker, w, task, "submit-review")).json()
                assert task["qa_review_status"] == "PENDING"
                task = (await act(qa, q, task, "qa-review/done")).json()
                assert task["status"] == "REVIEW" and task["qa_review_status"] == "DONE"
                assert task["completed_at"] is None
                assert (await act(qa, q, task, "approve")).status_code == 403
                task = (await act(author, a, task, "approve")).json()
                assert task["status"] == "DONE" and task["completed_at"]
                assert task["created_by"] == str(users[703])
                assert (await act(qa, q, task, "qa-review/done")).status_code == 409
                events = (await author.get(f"/api/v1/tasks/{task['id']}/events")).json()
                assert [e["sequence"] for e in events] == list(range(1, task["version"] + 1))
                assert events[-1]["approval_mode"] == "AUTHOR"
                assert any(e["type"] == "QA_REVIEW_DONE" for e in events)
                assert [e["after_data"]["qa_review_status"] for e in
                        (await author.get(f"/api/v1/tasks/{task['id']}/qa-reviews")).json()] == ["REWORK", "DONE"]
        await engine.dispose()
    asyncio.run(scenario())


def test_task_create_flushes_event_before_outbox_with_foreign_keys():
    async def scenario():
        engine, app, factory, users, project, _ = await fixture(enforce_foreign_keys=True)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as admin, \
                       httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as worker, \
                       httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as qa:
                csrf = await login(admin, 701)
                worker_csrf = await login(worker, 703)
                qa_csrf = await login(qa, 705)
                created = await new_task(admin, csrf, project, users[703], users[705])
                assert created["status"] == "OPEN"
                listed = await admin.get("/api/v1/tasks")
                assert listed.status_code == 200
                assert [row["id"] for row in listed.json()] == [created["id"]]
                assert listed.json()[0]["project_id"] == str(project)
                assert listed.json()[0]["assignee_id"] == str(users[703])
                assert listed.json()[0]["qa_reviewer_id"] == str(users[705])
                assert listed.json()[0]["status"] == "OPEN"
                assert "accept" not in listed.json()[0]["available_actions"]
                worker_list = await worker.get("/api/v1/tasks", params={"project_id": str(project)})
                assert worker_list.status_code == 200
                assert "accept" in worker_list.json()[0]["available_actions"]
                detail = await admin.get(f"/api/v1/tasks/{created['id']}")
                assert detail.status_code == 200
                assert detail.json()["qa_review_status"] == "PENDING"
                current = created
                for client, token, action in ((worker, worker_csrf, "accept"),
                                              (worker, worker_csrf, "start"),
                                              (worker, worker_csrf, "submit-review"),
                                              (qa, qa_csrf, "qa-review/done"),
                                              (admin, csrf, "approve")):
                    response = await act(client, token, current, action)
                    assert response.status_code == 200, response.text
                    current = response.json()
                assert current["status"] == "DONE"
                assert current["qa_review_status"] == "DONE"
                assert (await admin.get("/api/v1/tasks", params={"status": "DONE"})).json()[0]["id"] == created["id"]
                async with factory() as db:
                    events = (await db.scalars(select(TaskEvent).where(TaskEvent.task_id == UUID(created["id"])))).all()
                    outbox = (await db.scalars(select(OutboxEvent).where(OutboxEvent.aggregate_id == created["id"]))).all()
                    assert len(events) == len(outbox) == 6
                    assert {row.source_event_id for row in outbox} == {row.id for row in events}
        await engine.dispose()
    asyncio.run(scenario())


def test_author_early_approval_admin_override_rejection_and_cancel():
    async def scenario():
        engine, app, factory, users, project, _ = await fixture()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as author, \
                       httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as worker, \
                       httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as admin:
                a, w, s = await login(author, 703), await login(worker, 704), await login(admin, 701)
                early = await new_task(author, a, project, users[704], users[705])
                early = (await act(worker, w, early, "accept")).json()
                early = (await act(worker, w, early, "start")).json()
                early = (await act(worker, w, early, "submit-review")).json()
                early = (await act(author, a, early, "approve")).json()
                assert early["status"] == "DONE" and early["qa_review_status"] == "PENDING"
                author_rework = await new_task(author, a, project, users[704])
                author_rework = (await act(worker, w, author_rework, "accept")).json()
                author_rework = (await act(worker, w, author_rework, "start")).json()
                author_rework = (await act(worker, w, author_rework, "submit-review")).json()
                assert (await act(author, a, author_rework, "request-rework")).status_code == 422
                author_rework = (await act(author, a, author_rework, "request-rework", comment="Fix title")).json()
                assert author_rework["status"] == "REWORK"
                task = await new_task(author, a, project, users[704])
                task = (await act(worker, w, task, "accept")).json()
                task = (await act(worker, w, task, "start")).json()
                task = (await act(worker, w, task, "submit-review")).json()
                assert (await act(admin, s, task, "admin-approve", reason="Author away")).status_code == 422
                assert (await act(admin, s, task, "admin-approve", author_unavailable_confirmed=True)).status_code == 422
                final = (await act(admin, s, task, "admin-approve", author_unavailable_confirmed=True,
                                   reason="Author unavailable, confirmed by administrator")).json()
                assert final["status"] == "DONE" and final["created_by"] == str(users[703])
                async with factory() as db:
                    event = await db.scalar(select(TaskEvent).where(TaskEvent.task_id == UUID(task["id"]))
                                            .order_by(TaskEvent.sequence.desc()))
                    assert event.approval_mode == "ADMIN_OVERRIDE"
                    assert event.original_author_user_id == users[703]
                    assert event.author_unavailable_confirmed is True
                    assert (await db.scalars(select(AuditLog).where(AuditLog.action == "task.admin_approved"))).all()
                rejected = await new_task(author, a, project, users[704])
                assert (await act(worker, w, rejected, "reject")).status_code == 422
                rejected = (await act(worker, w, rejected, "reject", reason="No capacity")).json()
                assert rejected["status"] == "REJECTED"
                assert (await act(author, a, rejected, "cancel", reason="Cancel")).status_code == 200
                cancelled = await new_task(author, a, project, users[704])
                assert (await act(author, a, cancelled, "cancel")).status_code == 422
                cancelled = (await act(author, a, cancelled, "cancel", reason="No longer needed")).json()
                assert cancelled["status"] == "CANCELLED"
        await engine.dispose()
    asyncio.run(scenario())


def test_idempotent_transition_has_single_event_and_comments():
    async def scenario():
        engine, app, factory, users, project, _ = await fixture()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as author, \
                       httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as worker:
                a, w = await login(author, 703), await login(worker, 704)
                task = await new_task(author, a, project, users[704])
                key = str(uuid4())
                first = await worker.post(f"/api/v1/tasks/{task['id']}/accept", headers=headers(w, key),
                                          json={"expected_version": 1})
                second = await worker.post(f"/api/v1/tasks/{task['id']}/accept", headers=headers(w, key),
                                           json={"expected_version": 1})
                assert first.status_code == 200 and first.json() == second.json()
                assert len((await author.get(f"/api/v1/tasks/{task['id']}/events")).json()) == 2
                async with factory() as db:
                    assert len((await db.scalars(select(OutboxEvent).where(OutboxEvent.aggregate_id == task["id"]))).all()) == 2
                commented = await author.post(f"/api/v1/tasks/{task['id']}/comments", headers=headers(a),
                                              json={"body": "Please check the source"})
                assert commented.status_code == 201
                assert len((await author.get(f"/api/v1/tasks/{task['id']}/comments")).json()) == 1
                assert (await author.get(f"/api/v1/tasks/{task['id']}")).json()["created_by"] == str(users[703])
        await engine.dispose()
    asyncio.run(scenario())


def test_bot_lists_details_and_changes_task_without_sending_messages():
    async def scenario():
        engine, app, factory, users, project, _ = await fixture()
        async with factory() as db:
            async with db.begin():
                author = await db.get(User, users[703])
                task = await create_task(db, author, project_id=project, title="Bot work",
                                         description="Description", assignee_id=users[704],
                                         qa_reviewer_id=None, priority="MEDIUM",
                                         type="DEVELOPMENT", due_date=date(2027, 1, 1))
        dispatcher = build_dispatcher()
        bot = Bot(token=TOKEN)
        sender = TelegramUser(id=704, is_bot=False, first_name="Worker")
        chat = Chat(id=704, type="private")
        def update(update_id, text):
            message = Message(message_id=update_id, date=datetime.now(timezone.utc),
                              chat=chat, from_user=sender, text=text)
            return Update(update_id=update_id, message=message)
        try:
            with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
                await dispatcher.feed_update(bot, update(8001, "/my_tasks"), registration_factory=factory)
                await dispatcher.feed_update(bot, update(8002, "/task TASK-1001"), registration_factory=factory)
                await dispatcher.feed_update(bot, update(8003, "/accept TASK-1001"), registration_factory=factory)
                await dispatcher.feed_update(bot, update(8003, "/accept TASK-1001"), registration_factory=factory)
                await dispatcher.feed_update(bot, update(8004, "/comment TASK-1001 Work started"), registration_factory=factory)
                await dispatcher.feed_update(bot, update(8005, "/comments TASK-1001"), registration_factory=factory)
                await dispatcher.feed_update(bot, update(8006, "/history TASK-1001"), registration_factory=factory)
                assert answer.await_count == 6
            async with factory() as db:
                current = await db.get(Task, task.id)
                assert current.status == "ACCEPTED"
                assert len((await db.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id))).all()) == 3
                assert len((await db.scalars(select(TaskComment).where(TaskComment.task_id == task.id))).all()) == 1
        finally:
            await bot.session.close()
            await engine.dispose()
    asyncio.run(scenario())


def test_calendar_date_overdue_boundary_and_status_exclusions():
    today = datetime.now(ZoneInfo("Asia/Tashkent")).date()
    task = Task(current_due_date=today, status="OPEN")
    assert is_overdue(task, today=today) is False
    task.current_due_date = today - timedelta(days=1)
    for status in ("OPEN", "ACCEPTED", "IN_PROGRESS", "BLOCKED", "REWORK"):
        task.status = status
        assert is_overdue(task, today=today) is True
    for status in ("REVIEW", "REJECTED", "DONE", "CANCELLED"):
        task.status = status
        assert is_overdue(task, today=today) is False


def test_active_task_author_cannot_lose_project_access_before_approval():
    async def scenario():
        engine, app, factory, users, project, _ = await fixture()
        try:
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as author, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as admin:
                    author_csrf = await login(author, 703)
                    admin_csrf = await login(admin, 701)
                    task = await new_task(author, author_csrf, project, users[704])
                    response = await admin.put(f"/api/v1/projects/{project}/members",
                                               headers=headers(admin_csrf),
                                               json={"expected_version": 1, "members": [
                                                   str(users[number]) for number in (702, 704, 705, 708)]})
                    assert response.status_code == 409
                    assert (await author.get(f"/api/v1/tasks/{task['id']}")).status_code == 200
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_direct_api_enforces_project_and_task_actor_boundaries():
    async def scenario():
        engine, app, factory, users, project, _ = await fixture()
        try:
            async with factory() as db:
                async with db.begin():
                    db.add(UserRole(user_id=users[708], code="TEAM_LEAD"))
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as author, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as worker, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as qa, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as lead, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as outsider, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as anonymous:
                    a, w, q, l, o = [await login(client, number) for client, number in
                                     ((author, 703), (worker, 704), (qa, 705), (lead, 708), (outsider, 706))]
                    task = await new_task(author, a, project, users[704], users[705])
                    path = f"/api/v1/tasks/{task['id']}"
                    assert (await anonymous.get(path)).status_code == 401
                    assert (await anonymous.post(f"{path}/cancel", json={"expected_version": 1,
                        "reason": "Forged"})).status_code == 401
                    assert (await outsider.get(path)).status_code == 404
                    assert (await outsider.get(f"{path}/comments")).status_code == 404
                    assert (await outsider.get(f"{path}/events")).status_code == 404
                    assert (await outsider.patch(path, headers=headers(o),
                        json={"expected_version": 1, "title": "Forged"})).status_code == 404
                    assert (await outsider.post(f"{path}/cancel", headers=headers(o),
                        json={"expected_version": 1, "reason": "Forged"})).status_code == 404
                    assert (await author.post("/api/v1/tasks", headers=headers(a), json={
                        "project_id": str(project), "title": "Forged", "due_date": "2027-01-01",
                        "created_by": str(users[705])})).status_code == 422
                    assert (await lead.get(path)).status_code == 200
                    assert (await lead.patch(path, headers=headers(l),
                        json={"expected_version": 1, "title": "Forbidden"})).status_code == 403
                    assert (await lead.post(f"{path}/cancel", headers=headers(l),
                        json={"expected_version": 1, "reason": "Forbidden"})).status_code == 403
                    assert (await qa.post(f"{path}/accept", headers=headers(q),
                        json={"expected_version": 1})).status_code == 403
                    assert (await worker.post(f"{path}/reject", headers=headers(w),
                        json={"expected_version": 1, "reason": "  "})).status_code == 422
                    comment = await worker.post(f"{path}/comments", headers=headers(w), json={
                        "body": "I am working on it", "author_user_id": str(users[703])})
                    assert comment.status_code == 201
                    assert comment.json()["author_user_id"] == str(users[704])
        finally:
            await engine.dispose()
    asyncio.run(scenario())
