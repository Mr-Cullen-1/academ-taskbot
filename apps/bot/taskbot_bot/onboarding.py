"""Private Telegram access requests and Super Admin decisions."""

from uuid import UUID

from aiogram import Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from taskbot_api.models import Project, TelegramUpdate, User
from taskbot_api.registration import (
    REQUESTABLE_ROLES, decide_pending_user, requested_access, submit_access_request,
)
from taskbot_api.security import role_codes


ROLE_CHOICES = (("PM", "PM"), ("Team Lead", "TEAM_LEAD"),
                ("Предметник-разработчик", "SUBJECT_DEVELOPER"), ("QA", "QA"))
ROLE_BY_DIGIT = {str(index): role for index, (_, role) in enumerate(ROLE_CHOICES, 1)}


def role_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"reg:role:{index}")]
        for index, (label, _) in enumerate(ROLE_CHOICES, 1)
    ])


async def project_keyboard(db: AsyncSession, digit: str, page: int = 0) -> InlineKeyboardMarkup:
    rows = (await db.scalars(select(Project).order_by(Project.name, Project.id)
                             .offset(page * 12).limit(13))).all()
    buttons = [[InlineKeyboardButton(text=project.name[:55],
                                    callback_data=f"reg:project:{digit}:{project.id}")]
               for project in rows[:12]]
    buttons.append([InlineKeyboardButton(text="Пока без проекта",
                                         callback_data=f"reg:project:{digit}:none")])
    pages = []
    if page:
        pages.append(InlineKeyboardButton(text="←", callback_data=f"reg:page:{digit}:{page - 1}"))
    if len(rows) > 12:
        pages.append(InlineKeyboardButton(text="→", callback_data=f"reg:page:{digit}:{page + 1}"))
    if pages:
        buttons.append(pages)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def pending_prompt(db: AsyncSession, user: User) -> tuple[str, InlineKeyboardMarkup | None]:
    request = await requested_access(db, user.id)
    if request:
        return "Заявка отправлена. Дождитесь решения Super Admin.", None
    return "Выберите запрашиваемую роль. Super Admin подтвердит роль и доступ к проекту.", role_keyboard()


async def registration_callback(callback: CallbackQuery, event_update: Update,
                                registration_factory: async_sessionmaker[AsyncSession]) -> None:
    if (not isinstance(callback.message, Message) or callback.message.chat.type != "private" or
            callback.message.chat.id != callback.from_user.id):
        await callback.answer("Действие доступно только в личном чате.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) < 3 or parts[0] != "reg":
        await callback.answer("Неверная кнопка.", show_alert=True)
        return
    action = parts[1]
    if action in {"approve", "reject"}:
        await admin_decision(callback, event_update, registration_factory, action, parts)
        return
    async with registration_factory() as db:
        user = await db.scalar(select(User).where(User.telegram_user_id == callback.from_user.id))
        if user is None or user.status != "PENDING" or await requested_access(db, user.id):
            await callback.answer("Заявка уже отправлена или недоступна.", show_alert=True)
            return
        digit = parts[2]
        if digit not in ROLE_BY_DIGIT:
            await callback.answer("Неверная роль.", show_alert=True)
            return
        if action == "role" and len(parts) == 3:
            page = 0
        elif action == "page" and len(parts) == 4:
            try:
                page = int(parts[3])
            except ValueError:
                page = -1
            if not 0 <= page <= 1000:
                await callback.answer("Неверная страница.", show_alert=True)
                return
        elif action == "project" and len(parts) == 4:
            try:
                project_id = None if parts[3] == "none" else UUID(parts[3])
            except ValueError:
                await callback.answer("Неверный проект.", show_alert=True)
                return
            await db.rollback()  # Release the read transaction before the write session.
            result = await submit_access_request(
                registration_factory, update_id=event_update.update_id,
                telegram_user_id=callback.from_user.id,
                role=ROLE_BY_DIGIT[digit], project_id=project_id,
            )
            if result == "REQUEST_SUBMITTED":
                await callback.message.edit_text("Заявка отправлена. Дождитесь решения Super Admin.")
                await callback.answer()
            else:
                await callback.answer({
                    "ADMIN_UNAVAILABLE": "Администратор пока недоступен. Повторите позже.",
                    "PROJECT_NOT_FOUND": "Проект больше не существует. Выберите другой.",
                }.get(result, "Заявка уже обработана."), show_alert=True)
            return
        else:
            await callback.answer("Неверная кнопка.", show_alert=True)
            return
        markup = await project_keyboard(db, digit, page)
    await callback.message.edit_text("Выберите проект, к которому запрашиваете доступ:",
                                     reply_markup=markup)
    await callback.answer()


async def admin_decision(callback: CallbackQuery, event_update: Update,
                         factory: async_sessionmaker[AsyncSession], action: str,
                         parts: list[str]) -> None:
    if len(parts) != 3:
        await callback.answer("Неверная кнопка.", show_alert=True)
        return
    try:
        target_id = UUID(parts[2])
    except ValueError:
        await callback.answer("Неверная заявка.", show_alert=True)
        return
    async with factory() as db:
        try:
            async with db.begin():
                actor = await db.scalar(select(User).where(
                    User.telegram_user_id == callback.from_user.id))
                if actor is None or actor.status != "ACTIVE" or \
                        "SUPER_ADMIN" not in await role_codes(db, actor.id):
                    raise HTTPException(403, "FORBIDDEN")
                db.add(TelegramUpdate(update_id=event_update.update_id,
                                      response_code="REGISTRATION_DECISION"))
                await db.flush()
                target = await db.get(User, target_id, with_for_update=True)
                if target is None or target.status != "PENDING":
                    raise HTTPException(409, "ALREADY_DECIDED")
                request = await requested_access(db, target.id)
                if request is None or request.get("role") not in REQUESTABLE_ROLES:
                    raise HTTPException(409, "REQUEST_NOT_READY")
                project_id = UUID(request["project_id"]) if request.get("project_id") else None
                await decide_pending_user(
                    db, actor, target, status="ACTIVE" if action == "approve" else "REJECTED",
                    roles={request["role"]} if action == "approve" else None,
                    project_id=project_id if action == "approve" else None,
                )
        except IntegrityError:
            await db.rollback()
            if await db.get(TelegramUpdate, event_update.update_id) is None:
                raise
            await callback.answer("Решение уже обработано.", show_alert=True)
            return
        except (HTTPException, ValueError) as exc:
            await db.rollback()
            detail = exc.detail if isinstance(exc, HTTPException) else "INVALID_REQUEST"
            await callback.answer(f"Действие отклонено: {detail}.", show_alert=True)
            return
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Заявка одобрена." if action == "approve" else "Заявка отклонена.")


def build_onboarding_router() -> Router:
    router = Router()
    router.callback_query.register(registration_callback,
                                   lambda callback: bool(callback.data) and
                                   callback.data.startswith("reg:"))
    return router
