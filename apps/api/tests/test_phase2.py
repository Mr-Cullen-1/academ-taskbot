import asyncio
import hashlib
import hmac
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from aiogram import Bot
from aiogram.types import Chat, Contact, Message, Update, User as TelegramUser
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from taskbot_api.config import Settings
from taskbot_api.db import Base, session_factory
from taskbot_api.main import create_app
from taskbot_api.models import AuditLog, User, UserRole
from taskbot_api.registration import RegistrationEvent, process_registration
from taskbot_bot.__main__ import build_dispatcher


ORIGIN = "http://localhost:3000"
TOKEN = "123456:test-token-only"


async def environment():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    app = create_app(Settings(_env_file=None, database_url=None, bot_token=TOKEN,
                              web_origin=ORIGIN, session_cookie_secure=False), engine_override=engine)
    return engine, app


async def register(factory, number: int, *, foreign_contact: bool = False):
    async def event(update, kind, **kwargs):
        return await process_registration(factory, RegistrationEvent(
            update_id=update, telegram_user_id=number, telegram_chat_id=number,
            kind=kind, **kwargs))
    start = await event(number * 10, "START")
    contact = await event(number * 10 + 1, "CONTACT", phone="+998901234567",
                          contact_user_id=number + (1 if foreign_contact else 0))
    name = await event(number * 10 + 2, "NAME", name=f"Person {number}")
    return start, contact, name


async def bootstrap_for_test(factory, telegram_id: int):
    async with factory() as db:
        async with db.begin():
            user = await db.scalar(select(User).where(User.telegram_user_id == telegram_id))
            user.status = "ACTIVE"
            db.add(UserRole(user_id=user.id, code="SUPER_ADMIN", super_admin_slot=1))
        return user


def signed_proof(telegram_id: int, offset: int = 0) -> dict:
    data = {"id": telegram_id, "auth_date": int(time.time()) + offset, "first_name": "Test"}
    check = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    data["hash"] = hmac.new(hashlib.sha256(TOKEN.encode()).digest(), check.encode(), hashlib.sha256).hexdigest()
    return data


async def login(client, telegram_id: int, offset: int = 0):
    assert (await client.get("/api/v1/auth/telegram/challenge", headers={"Origin": ORIGIN})).status_code == 200
    return await client.post("/api/v1/auth/telegram", json=signed_proof(telegram_id, offset), headers={"Origin": ORIGIN})


