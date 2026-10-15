"""Private-chat task commands; rules are enforced by the shared service."""

from aiogram import Router
from aiogram.types import CallbackQuery, ForceReply, Message, Update
from fastapi import HTTPException
from sqlalchemy import select
from uuid import UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from taskbot_api.models import ProjectMember, Task, TaskComment, TaskEvent, TelegramUpdate, User
from taskbot_api.security import role_codes
from taskbot_api.task_service import (
    available_actions, is_overdue, visible_task,
    transition, add_comment,
)


ACTION_COMMANDS = {
    "accept": "accept", "reject": "reject", "begin": "start", "block": "block",
    "resume": "resume", "submit": "submit-review", "approve": "approve",
    "admin_approve": "admin-approve", "rework": "request-rework",
    "qa_done": "qa-review/done", "cancel": "cancel",
}
SUPPORTED = {"my_tasks", "created_tasks", "task", "comments", "history", "comment", *ACTION_COMMANDS}


async def task_command(message: Message, event_update: Update,
                       registration_factory: async_sessionmaker[AsyncSession]) -> None:
    if not message.from_user or message.chat.type != "private":
        return
    raw = (message.text or "").strip()
    command, _, arguments = raw.partition(" ")
    command = command[1:].split("@", 1)[0].lower()
    if command not in SUPPORTED:
        return
    async with registration_factory() as db:
        try:
            async with db.begin():
                db.add(TelegramUpdate(update_id=event_update.update_id, response_code="TASK_COMMAND"))
                await db.flush()
                user = await db.scalar(select(User).where(User.telegram_user_id == message.from_user.id))
                if user is None or user.status != "ACTIVE":
                    reply = "Доступ к задачам появится после одобрения заявки."
                else:
                    reply = await execute(db, user, command, arguments,
                                          f"telegram:{event_update.update_id}")
        except IntegrityError:
            await db.rollback()
            if await db.get(TelegramUpdate, event_update.update_id) is not None:
                return
            raise
        except HTTPException as exc:
            await db.rollback()
            reply = f"Действие отклонено: {exc.detail}."
    await message.answer(reply)


async def assignment_callback(callback: CallbackQuery, event_update: Update,
                              registration_factory: async_sessionmaker[AsyncSession]) -> None:
    if (not isinstance(callback.message, Message) or callback.message.chat.type != "private" or
            callback.message.chat.id != callback.from_user.id):
        await callback.answer("Действие доступно только в личном чате.", show_alert=True)
        return
    try:
        _, action, raw_id = (callback.data or "").split(":", 2)
        task_id = UUID(raw_id)
    except (ValueError, TypeError):
        await callback.answer("Неверная кнопка.", show_alert=True)
        return
    if action == "reject":
        # The actual transition waits for a nonempty reply; the prompt changes no data.
        async with registration_factory() as db:
            user = await db.scalar(select(User).where(User.telegram_user_id == callback.from_user.id))
            task = await db.get(Task, task_id)
            if not user or user.status != "ACTIVE" or not task or task.assignee_id != user.id or task.status != "OPEN":
                await callback.answer("Назначение уже недоступно.", show_alert=True)
                return
            try:
                await visible_task(db, user, task.id)
            except HTTPException:
                await callback.answer("Нет доступа к задаче.", show_alert=True)
                return
            key = task.task_key
        await callback.message.answer(f"Ответьте на это сообщение причиной отказа от {key}:",
                                      reply_markup=ForceReply(selective=True))
        await callback.answer()
        return
    if action != "accept":
        await callback.answer("Неверная кнопка.", show_alert=True)
        return
    reply = await assignment_transition(registration_factory, callback.from_user.id,
                                        event_update.update_id, task_id, "accept")
    await callback.answer(reply, show_alert=reply != "Задача принята.")
    if reply == "Задача принята.":
        await callback.message.edit_reply_markup(reply_markup=None)


async def refusal_reply(message: Message, event_update: Update,
                        registration_factory: async_sessionmaker[AsyncSession]) -> None:
    if not message.from_user or message.chat.type != "private" or not message.reply_to_message:
        return
    prompt = message.reply_to_message.text or ""
    prefix = "Ответьте на это сообщение причиной отказа от "
    if not message.reply_to_message.from_user or not message.reply_to_message.from_user.is_bot or \
            not prompt.startswith(prefix) or not prompt.endswith(":"):
        return
    reason = (message.text or "").strip()
    if not reason:
        await message.answer("Укажите причину отказа текстом.")
        return
    key = prompt[len(prefix):-1]
    async with registration_factory() as db:
        task = await db.scalar(select(Task).where(Task.task_key == key))
    if task is None:
        await message.answer("Задача не найдена.")
        return
    reply = await assignment_transition(registration_factory, message.from_user.id,
                                        event_update.update_id, task.id, "reject", reason)
    if reply:
        await message.answer(reply)


