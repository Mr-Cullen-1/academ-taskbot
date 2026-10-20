import asyncio

import httpx

from test_tasks import fixture, headers, login, new_task


def test_project_teams_and_calendar_sprints():
    async def scenario():
        engine, app, factory, users, project, other = await fixture(enforce_foreign_keys=True)
        try:
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as pm, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as member, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as admin:
                    pm_csrf = await login(pm, 702)
                    member_csrf = await login(member, 703)
                    admin_csrf = await login(admin, 701)
                    team = await pm.post(f"/api/v1/projects/{project}/teams", headers=headers(pm_csrf),
                                         json={"name": "Lessons"})
                    assert team.status_code == 201, team.text
                    team_id = team.json()["id"]
                    denied = await member.put(f"/api/v1/teams/{team_id}/members",
                                              headers=headers(member_csrf),
                                              json={"expected_version": 1, "members": [str(users[703])]})
                    assert denied.status_code == 403
                    members = await pm.put(f"/api/v1/teams/{team_id}/members", headers=headers(pm_csrf),
                                           json={"expected_version": 1,
                                                 "members": [str(users[703]), str(users[704])]})
                    assert members.status_code == 200, members.text
                    assert len((await member.get(f"/api/v1/teams/{team_id}/members")).json()) == 2
                    sprint = await pm.post(f"/api/v1/projects/{project}/sprints",
                                           headers=headers(pm_csrf),
                                           json={"name": "Week", "starts_on": "2027-01-01",
                                                 "ends_on": "2027-01-07"})
                    assert sprint.status_code == 201, sprint.text
                    assert sprint.json()["starts_on"] == "2027-01-01"
                    task = await new_task(member, member_csrf, project, users[704])
                    assigned = await pm.put(f"/api/v1/tasks/{task['id']}/sprint",
                                            headers=headers(pm_csrf),
                                            json={"expected_version": task["version"],
                                                  "sprint_id": sprint.json()["id"]})
                    assert assigned.status_code == 200, assigned.text
                    assert assigned.json()["sprint_id"] == sprint.json()["id"]
                    wrong = await admin.post(f"/api/v1/projects/{other}/sprints",
                                             headers=headers(admin_csrf),
                                             json={"name": "Other", "starts_on": "2027-01-01",
                                                   "ends_on": "2027-01-02"})
                    assert wrong.status_code == 201
                    mismatch = await pm.put(f"/api/v1/tasks/{task['id']}/sprint",
                                            headers=headers(pm_csrf),
                                            json={"expected_version": assigned.json()["version"],
                                                  "sprint_id": wrong.json()["id"]})
                    assert mismatch.status_code == 422
        finally:
            await engine.dispose()

    asyncio.run(scenario())
