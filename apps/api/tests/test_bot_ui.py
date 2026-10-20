"""Telegram UI exercises real ORM services with mocked Telegram methods."""

import asyncio
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import httpx
from aiogram import Bot
from aiogram.types import (CallbackQuery, Chat, MenuButtonWebApp, Message,
                           ReplyKeyboardMarkup, Update, User as TelegramUser)
from sqlalchemy import select

from taskbot_api.config import Settings
from taskbot_api.models import AuditLog, Notification, ProjectMember, Task, TaskEvent, User, UserRole
from taskbot_api.registration import (RegistrationEvent, process_registration,
                                      requested_access, submit_access_request)
from taskbot_api.task_service import create_task
from taskbot_bot.__main__ import build_dispatcher, configure_menu_button
from taskbot_bot.menu import MENU
from taskbot_worker.delivery import deliver_notifications
from test_phase4 import FakeTransport
from test_tasks import TOKEN, fixture, login


def incoming(update_id: int, telegram_id: int, text: str) -> Update:
    sender = TelegramUser(id=telegram_id, is_bot=False, first_name="Tester")
    return Update(update_id=update_id, message=Message(
        message_id=update_id, date=datetime.now(timezone.utc),
        chat=Chat(id=telegram_id, type="private"), from_user=sender, text=text))


def pressed(update_id: int, telegram_id: int, data: str) -> Update:
    sender = TelegramUser(id=telegram_id, is_bot=False, first_name="Tester")
    bot_sender = TelegramUser(id=999, is_bot=True, first_name="Taskbot")
    message = Message(message_id=update_id, date=datetime.now(timezone.utc),
                      chat=Chat(id=telegram_id, type="private"),
                      from_user=bot_sender, text="Taskbot")
    return Update(update_id=update_id, callback_query=CallbackQuery(
        id=str(update_id), from_user=sender, chat_instance="local",
        message=message, data=data))


async def register_pending(factory, telegram_id: int, base: int):
    for offset, kind, extra in ((0, "START", {}),
                                (1, "CONTACT", {"contact_user_id": telegram_id,
                                                "phone": "+998901234567"}),
                                (2, "NAME", {"name": "New colleague"})):
        await process_registration(factory, RegistrationEvent(
            update_id=base + offset, telegram_user_id=telegram_id,
            telegram_chat_id=telegram_id, kind=kind, **extra))


