"""Explicitly started Phase 4 worker. Importing the API never starts delivery."""

import argparse
import asyncio
import logging

from aiogram import Bot

from taskbot_api.config import Settings
from taskbot_api.db import build_engine, session_factory
from taskbot_api.models import now
from taskbot_worker.delivery import (
    DryRunTransport, MessageTransport, TelegramTransport, deliver_notifications,
    process_outbox,
)
from taskbot_worker.scheduler import (
    schedule_deadlines, schedule_reaction_reminders, schedule_weekly_reports,
)


log = logging.getLogger(__name__)


async def worker_tick(factory, settings: Settings, transport: MessageTransport,
                      *, at=None) -> dict:
    instant = at or now()
    counts = {
        "outbox": await process_outbox(factory, limit=settings.worker_batch_size, at=instant),
        "deadlines": await schedule_deadlines(factory, at=instant),
        "reaction": await schedule_reaction_reminders(factory, settings, at=instant),
        "reports": await schedule_weekly_reports(factory, at=instant),
    }
    counts["delivery"] = await deliver_notifications(factory, transport, settings, at=instant)
    return counts


async def run_worker(settings: Settings, *, once: bool = False) -> None:
    engine = build_engine(settings)
    if engine is None:
        raise RuntimeError("TASKBOT_DATABASE_URL is required for the worker")
    bot = None
    try:
        if settings.notification_delivery_enabled and settings.notification_transport == "telegram":
            bot = Bot(token=settings.bot_token.get_secret_value())
            transport: MessageTransport = TelegramTransport(bot)
        else:
            transport = DryRunTransport()
        factory = session_factory(engine)
        while True:
            counts = await worker_tick(factory, settings, transport)
            log.info("Worker tick outbox=%s deadlines=%s reaction=%s reports=%s delivery=%s",
                     counts["outbox"], counts["deadlines"], counts["reaction"],
                     counts["reports"], counts["delivery"])
            if once:
                break
            await asyncio.sleep(settings.worker_poll_seconds)
    finally:
        if bot is not None:
            await bot.session.close()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Taskbot background worker")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="validate imports and settings without connecting")
    mode.add_argument("--once", action="store_true", help="run one explicit worker tick")
    mode.add_argument("--run", action="store_true", help="run until interrupted")
    args = parser.parse_args()
    settings = Settings()
    if args.check:
        print("Worker handlers loaded; no database or Telegram request was made.")
        return
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        asyncio.run(run_worker(settings, once=args.once))
    except KeyboardInterrupt:
        log.info("Worker stopped.")


if __name__ == "__main__":
    main()
