"""Telegram chat UI backed by the same project and task services as the Mini App."""

import base64
import re
from datetime import date, datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup, Update, WebAppInfo)
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from taskbot_api.models import AuditLog, DeadlineRequest, PersonalReminder, Project, ProjectMember, Task, TelegramUpdate, User, now
from taskbot_api.deadline_routes import (DeadlineDecision, DeadlineRequestCreate,
                                         create_request_record, decide_request_record)
from taskbot_api.reminder_routes import queue_reminder, validate_time
from taskbot_api.notification_routes import my_notifications
from taskbot_api.reporting import report_message, report_projects, report_week, visible_saved_report
from taskbot_api.analytics_routes import aggregates
from taskbot_api.routes import can_view_project, project_members, projects, user_data
from taskbot_api.security import role_codes
from taskbot_api.task_routes import list_tasks
from taskbot_api.task_service import (PRIORITIES, TYPES, add_comment, available_actions,
                                      create_task, set_qa_reviewer, transition,
                                      update_task, visible_task)


MENU = ("Мои задачи", "Все задачи", "Создать задачу", "Проекты",
        "Мои назначения", "Уведомления", "Напоминания", "Отчёты", "Профиль и настройки",
        "Помощь", "Главное меню")
ACTION_LABELS = {"accept": "Принять", "reject": "Отказаться", "start": "Начать",
                 "block": "Заблокировать", "resume": "Продолжить",
                 "submit-review": "На проверку", "approve": "Принять результат",
                 "admin-approve": "Исключительная приёмка", "request-rework": "Вернуть в работу",
                 "qa-review/done": "QA: готово", "cancel": "Отменить",
                 "assign": "Назначить", "set-qa-reviewer": "Назначить QA",
                 "edit": "Изменить", "comment": "Комментарий"}
REASONS = {"reject", "block", "cancel", "admin-approve", "request-rework"}


class Dialog(StatesGroup):
    waiting = State()


def active_keyboard(webapp_url: str | None = None) -> ReplyKeyboardMarkup:
    rows = []
    if webapp_url:
        rows.append([KeyboardButton(text="Открыть задачи", web_app=WebAppInfo(url=webapp_url))])
    rows.extend([[KeyboardButton(text="Мои задачи"), KeyboardButton(text="Все задачи")],
                 [KeyboardButton(text="Создать задачу"), KeyboardButton(text="Проекты")],
                 [KeyboardButton(text="Мои назначения"), KeyboardButton(text="Уведомления")],
                 [KeyboardButton(text="Напоминания"), KeyboardButton(text="Отчёты")],
                 [KeyboardButton(text="Профиль и настройки")],
                 [KeyboardButton(text="Помощь"), KeyboardButton(text="Главное меню")]])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
        for row in rows])


def compact_id(value: UUID) -> str:
    return base64.urlsafe_b64encode(value.bytes).decode("ascii").rstrip("=")


def expanded_id(value: str) -> UUID:
    if not re.fullmatch(r"[A-Za-z0-9_-]{22}", value):
        raise HTTPException(422, "INVALID_BUTTON")
    return UUID(bytes=base64.urlsafe_b64decode(value + "=="))


async def active_user(db: AsyncSession, telegram_id: int) -> User | None:
    user = await db.scalar(select(User).where(User.telegram_user_id == telegram_id))
    return user if user and user.status == "ACTIVE" else None


async def task_list(db: AsyncSession, actor: User, kind: str, cursor: UUID | None = None,
                    *, status: str | None = None, priority: str | None = None) -> tuple[str, InlineKeyboardMarkup | None]:
    options = {"created_by": actor.id} if kind == "my" else \
              {"assignee_id": actor.id} if kind == "assigned" else {}
    rows = await list_tasks(db, actor, cursor=cursor, limit=11, status=status,
                           priority=priority, **options)
    shown = rows[:10]
    heading = {"my": "Мои задачи", "assigned": "Мои назначения", "all": "Доступные задачи"}[kind]
    if not shown:
        return f"{heading}: задач нет.", None
    buttons = [[(f"{row['task_key']} · {row['title'][:35]} · {row['status']}",
                 f"ui:task:{row['id']}")] for row in shown]
    if kind == "all" and cursor is None:
        buttons.insert(0, [("Фильтр по статусу / приоритету", "ui:filters")])
    if len(rows) > 10:
        following = (f"ui:f:{'s' if status else 'p'}:{status or priority}:{shown[-1]['id']}"
                     if status or priority else f"ui:list:{kind}:{shown[-1]['id']}")
        buttons.append([("Ещё →", following)])
    return heading, inline(buttons)


