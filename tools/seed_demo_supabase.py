"""One-time DEMO data seed in the configured PostgreSQL database.

Run only with an intentionally disabled notification worker. The script uses
the application's task service for every state transition and never sends a
Telegram message or creates a web session.
"""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from taskbot_api.config import get_settings
from taskbot_api.db import build_engine, session_factory
from taskbot_api.models import OutboxEvent, Project, ProjectMember, Task, TaskComment, TaskEvent, User, UserRole, now
from taskbot_api.routes import audit
from taskbot_api.task_service import add_comment, create_task, transition


PROJECT_NAME = "[DEMO] Taskbot UI — проверка статусов и приоритетов"
PROJECT_DESCRIPTION = (
    "Тестовый проект для визуальной проверки интерфейса, приоритетов, статусов, "
    "дедлайнов, фильтров и карточек задач. Не использовать для рабочей деятельности."
)
MANIFEST = Path(__file__).resolve().parents[1] / ".tmp" / "demo-supabase-manifest.json"


async def main() -> None:
    settings = get_settings()
    if (settings.database_url is None or
            not settings.database_url.lower().startswith("postgresql+asyncpg://") or
            settings.notification_delivery_enabled or settings.notification_transport != "dry_run"):
        raise SystemExit("Configured PostgreSQL and disabled dry_run delivery are required")
    engine = build_engine(settings)
    assert engine is not None
    try:
        factory = session_factory(engine)
        async with factory() as db:
            async with db.begin():
                admins = (await db.scalars(
                    select(User).join(UserRole, UserRole.user_id == User.id)
                    .where(UserRole.code == "SUPER_ADMIN", User.status == "ACTIVE")
                )).all()
                if len(admins) != 1:
                    raise RuntimeError("Exactly one active Super Admin is required")
                actor = admins[0]
                existing = await db.scalar(select(Project.id).where(Project.name == PROJECT_NAME))
                if existing:
                    raise RuntimeError("DEMO project already exists; no changes made")

                project = Project(name=PROJECT_NAME, description=PROJECT_DESCRIPTION,
                                  owner_user_id=actor.id)
                db.add(project)
                await db.flush()
                db.add(ProjectMember(project_id=project.id, user_id=actor.id))
                audit(db, actor, "project.created", "project", str(project.id),
                      {"demo_seed": True})
                await db.flush()

                today = datetime.now(ZoneInfo("Asia/Tashkent")).date()
                definitions = [
                    ("OPEN_URGENT", "URGENT", "Срочно проверить материалы перед выпуском", 0, True),
                    ("OPEN_OVERDUE", "HIGH", "Просроченное исправление иллюстраций", -1, True),
                    ("OPEN_LONG", "LOW", "Подготовить подробное описание содержания уроков по каждому разделу курса и сверить структуру длинных заголовков", 12, False),
                    ("ACCEPTED", "HIGH", "Проверить правки первого модуля", 2, True),
                    ("IN_PROGRESS", "MEDIUM", "Подготовить задания для урока", 3, True),
                    ("BLOCKED", "URGENT", "Устранить блокирующую ошибку публикации", 4, True),
                    ("REVIEW", "HIGH", "Передать учебный блок на проверку", 5, True),
                    ("REWORK", "MEDIUM", "Исправить замечания по тестам", 6, True),
                    ("DONE", "LOW", "Завершить проверку вводного урока", 2, True),
                    ("DONE_LATE", "HIGH", "Завершить ранее просроченный урок", -2, True),
                    ("CANCELLED", "LOW", "Отменённый черновик урока", 7, True),
                    ("REJECTED", "MEDIUM", "Назначение отклонено с причиной", 8, True),
                    ("REVIEW_SECOND", "URGENT", "Проверить второй учебный блок", 9, True),
                ]
                tasks = {}
                for label, priority, title, offset, assigned in definitions:
                    description = (
                        ("Длинное описание для проверки переноса строк и прокрутки. " * 30).strip()
                        if label == "OPEN_LONG" else
                        f"[DEMO] Сценарий {label}. Данные предназначены только для проверки интерфейса."
                    )
                    task = await create_task(
                        db, actor, project_id=project.id,
                        title=f"[DEMO] {label}: {title}", description=description,
                        assignee_id=actor.id if assigned else None, qa_reviewer_id=None,
                        priority=priority, type="DEVELOPMENT",
                        due_date=today + timedelta(days=offset),
                        request_id=f"demo-seed:{label}:create",
                    )
                    tasks[label] = task

                async def act(label: str, action: str, **extra) -> None:
                    task = tasks[label]
                    await transition(db, actor, task, action, task.version,
                                     request_id=f"demo-seed:{label}:{action}", **extra)

                progressed = ("ACCEPTED", "IN_PROGRESS", "BLOCKED", "REVIEW",
                              "REWORK", "DONE", "DONE_LATE", "REVIEW_SECOND")
                for label in progressed:
                    await act(label, "accept")
                for label in progressed[1:]:
                    await act(label, "start")
                await act("BLOCKED", "block", reason="[DEMO] Требуется исправление исходных материалов")
                for label in ("REVIEW", "REWORK", "DONE", "DONE_LATE", "REVIEW_SECOND"):
                    await act(label, "submit-review", result="[DEMO] Результат готов к проверке")
                await act("REWORK", "request-rework", comment="[DEMO] Исправить ответы в тестовом уроке")
                for label in ("DONE", "DONE_LATE"):
                    await act(label, "approve")
                await act("CANCELLED", "cancel", reason="[DEMO] Черновик больше не нужен")
                await act("REJECTED", "reject", reason="[DEMO] Нет доступа к исходным материалам")
                await add_comment(db, actor, tasks["BLOCKED"],
                                  "[DEMO] Комментарий для проверки истории задачи.",
                                  request_id="demo-seed:BLOCKED:comment")
                await db.flush()

                statuses = {task.status for task in tasks.values()}
                priorities = {task.priority for task in tasks.values()}
                assert len(tasks) == 13
                assert statuses == {"OPEN", "ACCEPTED", "REJECTED", "IN_PROGRESS", "BLOCKED",
                                    "REVIEW", "REWORK", "DONE", "CANCELLED"}
                assert priorities == {"URGENT", "HIGH", "MEDIUM", "LOW"}
                task_ids = [task.id for task in tasks.values()]
                event_count = await db.scalar(select(func.count()).select_from(TaskEvent)
                                              .where(TaskEvent.task_id.in_(task_ids)))
                outbox_count = await db.scalar(select(func.count()).select_from(OutboxEvent)
                                               .where(OutboxEvent.aggregate_id.in_([str(x) for x in task_ids])))
                assert event_count == outbox_count and event_count > 13
                manifest = {
                    "database": "configured Supabase PostgreSQL",
                    "project_id": str(project.id), "project_name": PROJECT_NAME,
                    "owner_user_id": str(actor.id),
                    "tasks": {label: {"id": str(task.id), "key": task.task_key,
                                      "status": task.status, "priority": task.priority,
                                      "due_date": task.current_due_date.isoformat()}
                              for label, task in tasks.items()},
                    "event_count": event_count, "outbox_count": outbox_count,
                    "created_at": now().isoformat(),
                }
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Created DEMO project {manifest['project_id']} with 13 tasks, "
              f"{event_count} events and {outbox_count} pending outbox events.")
        print("Delivery is disabled; no worker was started or messages sent.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
