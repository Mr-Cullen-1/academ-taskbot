"""Phase 4 runs against disposable SQLite and fake transports only."""

import asyncio
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, Update, User as TelegramUser
from sqlalchemy import delete, select

from taskbot_api.config import Settings
from taskbot_api.models import Notification, NotificationAttempt, ProjectMember, Task, TaskEvent, User, UserRole, WeeklyReport, now
from taskbot_bot.__main__ import build_dispatcher
from taskbot_api.reporting import TASHKENT, build_weekly_report, report_due, report_week
from taskbot_api.task_service import create_task
from taskbot_worker.delivery import (
    claim_notification, deliver_notifications, process_outbox,
)
from taskbot_worker.scheduler import (
    add_work_minutes, schedule_deadlines, schedule_reaction_reminders, schedule_weekly_reports,
)
from test_tasks import TOKEN, act, fixture, headers, login, new_task


class FakeTransport:
    def __init__(self, fail=0):
        self.sent = []
        self.fail = fail

    async def send(self, chat_id, body, keyboard=None):
        if self.fail:
            self.fail -= 1
            raise TimeoutError("mock timeout")
        self.sent.append((chat_id, body, keyboard))


def settings(**changes):
    return Settings(_env_file=None, database_url=None, bot_token=TOKEN,
                    notification_delivery_enabled=True, notification_transport="telegram",
                    **changes)


async def create_example(factory, users, project, *, due_date=date(2027, 1, 1)):
    async with factory() as db:
        async with db.begin():
            author = await db.get(User, users[703])
            task = await create_task(db, author, project_id=project, title="Lesson",
                                     description="Work", assignee_id=users[704], qa_reviewer_id=users[705],
                                     priority="HIGH", type="DEVELOPMENT", due_date=due_date)
            task_id = task.id
    return task_id