async def project_list(db: AsyncSession, actor: User, *, creating: bool = False,
                       cursor: UUID | None = None) -> tuple[str, InlineKeyboardMarkup | None]:
    rows = await projects(db, actor, cursor=cursor, limit=11)
    shown = rows[:10]
    if not shown:
        return "Доступных проектов пока нет.", None
    prefix = "newproj" if creating else "project"
    buttons = [[(row["name"][:55], f"ui:{prefix}:{row['id']}")] for row in shown]
    if len(rows) > 10:
        buttons.append([("Ещё →", f"ui:{'newlist' if creating else 'projects'}:{shown[-1]['id']}")])
    return ("Выберите проект для новой задачи:" if creating else "Доступные проекты:"), inline(buttons)


async def project_task_list(db: AsyncSession, actor: User, project_id: UUID,
                            cursor: UUID | None = None) -> tuple[str, InlineKeyboardMarkup | None]:
    if not await can_view_project(db, actor, project_id) or await db.get(Project, project_id) is None:
        raise HTTPException(404, "NOT_FOUND")
    rows = await list_tasks(db, actor, project_id=project_id, cursor=cursor, limit=11)
    shown = rows[:10]
    if not shown:
        return "В проекте пока нет задач.", None
    buttons = [[(f"{row['task_key']} · {row['title'][:35]}", f"ui:task:{row['id']}")]
               for row in shown]
    if len(rows) > 10:
        buttons.append([("Ещё →", f"ui:pt:{compact_id(project_id)}:{compact_id(UUID(shown[-1]['id']))}")])
    return "Задачи проекта:", inline(buttons)


async def members_markup(db: AsyncSession, actor: User, project_id: UUID, kind: str,
                         task_key: str | None = None,
                         excluded: UUID | None = None) -> InlineKeyboardMarkup:
    members = await project_members(project_id, db, actor)
    rows = []
    for member in members:
        if excluded and member["id"] == str(excluded):
            continue
        if kind == "qa" and "QA" not in member["roles"]:
            continue
        data = f"ui:{kind}:{task_key}:{member['id']}" if task_key else f"ui:{kind}:{member['id']}"
        rows.append([(member["name"][:45], data)])
    rows.append([("Без назначения", f"ui:{kind}:{task_key}:none" if task_key else f"ui:{kind}:none")])
    return inline(rows)


async def task_card(db: AsyncSession, actor: User, task: Task) -> tuple[str, InlineKeyboardMarkup]:
    actions = await available_actions(db, actor, task)
    project = await db.get(Project, task.project_id)
    assignee = await db.get(User, task.assignee_id) if task.assignee_id else None
    qa = await db.get(User, task.qa_reviewer_id) if task.qa_reviewer_id else None
    text = (f"{task.task_key} · {task.title}\nПроект: {project.name if project else '—'}\n"
            f"{task.description[:1000]}\nСтатус: {task.status}; приоритет: {task.priority}\n"
            f"Срок: {task.current_due_date}\nИсполнитель: {assignee.name if assignee else 'не назначен'}\n"
            f"QA: {qa.name if qa else 'не назначен'} ({task.qa_review_status or '—'})")
    buttons = [[(ACTION_LABELS[action], f"ui:act:{action}:{task.id}")]
               for action in actions if action in ACTION_LABELS]
    buttons.append([("Комментарии", f"ui:comments:{task.id}"),
                    ("История", f"ui:history:{task.id}")])
    buttons.append([("Продление срока", f"ui:deadline:task:{task.id}")])
    buttons.append([("← К задачам", "ui:list:all:first")])
    return text, inline(buttons)


