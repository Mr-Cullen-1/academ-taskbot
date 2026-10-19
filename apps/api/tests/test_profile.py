"""Private profile editing with real ORM tables and isolated sessions."""

import asyncio
import base64
from io import BytesIO
from uuid import uuid4

import httpx
from PIL import Image

from test_tasks import fixture, headers, login


def image_payload() -> dict:
    stream = BytesIO()
    Image.new("RGB", (24, 24), "purple").save(stream, format="PNG")
    return {"image_base64": base64.b64encode(stream.getvalue()).decode("ascii")}


def test_profile_name_avatar_permissions_validation_and_replay():
    async def scenario():
        engine, app, _, _, _, _ = await fixture()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as owner, \
                       httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as other:
                assert (await owner.get("/api/v1/users/me/avatar")).status_code == 401
                csrf = await login(owner, 703)
                other_csrf = await login(other, 704)
                profile = (await owner.get("/api/v1/users/me")).json()
                assert (await owner.patch("/api/v1/users/me", json={
                    "name": "New name", "expected_version": profile["version"]},
                    headers={"Idempotency-Key": str(uuid4())})).status_code == 403
                key = str(uuid4())
                updated = await owner.patch("/api/v1/users/me", json={
                    "name": "  New name  ", "expected_version": profile["version"]}, headers=headers(csrf, key))
                assert updated.status_code == 200 and updated.json()["name"] == "New name"
                assert (await owner.patch("/api/v1/users/me", json={
                    "name": "  New name  ", "expected_version": profile["version"]},
                    headers=headers(csrf, key))).json() == updated.json()
                conflict = await owner.patch("/api/v1/users/me", json={
                    "name": "Another name", "expected_version": profile["version"]}, headers=headers(csrf))
                assert conflict.status_code == 409
                assert (await other.get("/api/v1/users/me")).json()["name"] == "Person 704"
                assert (await owner.get("/api/v1/users/me/avatar")).status_code == 404
                invalid = await owner.put("/api/v1/users/me/avatar", json={
                    "image_base64": base64.b64encode(b"<svg>unsafe</svg>").decode()}, headers=headers(csrf))
                assert invalid.status_code == 422
                payload = image_payload()
                key = str(uuid4())
                saved = await owner.put("/api/v1/users/me/avatar", json=payload, headers=headers(csrf, key))
                assert saved.status_code == 200
                assert (await owner.put("/api/v1/users/me/avatar", json=payload,
                                        headers=headers(csrf, key))).json() == saved.json()
                avatar = await owner.get("/api/v1/users/me/avatar")
                assert avatar.status_code == 200 and avatar.headers["content-type"] == "image/jpeg"
                assert avatar.headers["cache-control"] == "private, no-store"
                assert avatar.content.startswith(b"\xff\xd8")
                assert (await other.get("/api/v1/users/me/avatar")).status_code == 404
                assert (await other.request("DELETE", "/api/v1/users/me/avatar", json={},
                                            headers=headers(other_csrf))).status_code == 200
                assert (await owner.get("/api/v1/users/me/avatar")).status_code == 200
                removed = await owner.request("DELETE", "/api/v1/users/me/avatar", json={}, headers=headers(csrf))
                assert removed.status_code == 200
                assert (await owner.get("/api/v1/users/me/avatar")).status_code == 404
        await engine.dispose()
    asyncio.run(scenario())