def test_registration_request_admin_buttons_and_rejected_delivery():
    async def scenario():
        engine, _, factory, users, project_id, _ = await fixture()
        bot = Bot(token=TOKEN)
        dispatcher = build_dispatcher()
        try:
            await register_pending(factory, 801, 80100)
            with patch.object(CallbackQuery, "answer", new_callable=AsyncMock), \
                 patch.object(Message, "edit_text", new_callable=AsyncMock), \
                 patch.object(Message, "edit_reply_markup", new_callable=AsyncMock):
                await dispatcher.feed_update(bot, pressed(80103, 801, "reg:role:3"),
                                             registration_factory=factory)
                await dispatcher.feed_update(bot, pressed(80104, 801, f"reg:project:3:{project_id}"),
                                             registration_factory=factory)
                async with factory() as db:
                    candidate = await db.scalar(select(User).where(User.telegram_user_id == 801))
                    request = await requested_access(db, candidate.id)
                    assert request["role"] == "SUBJECT_DEVELOPER"
                    assert request["project_id"] == str(project_id)
                    assert candidate.status == "PENDING"
                    assert await db.get(ProjectMember, (project_id, candidate.id)) is None
                    admin_note = await db.scalar(select(Notification).where(
                        Notification.kind == "REGISTRATION_REQUEST"))
                    assert admin_note.recipient_user_id == users[701]
                settings = Settings(_env_file=None, database_url=None, bot_token=TOKEN,
                                    notification_delivery_enabled=True,
                                    notification_transport="telegram")
                fake = FakeTransport()
                assert await deliver_notifications(factory, fake, settings) == 1
                assert fake.sent[0][2].inline_keyboard[0][0].callback_data.startswith("reg:approve:")
                await dispatcher.feed_update(bot, pressed(80105, 704, f"reg:approve:{candidate.id}"),
                                             registration_factory=factory)
                async with factory() as db:
                    assert (await db.get(User, candidate.id)).status == "PENDING"
                await dispatcher.feed_update(bot, pressed(80106, 701, f"reg:approve:{candidate.id}"),
                                             registration_factory=factory)
                await dispatcher.feed_update(bot, pressed(80107, 701, f"reg:approve:{candidate.id}"),
                                             registration_factory=factory)
                async with factory() as db:
                    current = await db.get(User, candidate.id)
                    assert current.status == "ACTIVE" and current.version == 2
                    assert await db.get(ProjectMember, (project_id, current.id)) is not None
                    assert (await db.scalars(select(UserRole.code).where(
                        UserRole.user_id == current.id))).all() == ["SUBJECT_DEVELOPER"]
                    notes = (await db.scalars(select(Notification).where(
                        Notification.recipient_user_id == current.id))).all()
                    assert len(notes) == 1 and notes[0].kind == "REGISTRATION_APPROVED"
                assert await deliver_notifications(factory, fake, settings) == 1
                assert fake.sent[-1][0] == 801

                await register_pending(factory, 802, 80200)
                await dispatcher.feed_update(bot, pressed(80203, 802, "reg:role:4"),
                                             registration_factory=factory)
                await dispatcher.feed_update(bot, pressed(80204, 802, "reg:project:4:none"),
                                             registration_factory=factory)
                async with factory() as db:
                    rejected = await db.scalar(select(User).where(User.telegram_user_id == 802))
                await dispatcher.feed_update(bot, pressed(80205, 701, f"reg:reject:{rejected.id}"),
                                             registration_factory=factory)
                async with factory() as db:
                    assert (await db.get(User, rejected.id)).status == "REJECTED"
                    assert (await db.scalars(select(UserRole).where(
                        UserRole.user_id == rejected.id))).all() == []
                assert await deliver_notifications(factory, fake, settings) >= 1
                assert any(chat == 802 and "отклонена" in body for chat, body, _ in fake.sent)
        finally:
            await bot.session.close()
            await engine.dispose()
    asyncio.run(scenario())


def test_menu_button_sections_create_and_transition_use_shared_rules():
    async def scenario():
        engine, _, factory, users, project_id, _ = await fixture()
        bot = Bot(token=TOKEN)
        dispatcher = build_dispatcher()
        try:
            with patch.object(bot, "set_chat_menu_button", new_callable=AsyncMock) as set_button:
                set_button.return_value = True
                assert await configure_menu_button(bot, "https://mini.example.test")
                button = set_button.await_args.kwargs["menu_button"]
                assert isinstance(button, MenuButtonWebApp)
                assert button.web_app.url == "https://mini.example.test"
            with patch.object(Message, "answer", new_callable=AsyncMock) as answer, \
                 patch.object(CallbackQuery, "answer", new_callable=AsyncMock):
                await dispatcher.feed_update(bot, incoming(99001, 703, "/start"),
                                             registration_factory=factory,
                                             webapp_url="https://mini.example.test")
                assert answer.await_count == 2
                keyboard = answer.await_args_list[0].kwargs["reply_markup"]
                assert isinstance(keyboard, ReplyKeyboardMarkup)
                assert keyboard.keyboard[0][0].web_app.url == "https://mini.example.test"
                assert set(MENU).issubset({item.text for row in keyboard.keyboard for item in row})
                inline_button = answer.await_args_list[1].kwargs["reply_markup"].inline_keyboard[0][0]
                assert inline_button.web_app.url == "https://mini.example.test"
                for offset, label in enumerate(MENU):
                    if label == "Создать задачу":
                        continue
                    await dispatcher.feed_update(bot, incoming(99010 + offset, 703, label),
                                                 registration_factory=factory)
                    assert answer.await_args.args[0]
                await dispatcher.feed_update(bot, incoming(99100, 703, "Создать задачу"),
                                             registration_factory=factory)
                await dispatcher.feed_update(bot, pressed(99101, 703, f"ui:newproj:{project_id}"),
                                             registration_factory=factory)
                for update_id, value in ((99102, "Bot lesson"), (99103, "Description"),
                                         (99104, "2027-01-01")):
                    await dispatcher.feed_update(bot, incoming(update_id, 703, value),
                                                 registration_factory=factory)
                for update_id, value in ((99105, "ui:priority:HIGH"),
                                         (99106, "ui:type:DEVELOPMENT"),
                                         (99107, f"ui:assignee:{users[704]}"),
                                         (99108, f"ui:qa:{users[705]}")):
                    await dispatcher.feed_update(bot, pressed(update_id, 703, value),
                                                 registration_factory=factory)
                confirm = answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
                await dispatcher.feed_update(bot, pressed(99109, 703, confirm),
                                             registration_factory=factory)
                async with factory() as db:
                    task = await db.scalar(select(Task).where(Task.title == "Bot lesson"))
                    assert task is not None and task.status == "OPEN" and task.assignee_id == users[704]
                    assert task.qa_reviewer_id == users[705]
                    assert len((await db.scalars(select(TaskEvent).where(
                        TaskEvent.task_id == task.id))).all()) == 1
                await dispatcher.feed_update(bot, pressed(99112, 703, "ui:filters"),
                                             registration_factory=factory)
                await dispatcher.feed_update(bot, pressed(99113, 703, "ui:f:s:OPEN:first"),
                                             registration_factory=factory)
                assert "Bot lesson" in str(answer.await_args.kwargs["reply_markup"])
                await dispatcher.feed_update(bot, pressed(99110, 704, f"ui:act:accept:{task.id}"),
                                             registration_factory=factory)
                async with factory() as db:
                    assert (await db.get(Task, task.id)).status == "ACCEPTED"
                await register_pending(factory, 803, 80300)
                await dispatcher.feed_update(bot, incoming(99111, 803, "Все задачи"),
                                             registration_factory=factory)
                assert "после одобрения" in answer.await_args.args[0]
        finally:
            await bot.session.close()
            await engine.dispose()
    asyncio.run(scenario())