async def menu_message(message: Message, state: FSMContext,
                       registration_factory: async_sessionmaker[AsyncSession],
                       webapp_url: str | None = None) -> None:
    if not message.from_user or message.chat.type != "private":
        return
    choice = (message.text or "").strip()
    await state.clear()
    async with registration_factory() as db:
        actor = await active_user(db, message.from_user.id)
        if actor is None:
            await message.answer("Рабочее меню доступно после одобрения заявки. Отправьте /start.")
            return
        if choice == "Главное меню":
            await message.answer("Главное меню Taskbot.", reply_markup=active_keyboard(webapp_url))
            return
        if choice == "Помощь":
            await message.answer("Выберите раздел в меню. Нажмите задачу для действий. "
                                 "Во время ввода используйте /cancel для отмены. "
                                 "Mini App открывается кнопкой «Открыть задачи».")
            return
        if choice in {"Мои задачи", "Все задачи", "Мои назначения"}:
            kind = {"Мои задачи": "my", "Все задачи": "all", "Мои назначения": "assigned"}[choice]
            text, markup = await task_list(db, actor, kind)
        elif choice == "Проекты":
            text, markup = await project_list(db, actor)
        elif choice == "Создать задачу":
            text, markup = await project_list(db, actor, creating=True)
            if markup:
                await state.set_state(Dialog.waiting)
                await state.update_data(step="project", nonce=uuid4().hex[:12])
        elif choice == "Уведомления":
            rows = await my_notifications(db, actor, limit=10)
            text = "Последние уведомления:\n" + "\n".join(
                f"• {row['body'][:250]} ({row['state']})" for row in rows) if rows else "Уведомлений нет."
            markup = None
        elif choice == "Напоминания":
            rows = (await db.scalars(select(PersonalReminder).where(
                PersonalReminder.owner_user_id == actor.id,
                PersonalReminder.cancelled_at.is_(None))
                .order_by(PersonalReminder.remind_at, PersonalReminder.id).limit(10))).all()
            text = "Личные напоминания:\n" + "\n".join(
                f"• {row.remind_at.astimezone(ZoneInfo('Asia/Tashkent')):%Y-%m-%d %H:%M} · {row.text[:160]}"
                for row in rows) if rows else "Личных напоминаний нет."
            markup = inline([[('Создать напоминание', 'ui:remind:new')]] +
                            [[(f"Удалить: {row.text[:24]}", f"ui:remind:delete:{row.id}")]
                             for row in rows])
        elif choice == "Отчёты":
            actor_roles = await role_codes(db, actor.id)
            if not {"SUPER_ADMIN", "TEAM_LEAD", "PM"} & actor_roles:
                text = "Недельные отчёты доступны Super Admin, Team Lead и PM."
            else:
                week_start, begin, end = report_week(now())
                report = await visible_saved_report(db, actor, week_start)
                text = report_message(report.payload) if report else "Отчёт за прошлую неделю ещё не сформирован."
                if {"SUPER_ADMIN", "PM"} & actor_roles:
                    summary = await aggregates(db, await report_projects(db, actor), begin=begin, end=end)
                    totals = summary["totals"]
                    text += (f"\nТекущие показатели: создано {totals['created']}, "
                             f"завершено {totals['completed']}, "
                             f"просрочено {totals['current_overdue']}, "
                             f"ожидают проверки {totals['awaiting_review_or_acceptance']}.")
            markup = None
        else:
            profile = user_data(actor, await role_codes(db, actor.id), private=True)
            text = (f"Профиль: {profile['name']}\nСтатус: {profile['status']}\n"
                    f"Роли: {', '.join(profile['roles'])}\nУведомления доступны в разделе меню.")
            markup = inline([[("Изменить имя", "ui:profile:name")]])
    await message.answer(text, reply_markup=markup)


async def cancel_dialog(message: Message, state: FSMContext,
                        webapp_url: str | None = None) -> None:
    await state.clear()
    await message.answer("Действие отменено. Главное меню.", reply_markup=active_keyboard(webapp_url))


async def dialog_message(message: Message, state: FSMContext, event_update: Update,
                         registration_factory: async_sessionmaker[AsyncSession]) -> None:
    if not message.from_user or message.chat.type != "private":
        return
    data = await state.get_data()
    step = data.get("step")
    value = (message.text or "").strip()
    if not value:
        await message.answer("Отправьте текст или /cancel.")
        return
    if step == "title":
        if not 1 <= len(value) <= 200:
            await message.answer("Название должно содержать от 1 до 200 символов.")
            return
        await state.update_data(title=value, step="description")
        await message.answer("Описание задачи. Отправьте /skip, если оно не требуется.")
        return
    if step == "description":
        if len(value) > 30000:
            await message.answer("Описание слишком длинное.")
            return
        await state.update_data(description="" if value == "/skip" else value, step="due_date")
        await message.answer("Укажите обязательный дедлайн календарной датой: ГГГГ-ММ-ДД.")
        return
    if step == "due_date":
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError
        except ValueError:
            await message.answer("Введите дату в формате ГГГГ-ММ-ДД.")
            return
        await state.update_data(due_date=value, step="priority")
        await message.answer("Приоритет:", reply_markup=inline([[(item, f"ui:priority:{item}")]
                                                           for item in ("URGENT", "HIGH", "MEDIUM", "LOW")]))
        return
    if step == "remind_time":
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d %H:%M").replace(
                tzinfo=ZoneInfo("Asia/Tashkent"))
            validate_time(parsed)
        except ValueError:
            await message.answer("Введите дату и время в формате ГГГГ-ММ-ДД ЧЧ:ММ (Ташкент).")
            return
        except HTTPException as exc:
            await message.answer(f"Время отклонено: {exc.detail}.")
            return
        await state.update_data(step="remind_text", remind_at=parsed.isoformat())
        await message.answer("Введите текст напоминания (до 1000 символов). /cancel — отмена.")
        return
    if step == "remind_text":
        if len(value) > 1000:
            await message.answer("Текст должен быть не длиннее 1000 символов.")
            return
        reply = await commit_personal_reminder(registration_factory, message.from_user.id,
                                               event_update.update_id, data, value)
        if reply.startswith("Готово:"):
            await state.clear()
        await message.answer(reply)
        return
    if step == "deadline_date":
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError
        except ValueError:
            await message.answer("Введите дату в формате ГГГГ-ММ-ДД.")
            return
        await state.update_data(step="deadline_reason", proposed_due_date=value)
        await message.answer("Укажите причину продления. /cancel — отмена.")
        return
    if step == "deadline_reason":
        if len(value) > 4000:
            await message.answer("Причина слишком длинная (до 4000 символов).")
            return
        await state.update_data(step="deadline_progress", reason=value)
        await message.answer("Опишите текущий прогресс. /cancel — отмена.")
        return
    if step == "deadline_progress":
        if len(value) > 4000:
            await message.answer("Описание прогресса слишком длинное (до 4000 символов).")
            return
        reply = await commit_deadline_request(registration_factory, message.from_user.id,
                                              event_update.update_id, data, value)
        if reply.startswith("Готово:"):
            await state.clear()
        await message.answer(reply)
        return
    if step in {"reason", "comment", "edit", "profile_name"}:
        if step == "profile_name" and not 2 <= len(value) <= 160:
            await message.answer("Имя должно содержать от 2 до 160 символов.")
            return
        if step == "reason" and not value:
            await message.answer("Причина обязательна.")
            return
        if step == "reason" and data.get("action") == "admin-approve":
            if not value.startswith("CONFIRM ") or not value[8:].strip():
                await message.answer("Для исключительной приёмки подтвердите недоступность автора: CONFIRM <причина>.")
                return
            value = value[8:].strip()
        if step == "edit":
            field = data.get("field")
            if ((field == "title" and len(value) > 200) or
                    (field == "description" and len(value) > 30000) or
                    (field == "priority" and value not in PRIORITIES) or
                    (field == "type" and value not in TYPES)):
                await message.answer("Недопустимое значение поля. Проверьте длину или допустимые варианты.")
                return
        reply = await commit_dialog(registration_factory, message.from_user.id,
                                    event_update.update_id, data, value)
        if reply.startswith("Готово:"):
            await state.clear()
        await message.answer(reply)
        return
    await message.answer("Используйте кнопки или /cancel.")