def mutation_headers(csrf: str) -> dict:
    return {"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid4())}


def test_registration_contact_replay_and_pending_access():
    async def scenario():
        engine, app = await environment()
        factory = session_factory(engine)
        start, contact, result = await register(factory, 101)
        assert [start.code, contact.code, result.code] == ["CONTACT_REQUIRED", "NAME_REQUIRED", "ACCOUNT_PENDING"]
        repeat = await process_registration(factory, RegistrationEvent(1012, 101, 101, "NAME", name="Person 101"))
        assert repeat.duplicate
        assert (await process_registration(factory, RegistrationEvent(9999, 101, 101, "START"))).code == "ACCOUNT_PENDING"
        async with factory() as db:
            assert len((await db.scalars(select(User).where(User.telegram_user_id == 101))).all()) == 1
            assert (await db.scalars(select(UserRole))).all() == []
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000") as client:
                assert (await client.get("/api/v1/projects")).status_code == 401
                assert (await login(client, 101)).status_code == 403
        await engine.dispose()
    asyncio.run(scenario())


def test_foreign_contact_cannot_register():
    async def scenario():
        engine, _ = await environment()
        factory = session_factory(engine)
        _, contact, result = await register(factory, 102, foreign_contact=True)
        assert contact.code == "INVALID_CONTACT"
        assert result.code == "CONTACT_REQUIRED"
        async with factory() as db:
            assert await db.scalar(select(User).where(User.telegram_user_id == 102)) is None
        await engine.dispose()
    asyncio.run(scenario())


def test_aiogram_handlers_process_updates_without_network():
    async def scenario():
        engine, _ = await environment()
        factory = session_factory(engine)
        dispatcher = build_dispatcher()
        bot = Bot(token=TOKEN)
        sender = TelegramUser(id=401, is_bot=False, first_name="Test")
        chat = Chat(id=401, type="private")
        def message(mid, *, text=None, contact=None):
            return Message(message_id=mid, date=datetime.now(timezone.utc), chat=chat,
                           from_user=sender, text=text, contact=contact)
        try:
            with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
                await dispatcher.feed_update(bot, Update(update_id=4010, message=message(1, text="/start")),
                                             registration_factory=factory)
                await dispatcher.feed_update(bot, Update(update_id=4011, message=message(2,
                    contact=Contact(phone_number="+998901234567", first_name="Test", user_id=401))),
                    registration_factory=factory)
                await dispatcher.feed_update(bot, Update(update_id=4012, message=message(3, text="Test Person")),
                                             registration_factory=factory)
                assert answer.await_count == 3
                await dispatcher.feed_update(bot, Update(update_id=4012, message=message(3, text="Test Person")),
                                             registration_factory=factory)
                assert answer.await_count == 3
            async with factory() as db:
                assert (await db.scalar(select(User).where(User.telegram_user_id == 401))).status == "PENDING"
        finally:
            await bot.session.close()
            await engine.dispose()
    asyncio.run(scenario())


def test_login_signature_origin_replay_and_csrf():
    async def scenario():
        engine, app = await environment()
        factory = session_factory(engine)
        await register(factory, 301)
        await bootstrap_for_test(factory, 301)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000") as client:
                await client.get("/api/v1/auth/telegram/challenge", headers={"Origin": ORIGIN})
                proof = signed_proof(301)
                assert (await client.post("/api/v1/auth/telegram", json=proof,
                    headers={"Origin": "https://attacker.invalid"})).status_code == 403
                invalid = {**proof, "first_name": "Altered"}
                assert (await client.post("/api/v1/auth/telegram", json=invalid,
                    headers={"Origin": ORIGIN})).status_code == 401
                logged = await client.post("/api/v1/auth/telegram", json=proof, headers={"Origin": ORIGIN})
                assert logged.status_code == 200
                assert (await client.post("/api/v1/projects", json={"name": "No CSRF"},
                    headers={"Idempotency-Key": str(uuid4())})).status_code == 403
                assert (await client.post("/api/v1/projects", json={"name": "No key"},
                    headers={"X-CSRF-Token": logged.json()["csrf_token"]})).status_code == 400
                await client.get("/api/v1/auth/telegram/challenge", headers={"Origin": ORIGIN})
                assert (await client.post("/api/v1/auth/telegram", json=proof,
                    headers={"Origin": ORIGIN})).status_code == 409
        await engine.dispose()
    asyncio.run(scenario())


def test_database_rejects_second_super_admin():
    async def scenario():
        engine, _ = await environment()
        factory = session_factory(engine)
        await register(factory, 501)
        await register(factory, 502)
        await bootstrap_for_test(factory, 501)
        async with factory() as db:
            other = await db.scalar(select(User).where(User.telegram_user_id == 502))
            db.add(UserRole(user_id=other.id, code="SUPER_ADMIN", super_admin_slot=1))
            try:
                await db.commit()
                assert False, "second Super Admin must violate the unique slot"
            except IntegrityError:
                await db.rollback()
        await engine.dispose()
    asyncio.run(scenario())


def test_approval_rejection_roles_projects_and_block():
    async def scenario():
        engine, app = await environment()
        factory = session_factory(engine)
        for number in (201, 202, 203, 204, 205):
            await register(factory, number)
        await bootstrap_for_test(factory, 201)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000") as admin:
                signed = await login(admin, 201)
                assert signed.status_code == 200
                csrf = signed.json()["csrf_token"]
                pending = await admin.get("/api/v1/users/pending")
                assert len(pending.json()) == 4
                by_telegram = {p["telegram_user_id"]: p for p in pending.json()}

                async def status(number, value, roles=None):
                    p = by_telegram[number]
                    body = {"status": value, "expected_version": p["version"]}
                    if roles is not None: body["roles"] = roles
                    return await admin.patch(f"/api/v1/users/{p['id']}/status", json=body,
                                             headers=mutation_headers(csrf))

                assert (await status(202, "ACTIVE", ["PM"])).status_code == 200
                assert (await status(203, "ACTIVE", ["TEAM_LEAD", "QA"])).status_code == 200
                assert (await status(204, "ACTIVE", ["SUBJECT_DEVELOPER"])).status_code == 200
                assert (await status(205, "REJECTED")).status_code == 200
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000") as rejected:
                    assert (await login(rejected, 205)).status_code == 403
                assert (await admin.get("/api/v1/users")).status_code == 200
                bad_role = await admin.put(f"/api/v1/users/{by_telegram[204]['id']}/roles",
                    json={"roles": ["OWNER"], "expected_version": 2}, headers=mutation_headers(csrf))
                assert bad_role.status_code == 422
                changed = await admin.put(f"/api/v1/users/{by_telegram[204]['id']}/roles",
                    json={"roles": ["SUBJECT_DEVELOPER", "QA"], "expected_version": 2},
                    headers=mutation_headers(csrf))
                assert changed.status_code == 200
                assert changed.json()["roles"] == ["QA", "SUBJECT_DEVELOPER"]
                sa_project = await admin.post("/api/v1/projects", json={"name": "Admin project"},
                                              headers=mutation_headers(csrf))
                assert sa_project.status_code == 201
                assert (await admin.post("/api/v1/projects", json={"name": "   "},
                                         headers=mutation_headers(csrf))).status_code == 422

                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000") as pm:
                    pm_login = await login(pm, 202)
                    assert pm_login.status_code == 200
                    pm_csrf = pm_login.json()["csrf_token"]
                    assert (await pm.get("/api/v1/users")).status_code == 403
                    assert (await pm.put(f"/api/v1/users/{by_telegram[204]['id']}/roles",
                        json={"roles": ["PM"], "expected_version": 3},
                        headers=mutation_headers(pm_csrf))).status_code == 403
                    created = await pm.post("/api/v1/projects", json={"name": "PM project"},
                                            headers=mutation_headers(pm_csrf))
                    assert created.status_code == 201
                    project_id = created.json()["id"]
                    assert len((await pm.get("/api/v1/projects")).json()) == 1
                    assert (await pm.get(f"/api/v1/projects/{sa_project.json()['id']}")).status_code == 404
                    assert len((await pm.get(f"/api/v1/projects/{project_id}/members")).json()) == 1
                    assert (await pm.put(f"/api/v1/projects/{project_id}/members",
                        json={"members": [by_telegram[202]["id"]], "expected_version": 1},
                        headers=mutation_headers(pm_csrf))).status_code == 403
                    assert (await pm.patch(f"/api/v1/users/{by_telegram[204]['id']}/status",
                        json={"status": "BLOCKED", "expected_version": 2},
                        headers=mutation_headers(pm_csrf))).status_code == 403
                    replay_key = mutation_headers(pm_csrf)
                    first = await pm.post("/api/v1/projects", json={"name": "Replay"}, headers=replay_key)
                    second = await pm.post("/api/v1/projects", json={"name": "Replay"}, headers=replay_key)
                    assert first.json() == second.json()
                    assert (await pm.post("/api/v1/projects", json={"name": "Other"}, headers=replay_key)).status_code == 409

                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000") as member:
                    member_login = await login(member, 203)
                    member_csrf = member_login.json()["csrf_token"]
                    assert {row["name"] for row in (await member.get("/api/v1/projects")).json()} == {
                        "Admin project", "PM project", "Replay"}
                    assert (await member.get(f"/api/v1/projects/{project_id}")).status_code == 200
                    assert (await member.post("/api/v1/projects", json={"name": "Forbidden"},
                        headers=mutation_headers(member_csrf))).status_code == 403
                    added = await admin.put(f"/api/v1/projects/{project_id}/members",
                        json={"members": [by_telegram[202]["id"], by_telegram[203]["id"]], "expected_version": 1},
                        headers=mutation_headers(csrf))
                    assert added.status_code == 200
                    assert len((await member.get("/api/v1/projects")).json()) == 3
                    blocked = await admin.patch(f"/api/v1/users/{by_telegram[203]['id']}/status",
                        json={"status": "BLOCKED", "expected_version": 2}, headers=mutation_headers(csrf))
                    assert blocked.status_code == 200
                    assert (await member.get("/api/v1/users/me")).status_code in (401, 403)
                    assert (await login(member, 203)).status_code == 403
                    unblocked = await admin.patch(f"/api/v1/users/{by_telegram[203]['id']}/status",
                        json={"status": "ACTIVE", "expected_version": 3}, headers=mutation_headers(csrf))
                    assert unblocked.status_code == 200
                    assert (await member.get("/api/v1/users/me")).status_code in (401, 403)
                    assert (await login(member, 203, offset=1)).status_code == 200
                async with factory() as db:
                    assert len((await db.scalars(select(AuditLog).where(AuditLog.target_id == by_telegram[203]["id"]))).all()) >= 2
        await engine.dispose()
    asyncio.run(scenario())
