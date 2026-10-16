"""Mini App proof exchange against real ORM models and isolated AsyncSession."""

import asyncio
import hashlib
import hmac
import json
import time
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode
from uuid import uuid4

import httpx
from aiogram import Bot
from aiogram.types import Chat, InlineKeyboardMarkup, Message, Update, User as TelegramUser
from sqlalchemy import select

from taskbot_api.config import Settings
from taskbot_api.main import create_app
from taskbot_api.models import LoginProof, TaskEvent, User, WebSession, now
from taskbot_bot.__main__ import build_dispatcher
from test_tasks import ORIGIN, TOKEN, fixture


def signed_init_data(telegram_id: int, *, offset: int = 0, query_id: str = "test",
                     user_override: str | None = None, token: str = TOKEN) -> str:
    data = {
        "auth_date": str(int(time.time()) + offset),
        "query_id": query_id,
        "user": user_override if user_override is not None else
            json.dumps({"id": telegram_id, "first_name": "Test"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


def test_webapp_login_validates_proof_account_session_and_csrf():
    async def scenario():
        engine, app, factory, users, project, other = await fixture()
        async with factory() as db:
            async with db.begin():
                db.add(User(telegram_user_id=709, telegram_chat_id=709, phone="+998709",
                            name="Pending", status="PENDING"))
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as admin, \
                       httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as member:
                assert (await admin.get("/api/v1/tasks")).status_code == 401
                valid = signed_init_data(701)
                assert (await admin.post("/api/v1/auth/webapp", json={"init_data": valid},
                                         headers={"Origin": "https://wrong.example"})).status_code == 403
                assert (await admin.post("/api/v1/auth/webapp", json={"init_data": valid.replace("701", "702")},
                                         headers={"Origin": ORIGIN})).status_code == 401
                assert (await admin.post("/api/v1/auth/webapp", json={"init_data": signed_init_data(701, token="other:test-token")},
                                         headers={"Origin": ORIGIN})).status_code == 401
                assert (await admin.post("/api/v1/auth/webapp", json={"init_data": signed_init_data(701, offset=-600)},
                                         headers={"Origin": ORIGIN})).status_code == 401
                assert (await admin.post("/api/v1/auth/webapp", json={"init_data": signed_init_data(701, user_override="{}")},
                                         headers={"Origin": ORIGIN})).status_code == 401
                assert (await admin.post("/api/v1/auth/webapp", json={"init_data": valid + "&user=duplicate"},
                                         headers={"Origin": ORIGIN})).status_code == 401
                for telegram_id, expected in ((709, "ACCOUNT_PENDING"), (707, "ACCOUNT_BLOCKED"),
                                               (999, "REGISTRATION_REQUIRED")):
                    denied = await admin.post("/api/v1/auth/webapp",
                                              json={"init_data": signed_init_data(telegram_id)},
                                              headers={"Origin": ORIGIN})
                    assert denied.status_code == 403 and denied.json()["detail"] == expected
                    assert (await admin.get("/api/v1/users/me")).status_code == 401
                logged = await admin.post("/api/v1/auth/webapp", json={"init_data": valid},
                                          headers={"Origin": ORIGIN})
                assert logged.status_code == 200
                assert logged.json()["user"]["id"] == str(users[701])
                csrf = logged.json()["csrf_token"]
                assert (await admin.get("/api/v1/users/me")).status_code == 200
                assert (await admin.get("/api/v1/projects")).status_code == 200
                assert (await member.post("/api/v1/auth/session/refresh",
                                          headers={"Origin": ORIGIN})).status_code == 401
                assert (await admin.post("/api/v1/auth/session/refresh")).status_code == 403
                assert (await admin.post("/api/v1/auth/session/refresh",
                                         headers={"Origin": "https://wrong.example"})).status_code == 403
                reopened = await admin.post("/api/v1/auth/session/refresh", headers={"Origin": ORIGIN})
                assert reopened.status_code == 200
                renewed_csrf = reopened.json()["csrf_token"]
                assert renewed_csrf != csrf
                assert (await admin.get("/api/v1/users/me")).status_code == 200
                replay = await member.post("/api/v1/auth/webapp", json={"init_data": valid},
                                            headers={"Origin": ORIGIN})
                assert replay.status_code == 409
                assert (await member.get("/api/v1/users/me")).status_code == 401
                body = {"project_id": str(project), "title": "Mini App task", "due_date": "2027-01-01"}
                no_csrf = await admin.post("/api/v1/tasks", json=body,
                                           headers={"Idempotency-Key": str(uuid4())})
                assert no_csrf.status_code == 403
                old_csrf = await admin.post("/api/v1/tasks", json=body, headers={
                    "X-CSRF-Token": csrf, "Idempotency-Key": str(uuid4())})
                assert old_csrf.status_code == 403
                created = await admin.post("/api/v1/tasks", json=body, headers={
                    "X-CSRF-Token": renewed_csrf, "Idempotency-Key": str(uuid4())})
                assert created.status_code == 201, created.text
                assert (await admin.get(f"/api/v1/tasks/{created.json()['id']}")).status_code == 200
                assert len((await admin.get("/api/v1/tasks")).json()) == 1
                member_login = await member.post("/api/v1/auth/webapp",
                                                 json={"init_data": signed_init_data(703)},
                                                 headers={"Origin": ORIGIN})
                assert member_login.status_code == 200
                assert (await member.get("/api/v1/tasks")).status_code == 200
                assert (await member.get("/api/v1/tasks", params={"project_id": str(other)})).json() == []
                assert (await member.post("/api/v1/projects", json={"name": "Forbidden"}, headers={
                    "X-CSRF-Token": member_login.json()["csrf_token"],
                    "Idempotency-Key": str(uuid4())})).status_code == 403
                assert (await admin.post("/api/v1/auth/logout", headers={"X-CSRF-Token": renewed_csrf})).status_code == 204
                assert (await admin.get("/api/v1/users/me")).status_code == 401
                assert (await admin.post("/api/v1/auth/session/refresh",
                                         headers={"Origin": ORIGIN})).status_code == 401
                async with factory() as db:
                    assert len((await db.scalars(select(LoginProof))).all()) == 2
                    assert len((await db.scalars(select(WebSession))).all()) == 2
                    assert len((await db.scalars(select(TaskEvent))).all()) == 1
        await engine.dispose()
    asyncio.run(scenario())


def test_session_refresh_rejects_expired_and_inactive_accounts():
    async def scenario():
        engine, app, factory, users, _, _ = await fixture()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://localhost:8000") as expired, \
                       httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://localhost:8000") as blocked:
                for client, telegram_id in ((expired, 701), (blocked, 703)):
                    result = await client.post("/api/v1/auth/webapp",
                                               json={"init_data": signed_init_data(telegram_id)},
                                               headers={"Origin": ORIGIN})
                    assert result.status_code == 200
                async with factory() as db:
                    sessions = (await db.scalars(select(WebSession))).all()
                    for session in sessions:
                        if session.user_id == users[701]:
                            session.expires_at = now() - timedelta(seconds=1)
                    user = await db.get(User, users[703])
                    user.status = "BLOCKED"
                    await db.commit()
                assert (await expired.post("/api/v1/auth/session/refresh",
                                           headers={"Origin": ORIGIN})).status_code == 401
                assert (await blocked.post("/api/v1/auth/session/refresh",
                                           headers={"Origin": ORIGIN})).status_code == 403
        await engine.dispose()
    asyncio.run(scenario())


def test_active_start_includes_webapp_button_without_contact_flow():
    async def scenario():
        engine, _, factory, _, _, _ = await fixture()
        bot = Bot(token=TOKEN)
        message = Message(message_id=1, date=int(time.time()), chat=Chat(id=701, type="private"),
                          from_user=TelegramUser(id=701, is_bot=False, first_name="Admin"), text="/start")
        try:
            with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
                await build_dispatcher().feed_update(bot, Update(update_id=9701, message=message),
                    registration_factory=factory, webapp_url="https://example.test/taskbot")
                assert answer.await_count == 1
                markup = answer.call_args.kwargs["reply_markup"]
                assert isinstance(markup, InlineKeyboardMarkup)
                assert markup.inline_keyboard[0][0].web_app.url == "https://example.test/taskbot"
        finally:
            await bot.session.close()
            await engine.dispose()
    asyncio.run(scenario())


def test_webapp_https_session_cookie_flags():
    async def scenario():
        engine, _, _, _, _, _ = await fixture()
        origin = "https://mini.example.test"
        app = create_app(Settings(_env_file=None, database_url=None, bot_token=TOKEN,
                                  web_origin=origin, session_cookie_secure=True), engine_override=engine)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin) as client:
                response = await client.post("/api/v1/auth/webapp",
                                             json={"init_data": signed_init_data(701)},
                                             headers={"Origin": origin})
                assert response.status_code == 200
                cookie = response.headers["set-cookie"].lower()
                assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie
        await engine.dispose()
    asyncio.run(scenario())