async def commit_dialog(factory: async_sessionmaker[AsyncSession], telegram_id: int,
                        update_id: int, data: dict, value: str) -> str:
    async with factory() as db:
        try:
            async with db.begin():
                actor = await active_user(db, telegram_id)
                if actor is None:
                    return "Доступ закрыт."
                db.add(TelegramUpdate(update_id=update_id, response_code="BOT_DIALOG"))
                await db.flush()
                step = data["step"]
                if step == "profile_name":
                    if actor.name != value:
                        actor.name = value
                        actor.version += 1
                        actor.updated_at = now()
                        db.add(AuditLog(actor_user_id=actor.id, action="user.profile_name_changed",
                                        target_type="user", target_id=str(actor.id), details={}))
                    return "Готово: имя сохранено."
                task = await visible_task(db, actor, UUID(data["task_id"]), lock=True)
                if step == "comment":
                    await add_comment(db, actor, task, value, f"telegram:{update_id}")
                elif step == "edit":
                    await update_task(db, actor, task, expected_version=task.version,
                                      changes={data["field"]: value},
                                      request_id=f"telegram:{update_id}")
                else:
                    action = data["action"]
                    await transition(db, actor, task, action, task.version,
                                     reason=value if action != "request-rework" else None,
                                     comment=value if action == "request-rework" else None,
                                     author_unavailable_confirmed=action == "admin-approve",
                                     request_id=f"telegram:{update_id}")
                return f"Готово: {task.task_key} обновлена."
        except IntegrityError:
            await db.rollback()
            if await db.get(TelegramUpdate, update_id):
                return "Действие уже обработано."
            raise
        except (HTTPException, ValueError) as exc:
            await db.rollback()
            return f"Действие отклонено: {exc.detail if isinstance(exc, HTTPException) else 'INVALID_INPUT'}."


async def commit_task(factory: async_sessionmaker[AsyncSession], telegram_id: int,
                      update_id: int, data: dict) -> str:
    async with factory() as db:
        try:
            async with db.begin():
                actor = await active_user(db, telegram_id)
                if actor is None:
                    return "Доступ закрыт."
                db.add(TelegramUpdate(update_id=update_id, response_code="BOT_TASK_CREATED"))
                await db.flush()
                task = await create_task(db, actor, project_id=UUID(data["project_id"]),
                                         title=data["title"], description=data["description"],
                                         assignee_id=UUID(data["assignee_id"]) if data.get("assignee_id") else None,
                                         qa_reviewer_id=UUID(data["qa_reviewer_id"]) if data.get("qa_reviewer_id") else None,
                                         priority=data["priority"], type=data["type"],
                                         due_date=date.fromisoformat(data["due_date"]),
                                         request_id=f"telegram:{update_id}")
                return f"Готово: создана {task.task_key}."
        except IntegrityError:
            await db.rollback()
            if await db.get(TelegramUpdate, update_id):
                return "Задача уже создана."
            raise
        except (HTTPException, ValueError, KeyError) as exc:
            await db.rollback()
            return f"Действие отклонено: {exc.detail if isinstance(exc, HTTPException) else 'INVALID_INPUT'}."


