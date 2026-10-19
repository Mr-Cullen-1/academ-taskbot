"""Team Lead has systemwide task visibility without admin authority."""

import asyncio
from uuid import UUID

import httpx
from sqlalchemy import select

from taskbot_api.models import ProjectMember, Task, User, UserRole
from taskbot_api.reporting import build_weekly_report

from test_tasks import act, fixture, headers, login, new_task


def test_team_lead_global_read_create_and_admin_denials():
    async def scenario():
        engine, app, factory, users, project, other = await fixture()
        try:
            async with factory() as db:
                async with db.begin():
                    lead = User(telegram_user_id=709, telegram_chat_id=709,
                                phone="+998709", name="Team Lead", status="ACTIVE")
                    db.add(lead)
                    await db.flush()
                    lead_id = lead.id
                    db.add(UserRole(user_id=lead_id, code="TEAM_LEAD"))
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as admin, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as lead, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as pm, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as qa, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as developer, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as outsider:
                    admin_csrf = await login(admin, 701)
                    lead_csrf = await login(lead, 709)
                    for client, number in ((pm, 702), (qa, 705), (developer, 703), (outsider, 706)):
                        await login(client, number)
                    first = await new_task(admin, admin_csrf, project, users[704], users[705])
                    second = await new_task(admin, admin_csrf, other, users[701])
                    comment = await admin.post(f"/api/v1/tasks/{second['id']}/comments",
                                               headers=headers(admin_csrf), json={"body": "Review notes"})
                    assert comment.status_code == 201

                    projects = await lead.get("/api/v1/projects")
                    assert projects.status_code == 200
                    assert {row["id"] for row in projects.json()} == {str(project), str(other)}
                    assert (await lead.get(f"/api/v1/projects/{other}")).status_code == 200
                    assert len((await lead.get(f"/api/v1/projects/{other}/members")).json()) == 1
                    tasks = await lead.get("/api/v1/tasks")
                    assert {row["id"] for row in tasks.json()} == {first["id"], second["id"]}
                    assert len((await lead.get("/api/v1/tasks", params={"project_id": str(other),
                                                                      "status": "OPEN", "priority": "HIGH",
                                                                      "assignee_id": str(users[701])})).json()) == 1
                    detail = await lead.get(f"/api/v1/tasks/{second['id']}")
                    assert detail.status_code == 200
                    assert detail.json()["assignee_id"] == str(users[701])
                    assert "admin-approve" not in detail.json()["available_actions"]
                    assert (await lead.get(f"/api/v1/tasks/{second['id']}/comments")).json()[0]["body"] == "Review notes"
                    assert len((await lead.get(f"/api/v1/tasks/{second['id']}/events")).json()) >= 2
                    assert (await lead.get(f"/api/v1/tasks/{second['id']}/qa-reviews")).status_code == 200

                    created = await new_task(lead, lead_csrf, other, users[701])
                    assert created["created_by"] == str(lead_id)
                    assert created["status"] == "OPEN"
                    assert (await lead.post("/api/v1/tasks", headers=headers(lead_csrf), json={
                        "project_id": str(other), "title": "Invalid assignee", "due_date": "2027-01-01",
                        "assignee_id": str(users[704])})).status_code == 422
                    assert (await lead.post("/api/v1/tasks", headers=headers(lead_csrf), json={
                        "project_id": str(other), "title": "Invalid QA", "due_date": "2027-01-01",
                        "qa_reviewer_id": str(users[705])})).status_code == 422

                    async with factory() as db:
                        assert await db.get(ProjectMember, (other, lead_id)) is None
                        assert await db.get(ProjectMember, (project, lead_id)) is None
                        saved = await db.get(User, lead_id)
                        report = await build_weekly_report(db, saved)
                        assert {row["project_id"] for row in report["projects"]} == {str(project), str(other)}
                        assert (await db.scalar(select(Task).where(Task.id == UUID(created["id"])))) is not None

                    assert (await lead.get("/api/v1/users")).status_code == 403
                    assert (await lead.get("/api/v1/users/pending")).status_code == 403
                    assert (await lead.put(f"/api/v1/users/{users[703]}/roles", headers=headers(lead_csrf),
                                           json={"roles": ["PM"], "expected_version": 1})).status_code == 403
                    assert (await lead.patch(f"/api/v1/users/{users[703]}/status", headers=headers(lead_csrf),
                                             json={"status": "BLOCKED", "expected_version": 1})).status_code == 403
                    assert (await lead.post("/api/v1/projects", headers=headers(lead_csrf),
                                            json={"name": "Forbidden"})).status_code == 403
                    assert (await lead.put(f"/api/v1/projects/{other}/members", headers=headers(lead_csrf),
                                           json={"members": [str(users[701])], "expected_version": 1})).status_code == 403
                    assert (await lead.patch(f"/api/v1/tasks/{second['id']}", headers=headers(lead_csrf),
                                             json={"expected_version": second["version"],
                                                   "title": "Forbidden"})).status_code == 403
                    second = (await admin.get(f"/api/v1/tasks/{second['id']}")).json()
                    for action in ("accept", "start", "submit-review"):
                        moved = await act(admin, admin_csrf, second, action, result="Ready")
                        assert moved.status_code == 200, moved.text
                        second = moved.json()
                    assert second["status"] == "REVIEW"
                    assert (await lead.post(f"/api/v1/tasks/{second['id']}/admin-approve",
                                            headers=headers(lead_csrf),
                                            json={"expected_version": second["version"],
                                                  "reason": "Forbidden",
                                                  "author_unavailable_confirmed": True})).status_code == 403

                    assert {row["id"] for row in (await admin.get("/api/v1/tasks")).json()} == {
                        first["id"], second["id"], created["id"]}
                    for client in (pm, qa, developer):
                        assert {row["id"] for row in (await client.get("/api/v1/tasks")).json()} == {first["id"]}
                        assert (await client.get(f"/api/v1/tasks/{second['id']}")).status_code == 404
                    assert (await outsider.get("/api/v1/tasks")).json() == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())