def test_outbox_idempotency_retry_recovery_and_private_api():
    async def scenario():
        engine, app, factory, users, project, _ = await fixture()
        try:
            task_id = await create_example(factory, users, project)
            instant = now() + timedelta(seconds=2)
            assert await process_outbox(factory, at=instant) == 1
            assert await process_outbox(factory, at=instant) == 0
            async with factory() as db:
                rows = (await db.scalars(select(Notification).where(Notification.task_id == task_id))).all()
                assert len(rows) == 1 and rows[0].recipient_user_id == users[704]
            fake = FakeTransport(fail=1)
            assert await deliver_notifications(factory, fake, settings(), at=instant) == 1
            async with factory() as db:
                row = await db.scalar(select(Notification).where(Notification.task_id == task_id))
                assert row.state == "RETRY" and row.attempts == 1
                assert row.last_error_code == "TimeoutError"
            assert await deliver_notifications(factory, fake, settings(), at=instant + timedelta(seconds=31)) == 1
            assert len(fake.sent) == 1
            assert fake.sent[0][2].inline_keyboard[0][0].text == "Принять"
            assert await deliver_notifications(factory, fake, settings(), at=instant + timedelta(seconds=32)) == 0
            async with factory() as db:
                attempts = (await db.scalars(select(NotificationAttempt))).all()
                assert [a.result for a in attempts] == ["RETRY", "SENT"]
            # A lease left by a crashed worker becomes claimable again.
            async with factory() as db:
                async with db.begin():
                    row = await db.scalar(select(Notification).where(Notification.task_id == task_id))
                    row.state = "PENDING"
                    row.sent_at = None
            claimed = await claim_notification(factory, at=instant + timedelta(minutes=2), lease_seconds=10)
            assert claimed and claimed[3] is True
            assert await deliver_notifications(factory, fake, settings(),
                                               at=instant + timedelta(minutes=2, seconds=11)) == 1
            async with factory() as db:
                row = await db.scalar(select(Notification).where(Notification.task_id == task_id))
                assert row.state == "SENT" and row.attempts == 3
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as worker, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as author:
                    await login(worker, 704)
                    await login(author, 703)
                    assert len((await worker.get("/api/v1/notifications")).json()) == 1
                    assert (await author.get("/api/v1/notifications")).json() == []
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_review_request_is_skipped_if_task_was_approved_before_delivery():
    async def scenario():
        engine, app, factory, users, project, _ = await fixture()
        try:
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as author, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as worker:
                    a, w = await login(author, 703), await login(worker, 704)
                    task = await new_task(author, a, project, users[704])
                    for client, csrf, action in ((worker, w, "accept"),
                                                 (worker, w, "start"),
                                                 (worker, w, "submit-review")):
                        response = await act(client, csrf, task, action)
                        assert response.status_code == 200
                        task = response.json()
                    assert await process_outbox(factory, at=now() + timedelta(seconds=2)) == 4
                    approved = await act(author, a, task, "approve")
                    assert approved.status_code == 200
                fake = FakeTransport()
                await deliver_notifications(factory, fake, settings(), at=now() + timedelta(seconds=3))
                async with factory() as db:
                    review = await db.scalar(select(Notification).where(
                        Notification.task_id == UUID(task["id"]), Notification.kind == "REVIEW_REQUESTED"))
                    assert review is not None and review.state == "SKIPPED"
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_qa_review_notification_requires_current_qa_role():
    async def scenario():
        engine, app, factory, users, project, _ = await fixture()
        try:
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as author, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as worker:
                    a, w = await login(author, 703), await login(worker, 704)
                    task = await new_task(author, a, project, users[704], users[705])
                    for action in ("accept", "start", "submit-review"):
                        response = await act(worker, w, task, action)
                        assert response.status_code == 200
                        task = response.json()
                    assert await process_outbox(factory, at=now() + timedelta(seconds=2)) == 4
                async with factory() as db:
                    async with db.begin():
                        await db.execute(delete(UserRole).where(
                            UserRole.user_id == users[705], UserRole.code == "QA"))
                        db.add(UserRole(user_id=users[705], code="SUBJECT_DEVELOPER"))
                fake = FakeTransport()
                await deliver_notifications(factory, fake, settings(), at=now() + timedelta(seconds=3))
                async with factory() as db:
                    row = await db.scalar(select(Notification).where(
                        Notification.task_id == UUID(task["id"]),
                        Notification.kind == "QA_REVIEW_REQUESTED"))
                    assert row is not None and row.state == "SKIPPED"
                    assert all(chat_id != 705 for chat_id, _, _ in fake.sent)
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_reassignment_back_skips_old_assignment_notifications():
    async def scenario():
        engine, app, factory, users, project, _ = await fixture()
        try:
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as author, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as pm:
                    a, p = await login(author, 703), await login(pm, 702)
                    task = await new_task(author, a, project, users[704])
                    for assignee in (users[705], users[704]):
                        changed = await pm.patch(f"/api/v1/tasks/{task['id']}",
                                                 headers=headers(p), json={
                                                     "expected_version": task["version"],
                                                     "assignee_id": str(assignee)})
                        assert changed.status_code == 200
                        task = changed.json()
                    assert await process_outbox(factory, at=now() + timedelta(seconds=2)) == 3
                fake = FakeTransport()
                await deliver_notifications(factory, fake, settings(), at=now() + timedelta(seconds=3))
                async with factory() as db:
                    assignments = (await db.scalars(select(Notification).where(
                        Notification.task_id == UUID(task["id"]),
                        Notification.kind == "ASSIGNED"))).all()
                    assert len(assignments) == 3
                    assert sorted(row.state for row in assignments) == ["SENT", "SKIPPED", "SKIPPED"]
                    assert [chat_id for chat_id, _, _ in fake.sent].count(704) == 1
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_deadline_reminders_reschedule_and_excluded_statuses():
    async def scenario():
        engine, _, factory, users, project, _ = await fixture()
        try:
            task_id = await create_example(factory, users, project, due_date=date(2026, 10, 16))
            instant = datetime(2026, 10, 15, 18, tzinfo=timezone.utc)
            async with factory() as db:
                async with db.begin():
                    task = await db.get(Task, task_id)
                    task.created_at = instant - timedelta(days=2)
            assert await schedule_deadlines(factory, at=instant) == 6
            assert await schedule_deadlines(factory, at=instant) == 0
            async with factory() as db:
                rows = (await db.scalars(select(Notification).where(Notification.task_id == task_id))).all()
                assert {row.kind for row in rows} == {"DUE_24H", "DUE_2H", "OVERDUE"}
                assert len(rows) == 6
                assert {row.scheduled_at.replace(tzinfo=timezone.utc) for row in rows
                        if row.kind == "DUE_24H"} == {datetime(2026, 10, 15, 19, tzinfo=timezone.utc)}
                assert {row.scheduled_at.replace(tzinfo=timezone.utc) for row in rows
                        if row.kind == "DUE_2H"} == {datetime(2026, 10, 16, 17, tzinfo=timezone.utc)}
            async with factory() as db:
                async with db.begin():
                    task = await db.get(Task, task_id)
                    task.current_due_date = date(2026, 10, 18)
            assert await schedule_deadlines(factory, at=instant) == 6
            async with factory() as db:
                rows = (await db.scalars(select(Notification).where(Notification.task_id == task_id))).all()
                assert len([row for row in rows if row.state == "SKIPPED"]) == 6
            for excluded in ("REVIEW", "REJECTED", "DONE", "CANCELLED"):
                async with factory() as db:
                    async with db.begin():
                        task = await db.get(Task, task_id)
                        task.status = excluded
                assert await schedule_deadlines(factory, at=instant) == 0
                async with factory() as db:
                    rows = (await db.scalars(select(Notification).where(Notification.task_id == task_id))).all()
                    assert all(row.state == "SKIPPED" for row in rows)
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_working_calendar_and_reaction_stop():
    config = settings()
    saturday = datetime(2026, 10, 17, 18, tzinfo=TASHKENT)
    assert add_work_minutes(saturday, 240, config).astimezone(TASHKENT) == \
        datetime(2026, 10, 19, 13, tzinfo=TASHKENT)
    async def scenario():
        engine, _, factory, users, project, _ = await fixture()
        try:
            task_id = await create_example(factory, users, project)
            async with factory() as db:
                async with db.begin():
                    event = await db.scalar(select(TaskEvent).where(TaskEvent.task_id == task_id))
                    event.occurred_at = saturday.astimezone(timezone.utc)
            trigger = add_work_minutes(saturday, 240, config)
            assert await schedule_reaction_reminders(factory, config, at=saturday.astimezone(timezone.utc)) == 2
            fake = FakeTransport()
            async with factory() as db:
                reminder = await db.scalar(select(Notification).where(Notification.kind == "NO_REACTION"))
                alert = await db.scalar(select(Notification).where(Notification.kind == "NO_RESPONSE_ALERT"))
                assert reminder.scheduled_at.replace(tzinfo=timezone.utc) == trigger
                assert alert.scheduled_at.replace(tzinfo=timezone.utc) == \
                    saturday.astimezone(timezone.utc) + timedelta(hours=1)
            async with factory() as db:
                async with db.begin():
                    task = await db.get(Task, task_id)
                    task.status = "ACCEPTED"
            assert await deliver_notifications(factory, fake, config, at=trigger) == 2
            assert fake.sent == []
            async with factory() as db:
                rows = (await db.scalars(select(Notification).where(
                    Notification.kind.in_(["NO_REACTION", "NO_RESPONSE_ALERT"])))).all()
                assert len(rows) == 2 and all(row.state == "SKIPPED" for row in rows)
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_one_hour_alert_to_author_and_project_team_leads_once():
    async def scenario():
        engine, _, factory, users, project, _ = await fixture()
        try:
            task_id = await create_example(factory, users, project)
            async with factory() as db:
                async with db.begin():
                    db.add_all([UserRole(user_id=users[703], code="TEAM_LEAD"),
                                UserRole(user_id=users[708], code="TEAM_LEAD"),
                                UserRole(user_id=users[706], code="TEAM_LEAD")])
            assert await schedule_reaction_reminders(factory, settings()) == 3
            assert await schedule_reaction_reminders(factory, settings()) == 0
            async with factory() as db:
                alerts = (await db.scalars(select(Notification).where(
                    Notification.task_id == task_id, Notification.kind == "NO_RESPONSE_ALERT"))).all()
                assert {row.recipient_user_id for row in alerts} == {users[703], users[708]}
                assert users[702] not in {row.recipient_user_id for row in alerts}
                first_due = min(row.scheduled_at.replace(tzinfo=timezone.utc) for row in alerts)
            fake = FakeTransport()
            assert await deliver_notifications(factory, fake, settings(), at=first_due - timedelta(seconds=1)) == 0
            assert await deliver_notifications(factory, fake, settings(), at=first_due) == 2
            assert {chat_id for chat_id, _, _ in fake.sent} == {703, 708}
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_super_admin_author_without_membership_receives_task_alerts():
    async def scenario():
        engine, _, factory, users, project, _ = await fixture()
        try:
            async with factory() as db:
                async with db.begin():
                    admin = await db.get(User, users[701])
                    task = await create_task(db, admin, project_id=project, title="Admin task",
                                             description="Work", assignee_id=users[704],
                                             qa_reviewer_id=None, priority="HIGH",
                                             type="DEVELOPMENT", due_date=date(2027, 1, 1))
                    task_id = task.id
            assert await schedule_reaction_reminders(factory, settings()) == 2
            assert await schedule_deadlines(factory) == 6
            async with factory() as db:
                rows = (await db.scalars(select(Notification).where(Notification.task_id == task_id))).all()
                assert any(row.kind == "NO_RESPONSE_ALERT" and row.recipient_user_id == users[701]
                           for row in rows)
                assert len([row for row in rows if row.kind.startswith("DUE_") and
                            row.recipient_user_id == users[701]]) == 2
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_assignment_buttons_accept_and_reasoned_refusal_without_telegram_calls():
    async def scenario():
        engine, _, factory, users, project, _ = await fixture()
        bot = Bot(token=TOKEN)
        try:
            accepted_id = await create_example(factory, users, project)
            rejected_id = await create_example(factory, users, project)
            dispatcher = build_dispatcher()
            sender = TelegramUser(id=704, is_bot=False, first_name="Worker")
            author_sender = TelegramUser(id=703, is_bot=False, first_name="Author")
            bot_user = TelegramUser(id=999, is_bot=True, first_name="Taskbot")
            chat = Chat(id=704, type="private")
            sent_message = Message(message_id=1, date=now(), chat=chat, from_user=bot_user,
                                   text="Новая задача")
            def button(update_id, task_id, action, actor=sender):
                query = CallbackQuery(id=str(update_id), from_user=actor, chat_instance="local",
                                      message=sent_message, data=f"task:{action}:{task_id}")
                return Update(update_id=update_id, callback_query=query)
            with patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as ack, \
                 patch.object(Message, "answer", new_callable=AsyncMock) as answer, \
                 patch.object(Message, "edit_reply_markup", new_callable=AsyncMock) as edit:
                await dispatcher.feed_update(bot, button(9100, accepted_id, "accept", author_sender),
                                             registration_factory=factory)
                async with factory() as db:
                    assert (await db.get(Task, accepted_id)).status == "OPEN"
                await dispatcher.feed_update(bot, button(9101, accepted_id, "accept"), registration_factory=factory)
                await dispatcher.feed_update(bot, button(9101, accepted_id, "accept"), registration_factory=factory)
                assert edit.await_count == 1
                await dispatcher.feed_update(bot, button(9102, rejected_id, "reject"), registration_factory=factory)
                assert "причиной отказа" in answer.await_args.args[0]
                prompt = Message(message_id=2, date=now(), chat=chat, from_user=bot_user,
                                 text="Ответьте на это сообщение причиной отказа от TASK-1002:")
                reply = Message(message_id=3, date=now(), chat=chat, from_user=sender,
                                reply_to_message=prompt, text="Не моя специализация")
                await dispatcher.feed_update(bot, Update(update_id=9103, message=reply), registration_factory=factory)
                assert "Отказ сохранён" in answer.await_args.args[0]
                assert ack.await_count == 4
            async with factory() as db:
                accepted = await db.get(Task, accepted_id)
                rejected = await db.get(Task, rejected_id)
                assert accepted.status == "ACCEPTED" and rejected.status == "REJECTED"
                events = (await db.scalars(select(TaskEvent).where(TaskEvent.task_id == rejected_id))).all()
                assert events[-1].after_data["reason"] == "Не моя специализация"
        finally:
            await bot.session.close()
            await engine.dispose()
    asyncio.run(scenario())


