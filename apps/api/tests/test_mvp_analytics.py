import asyncio
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from taskbot_api.models import Task, User, UserRole
from taskbot_api.reporting import TASHKENT
from test_tasks import fixture, login


def test_analytics_project_scope_and_period_boundaries():
    async def scenario():
        engine, app, factory, users, project, other = await fixture()
        try:
            async with factory() as db:
                async with db.begin():
                    db.add(UserRole(user_id=users[703], code="TEAM_LEAD"))
                    db.add(Task(task_key="TASK-9901", project_id=project, created_by=users[703],
                                assignee_id=users[704], title="Start", description="", status="OPEN",
                                priority="HIGH", type="DEVELOPMENT",
                                original_due_date=datetime(2027, 1, 1).date(),
                                current_due_date=datetime(2027, 1, 1).date(),
                                created_at=datetime(2026, 10, 26, 19, tzinfo=TASHKENT).astimezone(timezone.utc),
                                updated_at=datetime(2026, 10, 26, 19, tzinfo=TASHKENT).astimezone(timezone.utc), version=1))
                    db.add(Task(task_key="TASK-9902", project_id=other, created_by=users[701],
                                assignee_id=users[701], title="Private", description="", status="OPEN",
                                priority="LOW", type="DEVELOPMENT",
                                original_due_date=datetime(2027, 1, 1).date(),
                                current_due_date=datetime(2027, 1, 1).date(),
                                created_at=datetime(2026, 10, 27, 1, tzinfo=TASHKENT).astimezone(timezone.utc),
                                updated_at=datetime(2026, 10, 27, 1, tzinfo=TASHKENT).astimezone(timezone.utc), version=1))
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as pm, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as admin, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as lead:
                    await login(pm, 702)
                    await login(admin, 701)
                    await login(lead, 703)
                    params = {"start_on": "2026-10-27", "end_on": "2026-11-03"}
                    pm_report = await pm.get("/api/v1/reports/tasks", params=params)
                    assert pm_report.status_code == 200, pm_report.text
                    assert pm_report.json()["totals"]["created"] == 0
                    assert len(pm_report.json()["by_project"]) == 1
                    assert (await pm.get("/api/v1/reports/tasks", params={**params,
                                                                           "project_id": str(other)})).status_code == 404
                    admin_report = await admin.get("/api/v1/reports/tasks", params=params)
                    assert admin_report.json()["totals"]["created"] == 1
                    assert (await lead.get("/api/v1/reports/tasks", params=params)).status_code == 403
                    people = await pm.get("/api/v1/reports/users", params=params)
                    assert people.status_code == 200
                    assert all("phone" not in row for row in people.json()["users"])
        finally:
            await engine.dispose()

    asyncio.run(scenario())
