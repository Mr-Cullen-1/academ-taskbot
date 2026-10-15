"""Telegram registration and task actions."""

import argparse
import asyncio

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from taskbot_api.config import get_settings
from taskbot_api.db import build_engine, session_factory
from taskbot_api.registration import RegistrationEvent, process_registration
from taskbot_bot.tasks import build_task_router


def contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Поделиться контактом", request_contact=True)]],
                               resize_keyboard=True, one_time_keyboard=True)


async def handle_registration(message: Message, event_update: Update,
                              registration_factory: async_sessionmaker[AsyncSession], kind: str) -> None:
    if not message.from_user or message.chat.type != "private":
        return
    contact = message.contact
    result = await process_registration(registration_factory, RegistrationEvent(
        update_id=event_update.update_id, telegram_user_id=message.from_user.id,
        telegram_chat_id=message.chat.id, kind=kind,
        contact_user_id=contact.user_id if contact else None,
        phone=contact.phone_number if contact else None,
        name=message.text if kind == "NAME" else None,
    ))
    if result.duplicate:
        return
    replies = {
        "CONTACT_REQUIRED": ("Нажмите кнопку и поделитесь своим контактом.", contact_keyboard()),
        "NAME_REQUIRED": ("Введите ваше имя.", ReplyKeyboardRemove()),
        "INVALID_CONTACT": ("Нужен ваш собственный контакт через кнопку Telegram.", contact_keyboard()),
        "INVALID_NAME": ("Введите имя длиной от 2 до 160 символов.", ReplyKeyboardRemove()),
        "ACCOUNT_PENDING": ("Заявка отправлена. Дождитесь одобрения администратора.", ReplyKeyboardRemove()),
        "ACCOUNT_ACTIVE": ("Ваш аккаунт активен. Войдите в веб-панель через Telegram.", ReplyKeyboardRemove()),
        "ACCOUNT_REJECTED": ("Заявка отклонена. Обратитесь к администратору.", ReplyKeyboardRemove()),
        "ACCOUNT_BLOCKED": ("Доступ заблокирован. Обратитесь к администратору.", ReplyKeyboardRemove()),
        "START_REQUIRED": ("Сначала отправьте /start.", ReplyKeyboardRemove()),
    }
    text, markup = replies.get(result.code, ("Отправьте /start, чтобы начать регистрацию.", ReplyKeyboardRemove()))
    await message.answer(text, reply_markup=markup)


async def start(message: Message, event_update: Update,
                registration_factory: async_sessionmaker[AsyncSession]) -> None:
    await handle_registration(message, event_update, registration_factory, "START")


async def contact(message: Message, event_update: Update,
                  registration_factory: async_sessionmaker[AsyncSession]) -> None:
    await handle_registration(message, event_update, registration_factory, "CONTACT")


async def name(message: Message, event_update: Update,
               registration_factory: async_sessionmaker[AsyncSession]) -> None:
    await handle_registration(message, event_update, registration_factory, "NAME")


def build_dispatcher() -> Dispatcher:
    registration_router = Router()
    registration_router.message.register(start, CommandStart())
    registration_router.message.register(contact, lambda message: message.contact is not None)
    name_router = Router()
    name_router.message.register(name, lambda message: bool(message.text) and not message.text.startswith("/"))
    dispatcher = Dispatcher()
    dispatcher.include_router(registration_router)
    dispatcher.include_router(build_task_router())
    dispatcher.include_router(name_router)
    return dispatcher


async def run_polling() -> None:
    settings = get_settings()
    token = settings.bot_token
    if token is None or not token.get_secret_value():
        raise SystemExit("TASKBOT_BOT_TOKEN is required to start polling")
    engine = build_engine(settings)
    if engine is None:
        raise SystemExit("TASKBOT_DATABASE_URL is required to start polling")
    try:
        async with Bot(token=token.get_secret_value()) as bot:
            await build_dispatcher().start_polling(bot, registration_factory=session_factory(engine),
                                                   allowed_updates=["message", "callback_query"])
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Taskbot Telegram bot")
    parser.add_argument("--check", action="store_true", help="load handlers without contacting Telegram")
    args = parser.parse_args()
    if args.check:
        build_dispatcher()
        print("Bot handlers loaded; polling was not started.")
        return
    asyncio.run(run_polling())


if __name__ == "__main__":
    main()