def test_week_boundaries_across_month():
    monday = datetime(2026, 11, 2, 9, tzinfo=TASHKENT)
    start, begin, end = report_week(monday)
    assert start == date(2026, 10, 26)
    assert begin.astimezone(TASHKENT) == datetime(2026, 10, 26, tzinfo=TASHKENT)
    assert end.astimezone(TASHKENT) == datetime(2026, 11, 2, tzinfo=TASHKENT)
    assert report_due(monday)
    assert not report_due(monday - timedelta(minutes=1))
    assert report_due(monday + timedelta(days=1))


def test_weekly_report_metrics_and_date_snapshot():
    async def scenario():
        engine, _, factory, users, project, _ = await fixture()
        try:
            instant = datetime(2026, 11, 2, 9, tzinfo=TASHKENT)
            done_id = await create_example(factory, users, project, due_date=date(2026, 10, 30))
            open_id = await create_example(factory, users, project, due_date=date(2026, 11, 1))
            async with factory() as db:
                async with db.begin():
                    finished = await db.get(Task, done_id)
                    finished.created_at = datetime(2026, 10, 31, 23, 30, tzinfo=TASHKENT)
                    finished.completed_at = datetime(2026, 10, 30, 12, tzinfo=TASHKENT)
                    finished.status = "DONE"
                    finished.version = 2
                    db.add(TaskEvent(task_id=done_id, sequence=2, actor_user_id=users[703],
                                     type="APPROVE", before_data={},
                                     after_data={"original_due_date": "2026-10-30",
                                                 "current_due_date": "2026-10-30"},
                                     approval_mode="AUTHOR", occurred_at=finished.completed_at))
                    waiting = await db.get(Task, open_id)
                    waiting.created_at = datetime(2026, 11, 2, 0, tzinfo=TASHKENT)
                    recipient = await db.get(User, users[701])
            async with factory() as db:
                report = await build_weekly_report(db, recipient, at=instant)
                assert report["week_start"] == "2026-10-26"
                assert report["week_end_exclusive"] == "2026-11-02"
                assert report["totals"]["created"] == 1
                assert report["totals"]["completed"] == 1
                assert report["totals"]["accepted_by_author"] == 1
                assert report["totals"]["on_time_current"] == 1
                assert report["totals"]["on_time_original"] == 1
                assert report["totals"]["current_overdue"] == 1
                assert report["totals"]["awaiting_assignee_response"] == 1
                assert len(report["projects"]) == 2
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_report_delivery_scope_and_api():
    async def scenario():
        engine, app, factory, users, project, _ = await fixture()
        try:
            instant = datetime(2026, 11, 2, 9, tzinfo=TASHKENT)
            async with factory() as db:
                async with db.begin():
                    db.add(UserRole(user_id=users[703], code="TEAM_LEAD"))
            assert await schedule_weekly_reports(factory, at=instant - timedelta(minutes=1)) == 0
            assert await schedule_weekly_reports(factory, at=instant) == 2
            assert await schedule_weekly_reports(factory, at=instant + timedelta(minutes=1)) == 0
            async with factory() as db:
                saved = (await db.scalars(select(WeeklyReport))).all()
                assert {row.recipient_user_id for row in saved} == {users[701], users[703]}
            fake = FakeTransport()
            assert await deliver_notifications(factory, fake, settings(), at=instant) == 2
            assert len(fake.sent) == 2
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as admin, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as lead, \
                           httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as pm:
                    await login(admin, 701)
                    await login(lead, 703)
                    await login(pm, 702)
                    params = {"week_start": "2026-10-26"}
                    with patch("taskbot_api.notification_routes.now", return_value=instant):
                        assert len((await admin.get("/api/v1/reports/weekly", params=params)).json()["projects"]) == 2
                        lead_report = await lead.get("/api/v1/reports/weekly", params=params)
                        assert lead_report.status_code == 200
                        assert [row["project_id"] for row in lead_report.json()["projects"]] == [str(project)]
                        assert (await pm.get("/api/v1/reports/weekly", params=params)).status_code == 403
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_weekly_report_is_not_sent_after_team_lead_loses_project_access():
    async def scenario():
        engine, _, factory, users, project, _ = await fixture()
        try:
            instant = datetime(2026, 11, 2, 9, tzinfo=TASHKENT)
            async with factory() as db:
                async with db.begin():
                    db.add(UserRole(user_id=users[703], code="TEAM_LEAD"))
            assert await schedule_weekly_reports(factory, at=instant) == 2
            async with factory() as db:
                async with db.begin():
                    await db.delete(await db.get(ProjectMember, (project, users[703])))
            fake = FakeTransport()
            assert await deliver_notifications(factory, fake, settings(), at=instant) == 2
            assert {chat_id for chat_id, _, _ in fake.sent} == {701}
            async with factory() as db:
                skipped = await db.scalar(select(Notification).where(
                    Notification.recipient_user_id == users[703],
                    Notification.kind == "WEEKLY_REPORT"))
                assert skipped.state == "SKIPPED"
        finally:
            await engine.dispose()
    asyncio.run(scenario())