def test_pending_start_rejected_start_and_http_approval_share_registration_rules():
    async def scenario():
        engine, app, factory, users, project_id, _ = await fixture()
        bot = Bot(token=TOKEN)
        dispatcher = build_dispatcher()
        try:
            await register_pending(factory, 804, 80400)
            with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
                await dispatcher.feed_update(bot, incoming(80403, 804, "/start"),
                                             registration_factory=factory)
                assert "запрашиваемую роль" in answer.await_args.args[0]
                assert all("SUPER_ADMIN" not in button.callback_data
                           for row in answer.await_args.kwargs["reply_markup"].inline_keyboard
                           for button in row)
            assert await submit_access_request(factory, update_id=80404,
                                               telegram_user_id=804, role="SUPER_ADMIN",
                                               project_id=project_id) == "INVALID_ROLE"
            assert await submit_access_request(factory, update_id=80405,
                                               telegram_user_id=804, role="QA",
                                               project_id=project_id) == "REQUEST_SUBMITTED"
            assert await submit_access_request(factory, update_id=80405,
                                               telegram_user_id=804, role="QA",
                                               project_id=project_id) == "REQUEST_SUBMITTED"
            assert await submit_access_request(factory, update_id=80406,
                                               telegram_user_id=804, role="PM",
                                               project_id=None) == "REQUEST_ALREADY_SUBMITTED"
            async with factory() as db:
                target = await db.scalar(select(User).where(User.telegram_user_id == 804))
                assert len((await db.scalars(select(AuditLog).where(
                    AuditLog.action == "registration.requested",
                    AuditLog.target_id == str(target.id)))).all()) == 1
            await register_pending(factory, 805, 80500)
            async with factory() as db:
                async with db.begin():
                    rejected = await db.scalar(select(User).where(User.telegram_user_id == 805))
                    rejected.status = "REJECTED"
            with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
                await dispatcher.feed_update(bot, incoming(80503, 805, "/start"),
                                             registration_factory=factory)
                assert "отклонена" in answer.await_args.args[0]
                await dispatcher.feed_update(bot, incoming(80504, 805, "Все задачи"),
                                             registration_factory=factory)
                assert "после одобрения" in answer.await_args.args[0]
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                             base_url="http://localhost:8000") as client:
                    csrf = await login(client, 701)
                    pending = (await client.get("/api/v1/users/pending")).json()
                    candidate = next(row for row in pending if row["telegram_user_id"] == 804)
                    assert candidate["requested_role"] == "QA"
                    assert candidate["requested_project_id"] == str(project_id)
                    result = await client.patch(f"/api/v1/users/{candidate['id']}/status",
                        json={"status": "ACTIVE", "roles": ["QA"],
                              "project_id": str(project_id),
                              "expected_version": candidate["version"]},
                        headers={"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid4())})
                    assert result.status_code == 200, result.text
                    async with factory() as db:
                        assert await db.get(ProjectMember, (project_id, UUID(candidate["id"]))) is not None
        finally:
            await bot.session.close()
            await engine.dispose()
    asyncio.run(scenario())


def test_chat_assignment_and_qa_buttons_enforce_existing_roles():
    async def scenario():
        engine, _, factory, users, project_id, _ = await fixture()
        bot = Bot(token=TOKEN)
        dispatcher = build_dispatcher()
        try:
            async with factory() as db:
                async with db.begin():
                    author = await db.get(User, users[703])
                    task = await create_task(db, author, project_id=project_id,
                                             title="Unassigned", description="",
                                             assignee_id=None, qa_reviewer_id=None,
                                             priority="MEDIUM", type="DEVELOPMENT",
                                             due_date=date(2027, 1, 1))
                    task_id, key = task.id, task.task_key
            with patch.object(Message, "answer", new_callable=AsyncMock), \
                 patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as answer:
                await dispatcher.feed_update(bot, pressed(99201, 703,
                    f"ui:assign:{key}:{users[704]}"), registration_factory=factory)
                async with factory() as db:
                    assert (await db.get(Task, task_id)).assignee_id is None
                assert answer.await_args.kwargs.get("show_alert") is True
                await dispatcher.feed_update(bot, pressed(99202, 702,
                    f"ui:assign:{key}:{users[704]}"), registration_factory=factory)
                await dispatcher.feed_update(bot, pressed(99203, 703,
                    f"ui:qa:{key}:{users[705]}"), registration_factory=factory)
                async with factory() as db:
                    current = await db.get(Task, task_id)
                    assert current.assignee_id == users[704]
                    assert current.qa_reviewer_id == users[705]
                    assert len((await db.scalars(select(TaskEvent).where(
                        TaskEvent.task_id == task_id))).all()) == 3
        finally:
            await bot.session.close()
            await engine.dispose()
    asyncio.run(scenario())


def test_project_task_buttons_page_without_exposing_foreign_project():
    async def scenario():
        engine, _, factory, users, project_id, _ = await fixture()
        bot = Bot(token=TOKEN)
        dispatcher = build_dispatcher()
        try:
            async with factory() as db:
                async with db.begin():
                    author = await db.get(User, users[703])
                    for index in range(11):
                        await create_task(db, author, project_id=project_id,
                                          title=f"Paged {index}", description="",
                                          assignee_id=None, qa_reviewer_id=None,
                                          priority="MEDIUM", type="DEVELOPMENT",
                                          due_date=date(2027, 1, 1))
            with patch.object(Message, "answer", new_callable=AsyncMock) as answer, \
                 patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as acknowledge:
                await dispatcher.feed_update(bot, pressed(99301, 703,
                    f"ui:projecttasks:{project_id}"), registration_factory=factory)
                first_buttons = answer.await_args.kwargs["reply_markup"].inline_keyboard
                assert len(first_buttons) == 11
                next_data = first_buttons[-1][0].callback_data
                assert next_data.startswith("ui:pt:") and len(next_data) <= 64
                first_ids = {row[0].callback_data for row in first_buttons[:-1]}
                await dispatcher.feed_update(bot, pressed(99302, 703, next_data),
                                             registration_factory=factory)
                second_buttons = answer.await_args.kwargs["reply_markup"].inline_keyboard
                assert len(second_buttons) == 1
                assert second_buttons[0][0].callback_data not in first_ids
                answer.reset_mock()
                await dispatcher.feed_update(bot, pressed(99303, 706, next_data),
                                             registration_factory=factory)
                answer.assert_not_awaited()
                assert acknowledge.await_args.kwargs.get("show_alert") is True
        finally:
            await bot.session.close()
            await engine.dispose()
    asyncio.run(scenario())
