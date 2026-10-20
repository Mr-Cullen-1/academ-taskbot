import asyncio
from datetime import date, datetime
from uuid import UUID

import httpx
from sqlalchemy import select

from taskbot_api.models import DeadlineChange, Notification, TaskEvent
from taskbot_worker.delivery import process_outbox
from taskbot_worker.delivery import deliver_notifications
from taskbot_worker.scheduler import schedule_deadlines
from taskbot_api.reporting import TASHKENT
from test_phase4 import FakeTransport, settings
from test_tasks import fixture, headers, login, new_task


def test_deadline_request_approval_history_and_outbox():
    async def scenario():
        engine, app, factory, users, project, other = await fixture(enforce_foreign_keys=True)
        try:
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as author, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as assignee, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as outsider:
                    author_csrf = await login(author, 703)
                    assignee_csrf = await login(assignee, 704)
                    outsider_csrf = await login(outsider, 706)
                    task = await new_task(author, author_csrf, project, users[704])
                    payload = {"proposed_due_date": "2027-01-10", "reason": "More material",
                               "progress_note": "Half done", "expected_version": task["version"]}
                    denied = await outsider.post(f"/api/v1/tasks/{task['id']}/deadline-requests",
                                                 headers=headers(outsider_csrf), json=payload)
                    assert denied.status_code == 404
                    requested = await assignee.post(f"/api/v1/tasks/{task['id']}/deadline-requests",
                                                    headers=headers(assignee_csrf), json=payload)
                    assert requested.status_code == 201, requested.text
                    row = requested.json()
                    assert (await author.get(f"/api/v1/tasks/{task['id']}")).json()["current_due_date"] == "2027-01-01"
                    second = await assignee.post(f"/api/v1/tasks/{task['id']}/deadline-requests",
                                                 headers=headers(assignee_csrf),
                                                 json={**payload, "expected_version": task["version"] + 1})
                    assert second.status_code == 409
                    old_due_reminder = datetime(2027, 1, 1, 0, tzinfo=TASHKENT)
                    assert await schedule_deadlines(factory, at=old_due_reminder) == 6
                    forbidden = await outsider.post(f"/api/v1/deadline-requests/{row['id']}/approve",
                                                    headers=headers(outsider_csrf),
                                                    json={"expected_version": 1})
                    assert forbidden.status_code == 404
                    approved = await author.post(f"/api/v1/deadline-requests/{row['id']}/approve",
                                                 headers=headers(author_csrf),
                                                 json={"expected_version": 1})
                    assert approved.status_code == 200, approved.text
                    assert approved.json()["status"] == "APPROVED"
                    latest = (await author.get(f"/api/v1/tasks/{task['id']}")).json()
                    assert latest["original_due_date"] == "2027-01-01"
                    assert latest["current_due_date"] == "2027-01-10"
                    await schedule_deadlines(factory, at=old_due_reminder)
                    async with factory() as db:
                        change = await db.scalar(select(DeadlineChange).where(
                            DeadlineChange.request_id == UUID(row["id"])))
                        assert change.previous_due_date == date(2027, 1, 1)
                        assert change.new_due_date == date(2027, 1, 10)
                        event_types = (await db.scalars(select(TaskEvent.type).where(
                            TaskEvent.task_id == UUID(task["id"])).order_by(TaskEvent.sequence))).all()
                        assert event_types == ["CREATED", "DEADLINE_REQUESTED", "DEADLINE_APPROVED"]
                    assert await process_outbox(factory) == 3
                    fake = FakeTransport()
                    await deliver_notifications(factory, fake, settings(), at=old_due_reminder)
                    async with factory() as db:
                        stale = (await db.scalars(select(Notification).where(
                            Notification.kind == "DUE_24H",
                            Notification.due_date_snapshot == date(2027, 1, 1)))).all()
                        assert len(stale) == 2
                        assert all(row.state == "SKIPPED" for row in stale)
                    again = await author.post(f"/api/v1/deadline-requests/{row['id']}/approve",
                                              headers=headers(author_csrf), json={"expected_version": 1})
                    assert again.status_code == 409
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_watchers_and_dependencies_obey_project_and_role():
    async def scenario():
        engine, app, factory, users, project, other = await fixture(enforce_foreign_keys=True)
        try:
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as author, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as pm, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as outsider:
                    author_csrf = await login(author, 703)
                    pm_csrf = await login(pm, 702)
                    outsider_csrf = await login(outsider, 706)
                    first = await new_task(author, author_csrf, project, users[704])
                    second = await new_task(author, author_csrf, project, users[704])
                    task_id = first["id"]
                    forbidden = await outsider.post(f"/api/v1/tasks/{task_id}/watchers",
                                                    headers=headers(outsider_csrf),
                                                    json={"user_id": str(users[706])})
                    assert forbidden.status_code == 404
                    watched = await author.post(f"/api/v1/tasks/{task_id}/watchers",
                                                headers=headers(author_csrf),
                                                json={"user_id": str(users[703])})
                    assert watched.status_code == 201
                    assert len((await author.get(f"/api/v1/tasks/{task_id}/watchers")).json()) == 1
                    self_link = await pm.post(f"/api/v1/tasks/{task_id}/dependencies",
                                              headers=headers(pm_csrf),
                                              json={"depends_on_task_id": task_id})
                    assert self_link.status_code == 422
                    linked = await pm.post(f"/api/v1/tasks/{task_id}/dependencies",
                                           headers=headers(pm_csrf),
                                           json={"depends_on_task_id": second["id"]})
                    assert linked.status_code == 201, linked.text
                    cyclic = await pm.post(f"/api/v1/tasks/{second['id']}/dependencies",
                                           headers=headers(pm_csrf),
                                           json={"depends_on_task_id": task_id})
                    assert cyclic.status_code == 422
                    non_pm = await author.post(f"/api/v1/tasks/{second['id']}/dependencies",
                                               headers=headers(author_csrf),
                                               json={"depends_on_task_id": task_id})
                    assert non_pm.status_code == 403
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_deadline_request_cannot_change_new_assignees_deadline():
    async def scenario():
        engine, app, factory, users, project, other = await fixture(enforce_foreign_keys=True)
        try:
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as author, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as assignee, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as pm:
                    author_csrf = await login(author, 703)
                    assignee_csrf = await login(assignee, 704)
                    pm_csrf = await login(pm, 702)
                    task = await new_task(author, author_csrf, project, users[704])
                    requested = await assignee.post(f"/api/v1/tasks/{task['id']}/deadline-requests",
                        headers=headers(assignee_csrf), json={"proposed_due_date": "2027-01-10",
                            "reason": "Need time", "progress_note": "Started", "expected_version": task["version"]})
                    assert requested.status_code == 201
                    latest = (await pm.get(f"/api/v1/tasks/{task['id']}")).json()
                    reassigned = await pm.patch(f"/api/v1/tasks/{task['id']}", headers=headers(pm_csrf),
                        json={"assignee_id": str(users[708]), "expected_version": latest["version"]})
                    assert reassigned.status_code == 200, reassigned.text
                    decision = await author.post(
                        f"/api/v1/deadline-requests/{requested.json()['id']}/approve",
                        headers=headers(author_csrf), json={"expected_version": 1})
                    assert decision.status_code == 409
                    after = (await author.get(f"/api/v1/tasks/{task['id']}")).json()
                    assert after["current_due_date"] == "2027-01-01"
        finally:
            await engine.dispose()

    asyncio.run(scenario())
