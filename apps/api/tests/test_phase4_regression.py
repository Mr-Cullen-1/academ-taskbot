"""Worker regressions use a disposable SQLite database and fake transport."""

import asyncio
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from taskbot_api.models import (Notification, OutboxEvent, Project, ProjectMember,
                                Task, TaskEvent, User, WeeklyReport, now)
from taskbot_api.reporting import TASHKENT
from taskbot_api.task_service import add_comment, create_task, set_qa_reviewer
from taskbot_worker.delivery import (DryRunTransport, claim_notification, deliver_notifications,
                                     finish_notification, process_outbox,
                                     put_notification)
from taskbot_worker.scheduler import (schedule_deadlines,
                                      schedule_reaction_reminders,
                                      schedule_weekly_reports)
from test_phase4 import FakeTransport, create_example, settings
from test_tasks import fixture


DEMO_PROJECT_ID = UUID("6aa4b085-3cff-4a7b-9a75-7893f5cbea30")


def test_demo_is_processed_like_every_other_project():
    async def scenario():
        engine, _, factory, users, project_id, _ = await fixture()
        try:
            async with factory() as db:
                async with db.begin():
                    db.add(Project(id=DEMO_PROJECT_ID, name="DEMO", description="",
                                   owner_user_id=users[701]))
                    for number in (703, 704, 705):
                        db.add(ProjectMember(project_id=DEMO_PROJECT_ID,
                                             user_id=users[number]))
            demo_task = await create_example(factory, users, DEMO_PROJECT_ID,
                                             due_date=date(2026, 11, 1))
            real_task = await create_example(factory, users, project_id,
                                             due_date=date(2026, 11, 1))
            instant = datetime(2026, 11, 2, 9, tzinfo=TASHKENT)
            assert await process_outbox(factory, at=instant) == 2
            # Advance reminders have passed; each project gets two overdue rows.
            assert await schedule_deadlines(factory, at=instant) == 4
            assert await schedule_reaction_reminders(factory, settings(), at=instant) == 4
            assert await schedule_weekly_reports(factory, at=instant) == 2
            assert await process_outbox(factory, at=instant) == 0
            assert await schedule_deadlines(factory, at=instant) == 0
            assert await schedule_reaction_reminders(factory, settings(), at=instant) == 0
            assert await schedule_weekly_reports(factory, at=instant) == 0
            fake = FakeTransport()
            await deliver_notifications(factory, fake, settings(), at=instant)
            async with factory() as db:
                demo = await db.get(Task, demo_task)
                assert demo.status == "OPEN"
                demo_outbox = await db.scalar(select(OutboxEvent).join(
                    TaskEvent, TaskEvent.id == OutboxEvent.source_event_id).where(
                    TaskEvent.task_id == demo_task))
                assert demo_outbox.state == "PROCESSED"
                demo_notifications = (await db.scalars(select(Notification).where(
                    Notification.task_id == demo_task))).all()
                assert {row.kind for row in demo_notifications} == {
                    "ASSIGNED", "OVERDUE", "NO_REACTION", "NO_RESPONSE_ALERT"}
                assert all(row.state == "SENT" for row in demo_notifications)
                assert (await db.scalars(select(Notification).where(
                    Notification.task_id == real_task))).first() is not None
                reports = (await db.scalars(select(WeeklyReport))).all()
                assert len(reports) == 2
                admin_report = next(row for row in reports if row.recipient_user_id == users[701])
                assert str(DEMO_PROJECT_ID) in {
                    row["project_id"] for row in admin_report.payload["projects"]}
                assert admin_report.payload["totals"]["current_overdue"] == 2
                assert any("DEMO" in body for _, body, _ in fake.sent)
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_abandoned_lease_cannot_finalize_newer_claim():
    async def scenario():
        engine, _, factory, users, project_id, _ = await fixture()
        try:
            task_id = await create_example(factory, users, project_id)
            instant = now() + timedelta(seconds=2)
            assert await process_outbox(factory, at=instant) == 1
            first = await claim_notification(factory, at=instant, lease_seconds=10,
                                             max_attempts=3)
            second = await claim_notification(factory, at=instant + timedelta(seconds=11),
                                              lease_seconds=10, max_attempts=3)
            assert first is not None and second is not None
            assert first[0] == second[0] and first[5] == 1 and second[5] == 2
            await finish_notification(factory, first[0], at=instant + timedelta(seconds=12),
                                      outcome="SENT", error_code=None,
                                      claim_attempt=first[5], max_attempts=3,
                                      retry_base_seconds=30)
            async with factory() as db:
                row = await db.scalar(select(Notification).where(Notification.task_id == task_id))
                assert row.state == "SENDING" and row.attempts == 2
            await finish_notification(factory, second[0], at=instant + timedelta(seconds=12),
                                      outcome="SENT", error_code=None,
                                      claim_attempt=second[5], max_attempts=3,
                                      retry_base_seconds=30)
            async with factory() as db:
                row = await db.scalar(select(Notification).where(Notification.task_id == task_id))
                assert row.state == "SENT" and row.attempts == 2
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_invalid_outbox_is_failed_and_next_valid_event_proceeds():
    async def scenario():
        engine, _, factory, users, project_id, _ = await fixture()
        try:
            task_id = await create_example(factory, users, project_id)
            async with factory() as db:
                async with db.begin():
                    event = await db.scalar(select(TaskEvent).where(TaskEvent.task_id == task_id))
                    bad = OutboxEvent(aggregate_type="task", aggregate_id=str(task_id),
                                      source_event_id=event.id, kind="INVALID",
                                      payload={"task_id": str(task_id)},
                                      dedupe_key=f"bad:{uuid4()}",
                                      available_at=now() - timedelta(days=1))
                    db.add(bad)
                    await db.flush()
                    bad_id = bad.id
            assert await process_outbox(factory, at=now() + timedelta(seconds=2)) == 1
            async with factory() as db:
                assert (await db.get(OutboxEvent, bad_id)).state == "FAILED"
                assert len((await db.scalars(select(Notification).where(
                    Notification.task_id == task_id))).all()) == 1
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_unexpected_notification_integrity_error_is_not_hidden():
    async def scenario():
        engine, _, factory, _, _, _ = await fixture(enforce_foreign_keys=True)
        try:
            with pytest.raises(IntegrityError):
                async with factory() as db:
                    async with db.begin():
                        await put_notification(db, recipient_id=uuid4(), kind="TEST",
                                               key=f"test:{uuid4()}", body="Тест кириллицы",
                                               scheduled_at=now())
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_admin_approval_notifies_author_and_assignee_once_in_cyrillic():
    async def scenario():
        engine, _, factory, users, project_id, _ = await fixture()
        try:
            task_id = await create_example(factory, users, project_id)
            async with factory() as db:
                async with db.begin():
                    task = await db.get(Task, task_id)
                    task.status = "DONE"
                    task.title = "Готовый урок"
                    approval = TaskEvent(task_id=task_id, sequence=2,
                                         actor_user_id=users[701], type="ADMIN_APPROVE",
                                         before_data={"status": "REVIEW"},
                                         after_data={"status": "DONE",
                                                     "created_by": str(users[703]),
                                                     "assignee_id": str(users[704])},
                                         occurred_at=now())
                    db.add(approval)
                    await db.flush()
                    db.add(OutboxEvent(aggregate_type="task", aggregate_id=str(task_id),
                                       source_event_id=approval.id, kind="TASK_EVENT",
                                       payload={"task_id": str(task_id),
                                                "event_type": "ADMIN_APPROVE"},
                                       dedupe_key=f"task_event:{approval.id}",
                                       available_at=now()))
            assert await process_outbox(factory, at=now() + timedelta(seconds=2)) == 2
            fake = FakeTransport()
            await deliver_notifications(factory, fake, settings(),
                                        at=now() + timedelta(seconds=3))
            assert {chat for chat, _, _ in fake.sent} == {703, 704}
            assert all("Готовый урок" in body for _, body, _ in fake.sent)
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_disabled_delivery_preserves_queue_and_dry_run_is_simulated():
    async def scenario():
        engine, _, factory, users, project_id, _ = await fixture()
        try:
            task_id = await create_example(factory, users, project_id)
            instant = now() + timedelta(seconds=2)
            assert await process_outbox(factory, at=instant) == 1
            disabled = settings()
            disabled.notification_delivery_enabled = False
            assert await deliver_notifications(factory, DryRunTransport(), disabled,
                                               at=instant) == 0
            async with factory() as db:
                row = await db.scalar(select(Notification).where(Notification.task_id == task_id))
                assert row.state == "PENDING" and row.attempts == 0
            simulated = settings()
            simulated.notification_transport = "dry_run"
            assert await deliver_notifications(factory, DryRunTransport(), simulated,
                                               at=instant) == 1
            async with factory() as db:
                row = await db.scalar(select(Notification).where(Notification.task_id == task_id))
                assert row.state == "SIMULATED" and row.attempts == 1
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_deadline_change_does_not_cancel_in_flight_claim():
    async def scenario():
        engine, _, factory, users, project_id, _ = await fixture()
        try:
            task_id = await create_example(factory, users, project_id,
                                           due_date=date(2026, 11, 1))
            instant = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
            assert await schedule_deadlines(factory, at=instant) == 6
            async with factory() as db:
                async with db.begin():
                    old = await db.scalar(select(Notification).where(
                        Notification.task_id == task_id,
                        Notification.kind == "DUE_24H"))
                    old.state = "SENDING"
                    old.attempts = 1
                    old.lease_until = instant + timedelta(days=2)
                    old_id = old.id
                    task = await db.get(Task, task_id)
                    task.current_due_date = date(2026, 11, 2)
            assert await schedule_deadlines(factory, at=instant) == 6
            async with factory() as db:
                assert (await db.get(Notification, old_id)).state == "SENDING"
            # Once that lease expires, the claim is revalidated and skipped.
            claimed = await claim_notification(factory, at=instant + timedelta(hours=54),
                                               lease_seconds=10, max_attempts=3)
            assert claimed is not None and claimed[3] is False
            async with factory() as db:
                assert (await db.get(Notification, old_id)).state == "SKIPPED"
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_comment_uses_event_time_participants_after_reassignment():
    async def scenario():
        engine, _, factory, users, project_id, _ = await fixture()
        try:
            task_id = await create_example(factory, users, project_id)
            async with factory() as db:
                async with db.begin():
                    task = await db.get(Task, task_id)
                    author = await db.get(User, users[703])
                    await add_comment(db, author, task, "Проверьте готовый урок")
            async with factory() as db:
                async with db.begin():
                    task = await db.get(Task, task_id)
                    task.assignee_id = users[708]
            assert await process_outbox(factory, at=now() + timedelta(seconds=2)) == 2
            async with factory() as db:
                rows = (await db.scalars(select(Notification).where(
                    Notification.task_id == task_id,
                    Notification.kind == "TASK_COMMENT"))).all()
                assert {row.recipient_user_id for row in rows} == {users[704], users[705]}
            fake = FakeTransport()
            await deliver_notifications(factory, fake, settings(),
                                        at=now() + timedelta(seconds=3))
            assert {chat for chat, _, _ in fake.sent} == {705}
            assert all("TASK-" in body for _, body, _ in fake.sent)
            async with factory() as db:
                old = await db.scalar(select(Notification).where(
                    Notification.task_id == task_id,
                    Notification.kind == "TASK_COMMENT",
                    Notification.recipient_user_id == users[704]))
                assert old.state == "SKIPPED"
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_expired_lease_respects_max_attempts():
    async def scenario():
        engine, _, factory, users, project_id, _ = await fixture()
        try:
            task_id = await create_example(factory, users, project_id)
            instant = now() + timedelta(seconds=2)
            assert await process_outbox(factory, at=instant) == 1
            first = await claim_notification(factory, at=instant, lease_seconds=10,
                                             max_attempts=1)
            assert first is not None and first[3] is True
            expired = await claim_notification(factory, at=instant + timedelta(seconds=11),
                                               lease_seconds=10, max_attempts=1)
            assert expired is not None and expired[3] is False
            async with factory() as db:
                row = await db.scalar(select(Notification).where(Notification.task_id == task_id))
                assert row.state == "FAILED" and row.attempts == 1
                assert row.last_error_code == "LEASE_EXPIRED"
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_transient_outbox_failure_does_not_hold_up_other_events_and_recovers():
    async def scenario():
        engine, _, factory, users, project_id, _ = await fixture()
        try:
            first_task = await create_example(factory, users, project_id)
            second_task = await create_example(factory, users, project_id)
            instant = now() + timedelta(seconds=2)
            async with factory() as db:
                async with db.begin():
                    event = await db.scalar(select(TaskEvent).where(TaskEvent.task_id == first_task))
                    first = await db.scalar(select(OutboxEvent).where(
                        OutboxEvent.source_event_id == event.id))
                    first.available_at = instant - timedelta(days=1)
            from taskbot_worker.delivery import put_notification as real_put
            calls = 0

            async def fail_once(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("isolated simulated processing fault")
                return await real_put(*args, **kwargs)

            with patch("taskbot_worker.delivery.put_notification", side_effect=fail_once):
                assert await process_outbox(factory, at=instant, retry_base_seconds=30) == 1
            async with factory() as db:
                first = await db.scalar(select(OutboxEvent).join(TaskEvent).where(
                    TaskEvent.task_id == first_task))
                second = await db.scalar(select(OutboxEvent).join(TaskEvent).where(
                    TaskEvent.task_id == second_task))
                assert first.state == "UNPROCESSED" and first.attempts == 1
                assert first.last_error_code == "RuntimeError"
                assert second.state == "PROCESSED"
                assert not (await db.scalars(select(Notification).where(
                    Notification.task_id == first_task))).all()
            assert await process_outbox(factory, at=instant + timedelta(seconds=31)) == 1
            assert await process_outbox(factory, at=instant + timedelta(seconds=31)) == 0
            async with factory() as db:
                first = await db.scalar(select(OutboxEvent).join(TaskEvent).where(
                    TaskEvent.task_id == first_task))
                assert first.state == "PROCESSED"
                assert len((await db.scalars(select(Notification).where(
                    Notification.task_id == first_task))).all()) == 1
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_persistent_outbox_fault_is_terminal_after_limit():
    async def scenario():
        engine, _, factory, users, project_id, _ = await fixture()
        try:
            task_id = await create_example(factory, users, project_id)
            instant = now() + timedelta(seconds=2)
            async def always_fail(*args, **kwargs):
                raise RuntimeError("isolated persistent fault")
            with patch("taskbot_worker.delivery.put_notification", side_effect=always_fail):
                assert await process_outbox(factory, at=instant, max_attempts=2,
                                            retry_base_seconds=30) == 0
                assert await process_outbox(factory, at=instant + timedelta(seconds=31),
                                            max_attempts=2, retry_base_seconds=30) == 0
            async with factory() as db:
                row = await db.scalar(select(OutboxEvent).join(TaskEvent).where(
                    TaskEvent.task_id == task_id))
                assert row.state == "FAILED" and row.attempts == 2
            assert await process_outbox(factory, at=instant + timedelta(days=1)) == 0
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_partial_fanout_rolls_back_then_retries_without_duplicates():
    async def scenario():
        engine, _, factory, users, project_id, _ = await fixture()
        try:
            task_id = await create_example(factory, users, project_id)
            instant = now() + timedelta(seconds=2)
            assert await process_outbox(factory, at=instant) == 1
            async with factory() as db:
                async with db.begin():
                    task = await db.get(Task, task_id)
                    task.status = "DONE"
                    event = TaskEvent(task_id=task_id, sequence=2,
                                      actor_user_id=users[701], type="ADMIN_APPROVE",
                                      before_data={"status": "REVIEW"},
                                      after_data={"status": "DONE",
                                                  "created_by": str(users[703]),
                                                  "assignee_id": str(users[704])},
                                      occurred_at=now())
                    db.add(event)
                    await db.flush()
                    event_id = event.id
                    db.add(OutboxEvent(aggregate_type="task", aggregate_id=str(task_id),
                                       source_event_id=event_id, kind="TASK_EVENT",
                                       payload={"task_id": str(task_id),
                                                "event_type": "ADMIN_APPROVE"},
                                       dedupe_key=f"task_event:{event_id}",
                                       available_at=now()))
            from taskbot_worker.delivery import put_notification as real_put
            calls = 0

            async def fail_second(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("isolated partial fanout fault")
                return await real_put(*args, **kwargs)

            with patch("taskbot_worker.delivery.put_notification", side_effect=fail_second):
                assert await process_outbox(factory, at=instant, retry_base_seconds=30) == 0
            async with factory() as db:
                assert not (await db.scalars(select(Notification).where(
                    Notification.source_event_id == event_id))).all()
                row = await db.scalar(select(OutboxEvent).where(
                    OutboxEvent.source_event_id == event_id))
                assert row.state == "UNPROCESSED" and row.attempts == 1
            assert await process_outbox(factory, at=instant + timedelta(seconds=31)) == 1
            assert await process_outbox(factory, at=instant + timedelta(seconds=31)) == 0
            async with factory() as db:
                rows = (await db.scalars(select(Notification).where(
                    Notification.source_event_id == event_id))).all()
                assert {row.recipient_user_id for row in rows} == {users[703], users[704]}
                assert len(rows) == 2
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_weekly_report_and_notification_commit_atomically():
    async def scenario():
        engine, _, factory, users, project_id, _ = await fixture()
        try:
            instant = datetime(2026, 11, 2, 9, tzinfo=TASHKENT)

            async def fail_notification(*args, **kwargs):
                raise RuntimeError("isolated report notification fault")

            with patch("taskbot_worker.scheduler.put_notification",
                       side_effect=fail_notification):
                with pytest.raises(RuntimeError):
                    await schedule_weekly_reports(factory, at=instant)
            async with factory() as db:
                assert (await db.scalars(select(WeeklyReport))).all() == []
                assert (await db.scalars(select(Notification).where(
                    Notification.kind == "WEEKLY_REPORT"))).all() == []
            assert await schedule_weekly_reports(factory, at=instant) == 2
            assert await schedule_weekly_reports(factory, at=instant) == 0
            async with factory() as db:
                assert len((await db.scalars(select(WeeklyReport))).all()) == 2
                assert len((await db.scalars(select(Notification).where(
                    Notification.kind == "WEEKLY_REPORT"))).all()) == 2
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_existing_weekly_report_without_notification_is_repaired_once():
    async def scenario():
        engine, _, factory, users, project_id, _ = await fixture()
        try:
            instant = datetime(2026, 11, 2, 9, tzinfo=TASHKENT)
            assert await schedule_weekly_reports(factory, at=instant) == 2
            async with factory() as db:
                async with db.begin():
                    notification = await db.scalar(select(Notification).where(
                        Notification.kind == "WEEKLY_REPORT"))
                    await db.delete(notification)
            assert await schedule_weekly_reports(factory, at=instant) == 1
            assert await schedule_weekly_reports(factory, at=instant) == 0
            async with factory() as db:
                assert len((await db.scalars(select(WeeklyReport))).all()) == 2
                assert len((await db.scalars(select(Notification).where(
                    Notification.kind == "WEEKLY_REPORT"))).all()) == 2
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["blocked", "access_removed", "reassigned",
                                     "cancelled", "due_changed"])
def test_revalidation_between_claim_and_send(change):
    async def scenario():
        engine, _, factory, users, project_id, _ = await fixture()
        try:
            task_id = await create_example(factory, users, project_id)
            instant = now() + timedelta(seconds=2)
            assert await process_outbox(factory, at=instant) == 1
            real_claim = claim_notification

            async def claim_then_change(*args, **kwargs):
                claimed = await real_claim(*args, **kwargs)
                if claimed and claimed[3]:
                    async with factory() as db:
                        async with db.begin():
                            task = await db.get(Task, task_id)
                            if change == "blocked":
                                user = await db.get(User, users[704])
                                user.status = "BLOCKED"
                            elif change == "access_removed":
                                await db.delete(await db.get(ProjectMember,
                                    (project_id, users[704])))
                            elif change == "reassigned":
                                task.assignee_id = users[708]
                            elif change == "cancelled":
                                task.status = "CANCELLED"
                            else:
                                task.current_due_date = date(2027, 1, 2)
                return claimed

            fake = FakeTransport()
            with patch("taskbot_worker.delivery.claim_notification",
                       side_effect=claim_then_change):
                assert await deliver_notifications(factory, fake, settings(), at=instant) == 1
            async with factory() as db:
                row = await db.scalar(select(Notification).where(Notification.task_id == task_id))
                if change == "due_changed":
                    assert row.state == "SENT"
                    assert len(fake.sent) == 1 and "2027-01-02" in fake.sent[0][1]
                    assert "2027-01-01" not in fake.sent[0][1]
                else:
                    assert row.state == "SKIPPED" and fake.sent == []
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_qa_reassignment_after_claim_skips_old_reviewer():
    async def scenario():
        engine, _, factory, users, project_id, _ = await fixture()
        try:
            task_id = await create_example(factory, users, project_id)
            async with factory() as db:
                async with db.begin():
                    db.add(ProjectMember(project_id=project_id, user_id=users[706]))
            async with factory() as db:
                async with db.begin():
                    task = await db.get(Task, task_id)
                    author = await db.get(User, users[703])
                    await set_qa_reviewer(db, author, task, users[706], task.version)
            instant = now() + timedelta(seconds=2)
            assert await process_outbox(factory, at=instant) == 2
            real_claim = claim_notification

            async def claim_then_change(*args, **kwargs):
                claimed = await real_claim(*args, **kwargs)
                if claimed and claimed[3]:
                    async with factory() as db:
                        async with db.begin():
                            row = await db.get(Notification, claimed[0])
                            if row.kind == "QA_ASSIGNED":
                                task = await db.get(Task, task_id)
                                task.qa_reviewer_id = users[705]
                return claimed

            fake = FakeTransport()
            with patch("taskbot_worker.delivery.claim_notification",
                       side_effect=claim_then_change):
                await deliver_notifications(factory, fake, settings(), at=instant)
            async with factory() as db:
                row = await db.scalar(select(Notification).where(
                    Notification.task_id == task_id, Notification.kind == "QA_ASSIGNED"))
                assert row.state == "SKIPPED"
            assert all(chat_id != 706 for chat_id, _, _ in fake.sent)
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("error_kind", ["forbidden", "retry_after"])
def test_telegram_transport_error_classification_without_network(error_kind):
    async def scenario():
        engine, _, factory, users, project_id, _ = await fixture()
        try:
            task_id = await create_example(factory, users, project_id)
            instant = now() + timedelta(seconds=2)
            assert await process_outbox(factory, at=instant) == 1

            class FailingTransport:
                async def send(self, chat_id, body, keyboard=None):
                    method = SendMessage(chat_id=chat_id, text=body)
                    if error_kind == "forbidden":
                        raise TelegramForbiddenError(method, "mock forbidden")
                    raise TelegramRetryAfter(method, "mock retry", retry_after=12)

            assert await deliver_notifications(factory, FailingTransport(), settings(),
                                               at=instant) == 1
            async with factory() as db:
                row = await db.scalar(select(Notification).where(Notification.task_id == task_id))
                if error_kind == "forbidden":
                    assert row.state == "FAILED" and row.last_error_code == "TELEGRAM_REJECTED"
                else:
                    assert row.state == "RETRY" and row.last_error_code == "TELEGRAM_RETRY_AFTER"
                    assert row.scheduled_at.replace(tzinfo=timezone.utc) == instant + timedelta(seconds=12)
            fake = FakeTransport()
            if error_kind == "forbidden":
                assert await deliver_notifications(factory, fake, settings(),
                                                   at=instant + timedelta(days=1)) == 0
            else:
                assert await deliver_notifications(factory, fake, settings(),
                                                   at=instant + timedelta(seconds=11)) == 0
                assert await deliver_notifications(factory, fake, settings(),
                                                   at=instant + timedelta(seconds=12)) == 1
                assert len(fake.sent) == 1
        finally:
            await engine.dispose()
    asyncio.run(scenario())
