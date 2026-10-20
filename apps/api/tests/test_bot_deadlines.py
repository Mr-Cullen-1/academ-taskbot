import asyncio
from uuid import UUID

import httpx
from sqlalchemy import select

from taskbot_api.models import DeadlineChange, DeadlineRequest
from taskbot_bot.menu import commit_deadline_decision, commit_deadline_request
from test_tasks import fixture, login, new_task


def test_bot_deadline_workflow_shares_api_rules_and_update_deduplication():
    async def scenario():
        engine, app, factory, users, project, other = await fixture(enforce_foreign_keys=True)
        try:
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as author:
                    author_csrf = await login(author, 703)
                    task = await new_task(author, author_csrf, project, users[704])
                    data = {"task_id": task["id"], "proposed_due_date": "2027-01-10",
                            "reason": "More material"}
                    forbidden = await commit_deadline_request(factory, 706, 9801, data, "Half done")
                    assert "NOT_FOUND" in forbidden
                    requested = await commit_deadline_request(factory, 704, 9802, data, "Half done")
                    assert requested.startswith("Готово:")
                    assert await commit_deadline_request(factory, 704, 9802, data, "Half done") == "Действие уже обработано."
                    async with factory() as db:
                        rows = (await db.scalars(select(DeadlineRequest))).all()
                        assert len(rows) == 1
                        request_id = rows[0].id
                    assert "NOT_FOUND" in await commit_deadline_decision(factory, 706, 9803, request_id, True)
                    accepted = await commit_deadline_decision(factory, 703, 9804, request_id, True)
                    assert accepted.startswith("Готово:")
                    assert await commit_deadline_decision(factory, 703, 9804, request_id, True) == "Действие уже обработано."
                    latest = (await author.get(f"/api/v1/tasks/{task['id']}")).json()
                    assert latest["original_due_date"] == "2027-01-01"
                    assert latest["current_due_date"] == "2027-01-10"
                    async with factory() as db:
                        assert len((await db.scalars(select(DeadlineChange))).all()) == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())
