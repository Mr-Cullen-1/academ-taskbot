"""User-facing notification copy, independent of Telegram and production data."""

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from taskbot_api.models import DeadlineRequest, Task, TaskComment, User
from taskbot_api.reminder_routes import queue_reminder
from taskbot_api.reporting import empty_metrics, report_message
from taskbot_worker.delivery import event_message


class FakeDb:
    def __init__(self, rows=()):
        self.rows = {(model, identifier): row for model, identifier, row in rows}
        self.added = []

    async def get(self, model, identifier):
        return self.rows.get((model, identifier))

    def add(self, row):
        self.added.append(row)


def example(*, actor=True, comment=True, request=True, due=True):
    task_id, author_id, actor_id, comment_id, request_id = (uuid4() for _ in range(5))
    task = SimpleNamespace(id=task_id, task_key="TASK-1001", title="Готовый урок",
                           description="Подготовить урок", created_by=author_id,
                           current_due_date=date(2027, 1, 2) if due else None)
    after = {"reason": "Не моя специализация", "comment": "Исправить примеры",
             "comment_id": str(comment_id), "deadline_request_id": str(request_id),
             "proposed_due_date": "2027-01-04", "progress_note": "Черновик готов",
             "current_due_date": "2027-01-02" if due else None,
             "priority": "HIGH", "title": task.title}
    before = {"current_due_date": "2027-01-02" if due else None,
              "priority": "LOW", "title": task.title}
    rows = [(User, author_id, SimpleNamespace(name="Автор"))]
    if actor:
        rows.append((User, actor_id, SimpleNamespace(name="Исполнитель")))
    if comment:
        rows.append((TaskComment, comment_id,
                     SimpleNamespace(task_id=task_id, body="Проверьте пример")))
    if request:
        rows.append((DeadlineRequest, request_id,
                     SimpleNamespace(task_id=task_id, proposed_due_date=date(2027, 1, 4))))
    return FakeDb(rows), task, SimpleNamespace(actor_user_id=actor_id, type="UPDATED",
                                                approval_mode=None, before_data=before,
                                                after_data=after)


@pytest.mark.parametrize("kind,event_type,expected", [
    ("ASSIGNED", "CREATED", "Вам назначена задача"),
    ("ASSIGNMENT_REMOVED", "UPDATED", "больше не исполнитель"),
    ("TASK_UPDATED", "UPDATED", "изменены: приоритет"),
    ("QA_ASSIGNED", "QA_REVIEWER_CHANGED", "назначена QA-проверка"),
    ("QA_UNASSIGNED", "QA_REVIEWER_CHANGED", "снята QA-проверка"),
    ("REVIEW_REQUESTED", "SUBMIT_REVIEW", "отправлена на приёмку"),
    ("QA_REVIEW_REQUESTED", "SUBMIT_REVIEW", "ожидает вашей QA-проверки"),
    ("REWORK", "REQUEST_REWORK", "возвращена вам на доработку"),
    ("TASK_STATUS", "REQUEST_REWORK", "возвращена на доработку"),
    ("QA_DONE", "QA_REVIEW_DONE", "QA-проверка задачи"),
    ("TASK_DONE", "APPROVE", "принята автором"),
    ("TASK_CANCELLED", "CANCEL", "отменена"),
    ("TASK_BLOCKED", "BLOCK", "о блокировке выполнения"),
    ("TASK_COMMENT", "COMMENTED", "Проверьте пример"),
    ("DEADLINE_REQUEST", "DEADLINE_REQUESTED", "Предложенный срок: 2027-01-04"),
    ("DEADLINE_DECISION", "DEADLINE_APPROVED", "одобрен"),
    ("DEADLINE_DECISION", "DEADLINE_REJECTED", "отклонён"),
])
def test_event_text_is_clear_and_russian(kind, event_type, expected):
    db, task, event = example()
    event.type = event_type
    body = asyncio.run(event_message(db, task, event, kind))
    assert expected in body
    assert 'TASK-1001 «Готовый урок»' in body
    assert kind.lower().replace("_", " ") not in body.lower()
    assert len(body) <= 4000


@pytest.mark.parametrize("event_type,fragment", [
    ("ACCEPT", "назначение принято"),
    ("REJECT", "получен отказ от назначения"),
    ("START", "работа начата"),
    ("RESUME", "работа возобновлена"),
])
def test_assignment_responses_have_distinct_actions(event_type, fragment):
    db, task, event = example()
    event.type = event_type
    body = asyncio.run(event_message(db, task, event, "ASSIGNMENT_RESPONSE"))
    assert fragment in body
    assert "TASK-1001 «Готовый урок»" in body


def test_qa_completion_and_ordinary_vs_admin_approval():
    db, task, event = example()
    qa = asyncio.run(event_message(db, task, event, "QA_DONE"))
    ordinary = asyncio.run(event_message(db, task, event, "TASK_DONE"))
    event.type = "ADMIN_APPROVE"
    admin = asyncio.run(event_message(db, task, event, "TASK_DONE"))
    assert "ожидает приёмки" in qa and "принята" not in qa
    assert "принята автором" in ordinary
    assert "исключительной приёмки" in admin


def test_missing_optional_data_does_not_create_fake_details():
    db, task, event = example(actor=False, comment=False, request=False, due=False)
    event.after_data = {"comment_id": str(uuid4()), "deadline_request_id": str(uuid4())}
    for kind in ("TASK_COMMENT", "TASK_BLOCKED", "DEADLINE_REQUEST", "DEADLINE_DECISION"):
        body = asyncio.run(event_message(db, task, event, kind))
        assert "Готовый урок" in body
        assert "None" not in body and "Комментарий:" not in body
    assigned = asyncio.run(event_message(db, task, event, "ASSIGNED"))
    assert "Срок:" not in assigned


def test_updated_lists_only_changed_user_fields():
    db, task, event = example()
    body = asyncio.run(event_message(db, task, event, "TASK_UPDATED"))
    assert "изменены: приоритет" in body
    assert "название" not in body and "описание" not in body


def test_optional_long_text_is_bounded():
    db, task, event = example()
    task.description = "Описание " * 1000
    assigned = asyncio.run(event_message(db, task, event, "ASSIGNED"))
    assert len(assigned) < 4000 and "…" in assigned
    event.after_data["comment"] = "Исправить " * 1000
    rework = asyncio.run(event_message(db, task, event, "REWORK"))
    assert len(rework) < 4000 and "…" in rework


def test_personal_reminder_has_task_context_when_available():
    task_id = uuid4()
    task = SimpleNamespace(id=task_id, task_key="TASK-42", title="План урока")
    db = FakeDb()
    actor = SimpleNamespace(id=uuid4())
    row = SimpleNamespace(id=uuid4(), owner_user_id=uuid4(), task_id=task_id,
                          version=1, text="Проверить план", remind_at=None)
    with patch("taskbot_api.reminder_routes.visible_task", new_callable=AsyncMock,
               return_value=task) as check_access:
        asyncio.run(queue_reminder(db, row, actor))
        check_access.assert_awaited_once_with(db, actor, task_id)
    assert db.added[0].body == "Напоминание по задаче TASK-42 «План урока»: Проверить план"
    row.task_id = None
    asyncio.run(queue_reminder(db, row, actor))
    assert db.added[1].body == "Напоминание: Проверить план"


def test_weekly_report_header_uses_inclusive_dates():
    payload = {"week_start": "2026-10-26", "week_end_exclusive": "2026-11-02",
               "totals": empty_metrics(), "projects": []}
    message = report_message(payload)
    assert "с 2026-10-26 по 2026-11-01" in message
    assert "правая граница" not in message