async def commit_personal_reminder(factory: async_sessionmaker[AsyncSession], telegram_id: int,
                                   update_id: int, data: dict, value: str) -> str:
    async with factory() as db:
        try:
            async with db.begin():
                actor = await active_user(db, telegram_id)
                if actor is None:
                    return "Доступ закрыт."
                db.add(TelegramUpdate(update_id=update_id, response_code="BOT_REMINDER_CREATED"))
                await db.flush()
                row = PersonalReminder(owner_user_id=actor.id, task_id=None,
                                       remind_at=validate_time(datetime.fromisoformat(data["remind_at"])),
                                       text=value.strip())
                if not row.text:
                    raise HTTPException(422, "REMINDER_TEXT_REQUIRED")
                db.add(row)
                await db.flush()
                queue_reminder(db, row)
                return "Готово: личное напоминание создано."
        except IntegrityError:
            await db.rollback()
            if await db.get(TelegramUpdate, update_id):
                return "Действие уже обработано."
            raise
        except (HTTPException, ValueError, KeyError) as exc:
            await db.rollback()
            return f"Действие отклонено: {exc.detail if isinstance(exc, HTTPException) else 'INVALID_INPUT'}."


async def commit_personal_delete(factory: async_sessionmaker[AsyncSession], telegram_id: int,
                                 update_id: int, reminder_id: UUID) -> str:
    async with factory() as db:
        try:
            async with db.begin():
                actor = await active_user(db, telegram_id)
                if actor is None:
                    return "Доступ закрыт."
                db.add(TelegramUpdate(update_id=update_id, response_code="BOT_REMINDER_DELETED"))
                await db.flush()
                row = await db.get(PersonalReminder, reminder_id, with_for_update=True)
                if row is None or row.owner_user_id != actor.id or row.cancelled_at is not None:
                    raise HTTPException(404, "NOT_FOUND")
                row.cancelled_at = now()
                row.version += 1
                return "Готово: личное напоминание удалено."
        except IntegrityError:
            await db.rollback()
            if await db.get(TelegramUpdate, update_id):
                return "Действие уже обработано."
            raise
        except HTTPException as exc:
            await db.rollback()
            return f"Действие отклонено: {exc.detail}."


async def commit_deadline_request(factory: async_sessionmaker[AsyncSession], telegram_id: int,
                                  update_id: int, data: dict, progress: str) -> str:
    async with factory() as db:
        try:
            async with db.begin():
                actor = await active_user(db, telegram_id)
                if actor is None:
                    return "Доступ закрыт."
                db.add(TelegramUpdate(update_id=update_id, response_code="BOT_DEADLINE_REQUEST"))
                await db.flush()
                task_id = UUID(data["task_id"])
                task = await visible_task(db, actor, task_id)
                body = DeadlineRequestCreate(
                    proposed_due_date=date.fromisoformat(data["proposed_due_date"]),
                    reason=data["reason"], progress_note=progress,
                    expected_version=task.version)
                await create_request_record(db, actor, task_id, body, f"telegram:{update_id}")
                return f"Готово: запрос продления {task.task_key} отправлен автору."
        except IntegrityError:
            await db.rollback()
            if await db.get(TelegramUpdate, update_id):
                return "Действие уже обработано."
            raise
        except (HTTPException, ValueError, KeyError) as exc:
            await db.rollback()
            return f"Действие отклонено: {exc.detail if isinstance(exc, HTTPException) else 'INVALID_INPUT'}."


async def commit_deadline_decision(factory: async_sessionmaker[AsyncSession], telegram_id: int,
                                   update_id: int, request_id: UUID, approve: bool) -> str:
    async with factory() as db:
        try:
            async with db.begin():
                actor = await active_user(db, telegram_id)
                if actor is None:
                    return "Доступ закрыт."
                db.add(TelegramUpdate(update_id=update_id, response_code="BOT_DEADLINE_DECISION"))
                await db.flush()
                row = await db.get(DeadlineRequest, request_id)
                if row is None:
                    raise HTTPException(404, "NOT_FOUND")
                await decide_request_record(db, actor, request_id,
                                            DeadlineDecision(expected_version=row.version),
                                            approve=approve,
                                            request_idempotency=f"telegram:{update_id}")
                return "Готово: запрос продления одобрен." if approve else "Готово: запрос продления отклонён."
        except IntegrityError:
            await db.rollback()
            if await db.get(TelegramUpdate, update_id):
                return "Действие уже обработано."
            raise
        except HTTPException as exc:
            await db.rollback()
            return f"Действие отклонено: {exc.detail}."


