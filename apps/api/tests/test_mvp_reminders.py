import asyncio
from datetime import timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select

from taskbot_api import notification_routes
from taskbot_api.models import ApiIdempotency, Notification, PersonalReminder, ProjectMember, User, now
from taskbot_api.notification_routes import my_notifications
from taskbot_worker.delivery import deliver_notifications
from test_tasks import fixture, headers, login, new_task
from test_phase4 import FakeTransport, settings


def test_personal_reminders_and_notification_privacy():
    async def scenario():
        engine, app, factory, users, project, other = await fixture()
        try:
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as owner, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as stranger:
                    owner_csrf = await login(owner, 703)
                    stranger_csrf = await login(stranger, 706)
                    due = now() + timedelta(minutes=5)
                    created = await owner.post("/api/v1/reminders", headers=headers(owner_csrf),
                                               json={"remind_at": due.isoformat(), "text": "Check notes"})
                    assert created.status_code == 201, created.text
                    row = created.json()
                    reminder_id = row["id"]
                    assert row["version"] == 1
                    assert len((await owner.get("/api/v1/reminders")).json()) == 1
                    assert (await stranger.get("/api/v1/reminders")).json() == []
                    denied = await stranger.patch(f"/api/v1/reminders/{reminder_id}",
                                                   headers=headers(stranger_csrf),
                                                   json={"expected_version": 1, "text": "Hijack"})
                    assert denied.status_code == 404
                    changed = await owner.patch(f"/api/v1/reminders/{reminder_id}",
                                                headers=headers(owner_csrf),
                                                json={"expected_version": 1,
                                                      "remind_at": (due + timedelta(minutes=5)).isoformat(),
                                                      "text": "Updated notes"})
                    assert changed.status_code == 200, changed.text
                    async with factory() as db:
                        notes = (await db.scalars(select(Notification).where(
                            Notification.kind == "PERSONAL_REMINDER")
                            .order_by(Notification.created_at))).all()
                        assert len(notes) == 2
                        old_id, new_id = notes[0].id, notes[1].id
                        assert notes[0].body == "Напоминание: Check notes"
                        assert notes[1].body == "Напоминание: Updated notes"
                    assert (await stranger.post(f"/api/v1/notifications/{new_id}/read",
                                                headers=headers(stranger_csrf))).status_code == 404
                    marked = await owner.post(f"/api/v1/notifications/{new_id}/read",
                                              headers=headers(owner_csrf))
                    assert marked.status_code == 200 and marked.json()["read_at"]
                    again = await owner.post(f"/api/v1/notifications/{new_id}/read",
                                             headers=headers(owner_csrf))
                    assert again.status_code == 200
                    fake = FakeTransport()
                    assert await deliver_notifications(factory, fake, settings(),
                                                       at=due + timedelta(minutes=6)) == 2
                    assert len(fake.sent) == 1 and fake.sent[0][0] == 703
                    async with factory() as db:
                        assert (await db.get(Notification, old_id)).state == "SKIPPED"
                        assert (await db.get(Notification, new_id)).state == "SENT"
                    deleted = await owner.request("DELETE", f"/api/v1/reminders/{reminder_id}",
                                                  headers=headers(owner_csrf),
                                                  json={"expected_version": 2})
                    assert deleted.status_code == 200
                    assert (await owner.get("/api/v1/reminders")).json() == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_task_reminder_does_not_reveal_title_after_access_revoked():
    async def scenario():
        engine, app, factory, users, project, _ = await fixture(enforce_foreign_keys=True)
        try:
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as owner:
                    csrf = await login(owner, 703)
                    unlinked = await owner.post("/api/v1/reminders", headers=headers(csrf), json={
                        "remind_at": (now() + timedelta(hours=2)).isoformat(),
                        "text": "Личная заметка",
                    })
                    assert unlinked.status_code == 201, unlinked.text
                    created_task = await owner.post("/api/v1/tasks", headers=headers(csrf), json={
                        "project_id": str(project), "title": "Секретный план",
                        "description": "", "assignee_id": str(users[704]),
                        "priority": "HIGH", "type": "DEVELOPMENT",
                        "due_date": (now() + timedelta(days=30)).date().isoformat(),
                    })
                    assert created_task.status_code == 201, created_task.text
                    task_id = created_task.json()["id"]
                    due = now() + timedelta(hours=1)
                    created = await owner.post("/api/v1/reminders", headers=headers(csrf), json={
                        "task_id": task_id, "remind_at": due.isoformat(), "text": "Проверить план",
                    })
                    assert created.status_code == 201, created.text
                    reminder_id = created.json()["id"]
                    listed = (await owner.get("/api/v1/notifications")).json()
                    assert len(listed) == 2
                    first_note = next(note for note in listed if note["task_id"] == task_id)
                    first_id = first_note["id"]
                    assert "Секретный план" in first_note["body"]
                    assert created_task.json()["task_key"] in first_note["body"]

                    replay_key = str(uuid4())
                    marked = await owner.post(f"/api/v1/notifications/{first_id}/read",
                                              headers=headers(csrf, replay_key))
                    assert marked.status_code == 200 and "Секретный план" in marked.json()["body"]
                    edit_key = str(uuid4())
                    changed = await owner.patch(f"/api/v1/reminders/{reminder_id}",
                                                headers=headers(csrf, edit_key),
                                                json={"expected_version": 1, "text": "Обновить план"})
                    assert changed.status_code == 200, changed.text
                    assert changed.json()["version"] == 2
                    listed = (await owner.get("/api/v1/notifications")).json()
                    assert len(listed) == 3
                    assert all("Секретный план" in note["body"]
                               for note in listed if note["task_id"] == task_id)

                    async with factory() as db:
                        async with db.begin():
                            membership = await db.get(ProjectMember, (project, users[703]))
                            await db.delete(membership)

                    assert (await owner.get(f"/api/v1/tasks/{task_id}")).status_code == 404
                    visible = (await owner.get("/api/v1/notifications")).json()
                    assert len(visible) == 1 and visible[0]["body"] == "Напоминание: Личная заметка"
                    assert (await owner.get("/api/v1/notifications?limit=1")).json() == visible
                    for key in (replay_key, str(uuid4())):
                        denied_read = await owner.post(
                            f"/api/v1/notifications/{first_id}/read", headers=headers(csrf, key))
                        assert denied_read.status_code == 404
                        assert "Секретный план" not in denied_read.text
                    denied_edit = await owner.patch(f"/api/v1/reminders/{reminder_id}",
                                                    headers=headers(csrf),
                                                    json={"expected_version": 2, "text": "Ещё раз"})
                    assert denied_edit.status_code == 404
                    assert "Секретный план" not in denied_edit.text
                    denied_replay = await owner.patch(f"/api/v1/reminders/{reminder_id}",
                                                      headers=headers(csrf, edit_key),
                                                      json={"expected_version": 1, "text": "Обновить план"})
                    assert denied_replay.status_code == 404
                    reminders = (await owner.get("/api/v1/reminders")).json()
                    assert len(reminders) == 2
                    assert next(row for row in reminders if row["id"] == reminder_id)["version"] == 2
                    assert "Секретный план" not in str(reminders)
                    async with factory() as db:
                        actor = await db.get(User, users[703])
                        assert await my_notifications(db, actor, limit=1) == visible
                        reminder = await db.get(PersonalReminder, UUID(reminder_id))
                        assert reminder.version == 2
                        notes = (await db.scalars(select(Notification).where(
                            Notification.recipient_user_id == users[703]))).all()
                        assert len(notes) == 3
                        assert all("Секретный план" in note.body
                                   for note in notes if str(note.task_id) == task_id)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_notification_read_replay_rechecks_access_after_precheck():
    async def scenario():
        engine, app, factory, users, project, _ = await fixture(enforce_foreign_keys=True)
        try:
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                             base_url="http://localhost:8000") as owner:
                    csrf = await login(owner, 703)
                    task = await new_task(owner, csrf, project, users[704])
                    created = await owner.post("/api/v1/reminders", headers=headers(csrf), json={
                        "task_id": task["id"],
                        "remind_at": (now() + timedelta(hours=1)).isoformat(),
                        "text": "Проверить",
                    })
                    assert created.status_code == 201, created.text
                    note = (await owner.get("/api/v1/notifications")).json()[0]
                    assert task["title"] in note["body"]
                    key = str(uuid4())
                    first = await owner.post(f"/api/v1/notifications/{note['id']}/read",
                                             headers=headers(csrf, key))
                    assert first.status_code == 200 and task["title"] in first.json()["body"]

                    original_mutation = notification_routes.mutation
                    revoked = False

                    async def revoke_before_replay(*args, **kwargs):
                        nonlocal revoked
                        async with factory() as writer:
                            async with writer.begin():
                                membership = await writer.get(ProjectMember, (project, users[703]))
                                await writer.delete(membership)
                        revoked = True
                        stored_response = await original_mutation(*args, **kwargs)
                        assert task["title"] in stored_response.body.decode()
                        return stored_response

                    with patch.object(notification_routes, "mutation", new=revoke_before_replay):
                        replay = await owner.post(f"/api/v1/notifications/{note['id']}/read",
                                                  headers=headers(csrf, key))
                    assert revoked
                    assert replay.status_code == 404
                    assert task["title"] not in replay.text
                    async with factory() as db:
                        saved = await db.scalar(select(ApiIdempotency).where(ApiIdempotency.key == key))
                        assert task["title"] in saved.response_body["body"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_notification_limit_uses_stable_cursor_during_insert():
    async def scenario():
        engine, app, factory, users, project, _ = await fixture(enforce_foreign_keys=True)
        try:
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                             base_url="http://localhost:8000") as owner:
                    csrf = await login(owner, 703)
                    task = await new_task(owner, csrf, project, users[704])
                stamp = now() - timedelta(minutes=10)
                scheduled = now() + timedelta(hours=1)

                def note(body, created_at, task_id=None):
                    return Notification(recipient_user_id=users[703], task_id=task_id,
                                        kind="PERSONAL_REMINDER", dedupe_key=str(uuid4()),
                                        body=body, scheduled_at=scheduled, state="PENDING",
                                        created_at=created_at)

                async with factory() as writer:
                    async with writer.begin():
                        writer.add_all([note("Старое доступное", stamp),
                                        note("Новое доступное", stamp + timedelta(minutes=1)),
                                        note("Скрытое", stamp + timedelta(minutes=2), UUID(task["id"]))])
                async with factory() as writer:
                    async with writer.begin():
                        membership = await writer.get(ProjectMember, (project, users[703]))
                        await writer.delete(membership)

                original_check = notification_routes.can_view_notification
                checked = 0

                async def insert_between_batches(db, actor, row):
                    nonlocal checked
                    allowed = await original_check(db, actor, row)
                    checked += 1
                    if checked == 2:
                        async with factory() as writer:
                            async with writer.begin():
                                writer.add(note("Вставлено во время чтения",
                                                stamp + timedelta(minutes=3)))
                    return allowed

                with patch.object(notification_routes, "can_view_notification",
                                  new=insert_between_batches):
                    async with factory() as db:
                        actor = await db.get(User, users[703])
                        listed = await my_notifications(db, actor, limit=2)
                assert checked >= 3
                assert [row["body"] for row in listed] == ["Новое доступное", "Старое доступное"]
                assert len({row["id"] for row in listed}) == 2
        finally:
            await engine.dispose()

    asyncio.run(scenario())
