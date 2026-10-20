import asyncio
from datetime import timedelta
from uuid import uuid4

import httpx
from sqlalchemy import select

from taskbot_api.models import Notification, now
from taskbot_worker.delivery import deliver_notifications
from test_tasks import fixture, headers, login
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