async def assignment_transition(factory: async_sessionmaker[AsyncSession], telegram_user_id: int,
                                update_id: int, task_id: UUID, action: str,
                                reason: str | None = None) -> str:
    async with factory() as db:
        try:
            async with db.begin():
                db.add(TelegramUpdate(update_id=update_id, response_code="ASSIGNMENT_ACTION"))
                await db.flush()
                user = await db.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
                if user is None or user.status != "ACTIVE":
                    return "Нет доступа к задачам."
                task = await db.get(Task, task_id, with_for_update=True)
                if task is None:
                    return "Задача не найдена."
                await visible_task(db, user, task.id)
                await transition(db, user, task, action, task.version, reason=reason,
                                 request_id=f"telegram:{update_id}")
                return "Задача принята." if action == "accept" else "Отказ сохранён и автор уведомлён."
        except IntegrityError:
            await db.rollback()
            if await db.get(TelegramUpdate, update_id) is not None:
                return "Действие уже обработано."
            raise
        except HTTPException as exc:
            await db.rollback()
            return f"Действие отклонено: {exc.detail}."


async def execute(db: AsyncSession, user: User, command: str,
                  arguments: str, request_id: str) -> str:
    if command in {"my_tasks", "created_tasks"}:
        field = Task.assignee_id if command == "my_tasks" else Task.created_by
        stmt = select(Task).where(field == user.id).order_by(Task.created_at.desc()).limit(20)
        if "SUPER_ADMIN" not in await role_codes(db, user.id):
            stmt = stmt.join(ProjectMember, ProjectMember.project_id == Task.project_id).where(
                ProjectMember.user_id == user.id)
        rows = (await db.scalars(stmt)).all()
        return "\n".join(f"{task.task_key} · {task.title} · {task.status}" for task in rows) or "Задач нет."
    key, _, extra = arguments.strip().partition(" ")
    if not key:
        raise HTTPException(422, "Укажите ключ задачи, например TASK-1001")
    task = await db.scalar(select(Task).where(Task.task_key == key.upper()).with_for_update())
    if task is None:
        raise HTTPException(404, "NOT_FOUND")
    task = await visible_task(db, user, task.id)
    if command == "task":
        actions = await available_actions(db, user, task)
        qa_label = "пройдена" if task.qa_review_status == "DONE" and task.status in {"REVIEW", "DONE"} else \
            "ожидает повторной проверки" if task.qa_review_status == "DONE" else task.qa_review_status or "не назначен"
        commands = []
        for alias, code in ACTION_COMMANDS.items():
            if code not in actions:
                continue
            suffix = " CONFIRM <причина>" if code == "admin-approve" else \
                " <комментарий>" if code == "request-rework" else \
                " <причина>" if code in {"reject", "block", "cancel"} else \
                " [результат]" if code == "submit-review" else ""
            commands.append("/" + alias + " " + task.task_key + suffix)
        if "comment" in actions:
            commands.append("/comment " + task.task_key + " текст")
        return (f"{task.task_key} · {task.title}\n{task.description[:1000]}\nСтатус: {task.status}\n"
                f"QA: {qa_label}\n"
                f"Исполнитель: {task.assignee_id or 'не назначен'}\n"
                f"Приоритет: {task.priority}; срок: {task.current_due_date}"
                f"{' (просрочена)' if is_overdue(task) else ''}\n"
                f"Команды: {', '.join(commands)}")
    if command == "comments":
        rows = (await db.scalars(select(TaskComment).where(TaskComment.task_id == task.id)
                                 .order_by(TaskComment.created_at.desc()).limit(10))).all()
        return "\n".join(f"{row.author_user_id}: {row.body[:250]}" for row in rows) or "Комментариев нет."
    if command == "history":
        rows = (await db.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id)
                                 .order_by(TaskEvent.sequence.desc()).limit(10))).all()
        return "\n".join(f"#{row.sequence} {row.type} · {row.actor_user_id}" for row in rows) or "Истории нет."
    if command == "comment":
        comment = await add_comment(db, user, task, extra, request_id)
        return f"Комментарий к {task.task_key} сохранён ({comment.id})."
    action = ACTION_COMMANDS[command]
    reason = extra if action in {"reject", "block", "cancel", "admin-approve"} else None
    comment = extra if action == "request-rework" else None
    result = extra if action == "submit-review" else None
    confirmed = False
    if action == "admin-approve":
        marker, _, reason = extra.partition(" ")
        confirmed = marker == "CONFIRM"
    updated = await transition(db, user, task, action, task.version,
                               reason=reason, comment=comment, result=result,
                               author_unavailable_confirmed=confirmed,
                               request_id=request_id)
    qa_label = "пройдена" if updated.qa_review_status == "DONE" and updated.status in {"REVIEW", "DONE"} else \
        "ожидает повторной проверки" if updated.qa_review_status == "DONE" else updated.qa_review_status or "не назначен"
    return f"{updated.task_key}: {updated.status}; QA: {qa_label}."


def build_task_router() -> Router:
    router = Router()
    router.callback_query.register(assignment_callback,
                                   lambda callback: bool(callback.data) and callback.data.startswith("task:"))
    router.message.register(refusal_reply, lambda message: bool(message.reply_to_message) and
                            bool(message.reply_to_message.text) and
                            message.reply_to_message.text.startswith("Ответьте на это сообщение причиной отказа от "))
    router.message.register(task_command, lambda message: bool(message.text) and message.text.startswith("/"))
    return router