async def ui_callback(callback: CallbackQuery, state: FSMContext, event_update: Update,
                      registration_factory: async_sessionmaker[AsyncSession],
                      webapp_url: str | None = None) -> None:
    if (not isinstance(callback.message, Message) or callback.message.chat.type != "private" or
            callback.message.chat.id != callback.from_user.id):
        await callback.answer("Только в личном чате.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    async with registration_factory() as db:
        actor = await active_user(db, callback.from_user.id)
        if actor is None:
            await callback.answer("Доступ закрыт. Отправьте /start.", show_alert=True)
            return
        try:
            action = parts[1]
            if action in {"list", "projects", "newlist"}:
                cursor = None if parts[-1] == "first" else UUID(parts[-1])
                if action == "list":
                    text, markup = await task_list(db, actor, parts[2], cursor)
                else:
                    text, markup = await project_list(db, actor, creating=action == "newlist", cursor=cursor)
            elif action == "filters":
                text = "Выберите фильтр задач:"
                markup = inline([[("Все", "ui:list:all:first")]] +
                                [[(f"Статус: {value}", f"ui:f:s:{value}:first")]
                                 for value in ("OPEN", "ACCEPTED", "REJECTED", "IN_PROGRESS",
                                               "BLOCKED", "REVIEW", "REWORK", "DONE", "CANCELLED")] +
                                [[(f"Приоритет: {value}", f"ui:f:p:{value}:first")]
                                 for value in ("URGENT", "HIGH", "MEDIUM", "LOW")])
            elif action == "f":
                dimension, value = parts[2], parts[3]
                if (dimension == "s" and value not in {"OPEN", "ACCEPTED", "REJECTED",
                                                         "IN_PROGRESS", "BLOCKED", "REVIEW",
                                                         "REWORK", "DONE", "CANCELLED"}) or \
                        (dimension == "p" and value not in PRIORITIES) or dimension not in {"s", "p"}:
                    raise HTTPException(422, "INVALID_FILTER")
                cursor = None if parts[4] == "first" else UUID(parts[4])
                text, markup = await task_list(db, actor, "all", cursor,
                                               status=value if dimension == "s" else None,
                                               priority=value if dimension == "p" else None)
            elif action == "task":
                task = await visible_task(db, actor, UUID(parts[2]))
                text, markup = await task_card(db, actor, task)
            elif action == "remind" and parts[2:] == ["new"]:
                await state.set_state(Dialog.waiting)
                await state.update_data(step="remind_time")
                text, markup = "Укажите дату и время: ГГГГ-ММ-ДД ЧЧ:ММ (Ташкент). /cancel — отмена.", None
            elif action == "remind" and len(parts) == 4 and parts[2] == "delete":
                reminder_id = UUID(parts[3])
                await db.rollback()
                text = await commit_personal_delete(registration_factory, callback.from_user.id,
                                                    event_update.update_id, reminder_id)
                markup = None
            elif action == "deadline" and len(parts) == 4 and parts[2] == "task":
                task = await visible_task(db, actor, UUID(parts[3]))
                requests = (await db.scalars(select(DeadlineRequest).where(
                    DeadlineRequest.task_id == task.id).order_by(DeadlineRequest.created_at.desc())
                    .limit(5))).all()
                text = "Запросы продления:\n" + "\n".join(
                    f"• {row.proposed_due_date} · {row.status} · {row.reason[:120]}"
                    for row in requests) if requests else "Запросов продления пока нет."
                buttons = []
                if (task.assignee_id == actor.id and task.status not in {"DONE", "CANCELLED", "REJECTED"} and
                        not any(row.status == "PENDING" for row in requests)):
                    buttons.append([("Запросить продление", f"ui:deadline:new:{task.id}")])
                roles = await role_codes(db, actor.id)
                if (task.created_by == actor.id or
                        ("PM" in roles and await db.get(ProjectMember, (task.project_id, actor.id)))):
                    for row in requests:
                        if row.status == "PENDING":
                            buttons.append([("Одобрить", f"ui:deadline:approve:{row.id}"),
                                            ("Отклонить", f"ui:deadline:reject:{row.id}")])
                buttons.append([("← К задаче", f"ui:task:{task.id}")])
                markup = inline(buttons)
            elif action == "deadline" and len(parts) == 4 and parts[2] == "new":
                task = await visible_task(db, actor, UUID(parts[3]))
                if task.assignee_id != actor.id or task.status in {"DONE", "CANCELLED", "REJECTED"}:
                    raise HTTPException(403, "FORBIDDEN")
                await state.set_state(Dialog.waiting)
                await state.update_data(step="deadline_date", task_id=str(task.id))
                text, markup = "Укажите новую дату срока: ГГГГ-ММ-ДД. /cancel — отмена.", None
            elif action == "deadline" and len(parts) == 4 and parts[2] in {"approve", "reject"}:
                request_id = UUID(parts[3])
                reference = await db.get(DeadlineRequest, request_id)
                if reference is None:
                    raise HTTPException(404, "NOT_FOUND")
                await visible_task(db, actor, reference.task_id)
                await db.rollback()
                text = await commit_deadline_decision(registration_factory, callback.from_user.id,
                                                      event_update.update_id, request_id,
                                                      parts[2] == "approve")
                markup = None
            elif action == "project":
                project_id = UUID(parts[2])
                project = await db.get(Project, project_id)
                if not project or not await can_view_project(db, actor, project_id):
                    raise HTTPException(404, "NOT_FOUND")
                text, markup = f"{project.name}\n{project.description[:1000]}", inline([
                    [("Задачи проекта", f"ui:projecttasks:{project_id}")],
                    [("Создать задачу", f"ui:newproj:{project_id}")]])
            elif action == "projecttasks":
                project_id = UUID(parts[2])
                text, markup = await project_task_list(db, actor, project_id)
            elif action == "pt" and len(parts) == 4:
                text, markup = await project_task_list(
                    db, actor, expanded_id(parts[2]), expanded_id(parts[3]))
            elif action == "newproj":
                project_id = UUID(parts[2])
                if await db.get(Project, project_id) is None or not await can_view_project(db, actor, project_id):
                    raise HTTPException(404, "NOT_FOUND")
                await state.set_state(Dialog.waiting)
                await state.update_data(step="title", project_id=str(project_id), nonce=uuid4().hex[:12])
                text, markup = "Введите название задачи (до 200 символов). /cancel — отмена.", None
            elif action in {"comments", "history"}:
                task = await visible_task(db, actor, UUID(parts[2]))
                from taskbot_api.models import TaskComment, TaskEvent
                if action == "comments":
                    rows = (await db.scalars(select(TaskComment).where(TaskComment.task_id == task.id)
                                             .order_by(TaskComment.created_at.desc()).limit(10))).all()
                    text = "\n".join(f"• {row.body[:300]}" for row in rows) or "Комментариев нет."
                else:
                    rows = (await db.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id)
                                             .order_by(TaskEvent.sequence.desc()).limit(10))).all()
                    text = "\n".join(f"#{row.sequence} {row.type}" for row in rows) or "Истории нет."
                markup = inline([[("← К задаче", f"ui:task:{task.id}")]])
            elif action == "profile" and parts[2:] == ["name"]:
                await state.set_state(Dialog.waiting)
                await state.update_data(step="profile_name")
                text, markup = "Введите новое имя (2–160 символов). /cancel — отмена.", None
            elif action == "priority":
                data = await state.get_data()
                if data.get("step") != "priority" or parts[2] not in PRIORITIES:
                    raise HTTPException(409, "DIALOG_EXPIRED")
                await state.update_data(priority=parts[2], step="type")
                text = "Тип задачи:"
                markup = inline([[(item, f"ui:type:{item}")] for item in
                                 ("DEVELOPMENT", "BUG_FIX", "IMPROVEMENT", "QA_CHECK", "RESEARCH")])
            elif action == "type":
                data = await state.get_data()
                if data.get("step") != "type" or parts[2] not in TYPES:
                    raise HTTPException(409, "DIALOG_EXPIRED")
                await state.update_data(type=parts[2], step="assignee")
                text, markup = "Выберите исполнителя:", await members_markup(
                    db, actor, UUID(data["project_id"]), "assignee")
            elif action in {"assignee", "qa"} and len(parts) == 3:
                data = await state.get_data()
                if data.get("step") != action:
                    raise HTTPException(409, "DIALOG_EXPIRED")
                chosen = None if parts[2] == "none" else UUID(parts[2])
                if chosen:
                    from taskbot_api.task_service import eligible_member
                    await eligible_member(db, UUID(data["project_id"]), chosen, qa=action == "qa")
                if action == "assignee":
                    await state.update_data(assignee_id=str(chosen) if chosen else None, step="qa")
                    text, markup = "Выберите QA-проверяющего:", await members_markup(
                        db, actor, UUID(data["project_id"]), "qa", excluded=chosen)
                else:
                    if chosen and str(chosen) == data.get("assignee_id"):
                        raise HTTPException(422, "QA_MUST_DIFFER_FROM_ASSIGNEE")
                    await state.update_data(qa_reviewer_id=str(chosen) if chosen else None, step="confirm")
                    latest = await state.get_data()
                    text = (f"Создать задачу «{latest['title']}»?\nСрок: {latest['due_date']}; "
                            f"приоритет: {latest['priority']}; тип: {latest['type']}.")
                    markup = inline([[('Создать', f"ui:create:{latest['nonce']}")],
                                     [('Отмена', 'ui:cancel')]])
            elif action == "create":
                data = await state.get_data()
                if data.get("step") != "confirm" or parts[2] != data.get("nonce"):
                    raise HTTPException(409, "DIALOG_EXPIRED")
                await db.rollback()
                text = await commit_task(registration_factory, callback.from_user.id,
                                         event_update.update_id, data)
                if text.startswith("Готово:"):
                    await state.clear()
                markup = None
            elif action == "cancel":
                await state.clear()
                text, markup = "Действие отменено.", None
            elif action == "act":
                task = await visible_task(db, actor, UUID(parts[-1]))
                selected = ":".join(parts[2:-1])
                if selected not in await available_actions(db, actor, task):
                    raise HTTPException(403, "FORBIDDEN")
                if selected in {"assign", "set-qa-reviewer"}:
                    kind = "assign" if selected == "assign" else "qa"
                    text = "Выберите исполнителя:" if kind == "assign" else "Выберите QA:"
                    markup = await members_markup(db, actor, task.project_id, kind,
                                                  task.task_key, excluded=task.assignee_id if kind == "qa" else None)
                elif selected == "edit":
                    text, markup = "Какое поле изменить?", inline([[
                        (field, f"ui:edit:{field}:{task.task_key}")]
                        for field in ("title", "description", "priority", "type")])
                elif selected in REASONS or selected == "comment":
                    await state.set_state(Dialog.waiting)
                    await state.update_data(step="comment" if selected == "comment" else "reason",
                                            task_id=str(task.id), action=selected)
                    text, markup = ("Введите комментарий." if selected == "comment" else
                                    "Введите обязательную причину. /cancel — отмена."), None
                else:
                    task_id = task.id
                    await db.rollback()
                    text = await commit_action(registration_factory, callback.from_user.id,
                                               event_update.update_id, task_id, selected)
                    markup = None
            elif action == "edit":
                field, key = parts[2], parts[3]
                if field not in {"title", "description", "priority", "type"}:
                    raise HTTPException(422, "INVALID_FIELD")
                task = await db.scalar(select(Task).where(Task.task_key == key))
                if task is None:
                    raise HTTPException(404, "NOT_FOUND")
                await visible_task(db, actor, task.id)
                if "edit" not in await available_actions(db, actor, task):
                    raise HTTPException(403, "FORBIDDEN")
                await state.set_state(Dialog.waiting)
                await state.update_data(step="edit", task_id=str(task.id), field=field)
                text, markup = f"Введите новое значение {field}. /cancel — отмена.", None
            elif action in {"assign", "qa"} and len(parts) == 4:
                key = parts[2]
                chosen = None if parts[3] == "none" else UUID(parts[3])
                task = await db.scalar(select(Task).where(Task.task_key == key))
                if task is None:
                    raise HTTPException(404, "NOT_FOUND")
                await visible_task(db, actor, task.id)
                if ("assign" if action == "assign" else "set-qa-reviewer") not in await available_actions(db, actor, task):
                    raise HTTPException(403, "FORBIDDEN")
                task_id = task.id
                await db.rollback()
                text = await commit_assignment(registration_factory, callback.from_user.id,
                                               event_update.update_id, task_id, action, chosen)
                markup = None
            else:
                raise HTTPException(422, "INVALID_BUTTON")
        except (HTTPException, ValueError, IndexError, KeyError) as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else "INVALID_BUTTON"
            await callback.answer(f"Действие отклонено: {detail}.", show_alert=True)
            return
    await callback.message.answer(text, reply_markup=markup)
    await callback.answer()


