import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select

from taskbot_api.models import Notification, PersonalReminder
from taskbot_bot.menu import commit_personal_delete, commit_personal_reminder
from test_tasks import fixture


def test_bot_personal_reminder_uses_shared_queue_and_update_deduplication():
    async def scenario():
        engine, app, factory, users, project, other = await fixture(enforce_foreign_keys=True)
        try:
            instant = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
            data = {"remind_at": instant}
            first = await commit_personal_reminder(factory, 703, 9901, data, "Check the lesson")
            second = await commit_personal_reminder(factory, 703, 9901, data, "Check the lesson")
            assert first.startswith("Готово:")
            assert second == "Действие уже обработано."
            async with factory() as db:
                rows = (await db.scalars(select(PersonalReminder))).all()
                notes = (await db.scalars(select(Notification).where(
                    Notification.kind == "PERSONAL_REMINDER"))).all()
                assert len(rows) == len(notes) == 1
                assert notes[0].recipient_user_id == users[703]
                reminder_id = rows[0].id
            denied = await commit_personal_delete(factory, 704, 9902, reminder_id)
            assert "NOT_FOUND" in denied
            removed = await commit_personal_delete(factory, 703, 9903, reminder_id)
            assert removed.startswith("Готово:")
            assert await commit_personal_delete(factory, 703, 9903, reminder_id) == "Действие уже обработано."
            async with factory() as db:
                row = await db.get(PersonalReminder, reminder_id)
                assert row.cancelled_at is not None and row.version == 2
        finally:
            await engine.dispose()

    asyncio.run(scenario())