async def commit_action(factory: async_sessionmaker[AsyncSession], telegram_id: int,
                        update_id: int, task_id: UUID, action: str) -> str:
    async with factory() as db:
        try:
            async with db.begin():
                actor = await active_user(db, telegram_id)
                if actor is None:
                    return "Доступ закрыт."
                db.add(TelegramUpdate(update_id=update_id, response_code="BOT_TASK_ACTION"))
                await db.flush()
                task = await visible_task(db, actor, task_id, lock=True)
                await transition(db, actor, task, action, task.version,
                                 request_id=f"telegram:{update_id}")
                return f"Готово: {task.task_key} — {task.status}."
        except IntegrityError:
            await db.rollback()
            if await db.get(TelegramUpdate, update_id):
                return "Действие уже обработано."
            raise
        except HTTPException as exc:
            await db.rollback()
            return f"Действие отклонено: {exc.detail}."


async def commit_assignment(factory: async_sessionmaker[AsyncSession], telegram_id: int,
                            update_id: int, task_id: UUID, kind: str,
                            user_id: UUID | None) -> str:
    async with factory() as db:
        try:
            async with db.begin():
                actor = await active_user(db, telegram_id)
                if actor is None:
                    return "Доступ закрыт."
                db.add(TelegramUpdate(update_id=update_id, response_code="BOT_ASSIGNMENT"))
                await db.flush()
                task = await visible_task(db, actor, task_id, lock=True)
                if kind == "assign":
                    await update_task(db, actor, task, expected_version=task.version,
                                      changes={"assignee_id": user_id}, request_id=f"telegram:{update_id}")
                else:
                    await set_qa_reviewer(db, actor, task, user_id, task.version,
                                          request_id=f"telegram:{update_id}")
                return f"Готово: назначение для {task.task_key} сохранено."
        except IntegrityError:
            await db.rollback()
            if await db.get(TelegramUpdate, update_id):
                return "Действие уже обработано."
            raise
        except HTTPException as exc:
            await db.rollback()
            return f"Действие отклонено: {exc.detail}."


def build_menu_router() -> Router:
    router = Router()
    router.callback_query.register(ui_callback, lambda callback: bool(callback.data) and
                                   callback.data.startswith("ui:"))
    router.message.register(cancel_dialog, lambda message: message.text == "/cancel")
    router.message.register(menu_message, lambda message: message.text in MENU)
    router.message.register(dialog_message, StateFilter(Dialog.waiting))
    return router
