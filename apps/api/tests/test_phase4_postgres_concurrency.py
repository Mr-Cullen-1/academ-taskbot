"""Optional real PostgreSQL concurrency check on an explicit local test database.

Set TASKBOT_TEST_DATABASE_URL to a loopback postgresql+asyncpg URL whose
database name begins with taskbot_test_. This never reads TASKBOT_DATABASE_URL.
Each run creates and removes only its own randomly named schema.
"""

import asyncio
import os
from datetime import date, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from taskbot_api.db import Base, session_factory
from taskbot_api.models import Notification, Project, ProjectMember, TaskKeySequence, User, now
from taskbot_api.task_service import create_task
from taskbot_worker.delivery import (claim_notification, finish_notification,
                                     process_outbox)


def test_two_workers_claim_one_notification_on_local_postgres():
    raw = os.environ.get("TASKBOT_TEST_DATABASE_URL")
    if not raw:
        pytest.skip("Dedicated local PostgreSQL test URL is not configured")
    url = make_url(raw)
    if (url.drivername != "postgresql+asyncpg" or
            url.host not in {"localhost", "127.0.0.1", "::1"} or
            not (url.database or "").startswith("taskbot_test_")):
        pytest.skip("PostgreSQL concurrency test requires an explicit loopback taskbot_test_ database")

    async def scenario():
        schema = f"phase4_{uuid4().hex}"
        admin = create_async_engine(raw)
        engine = None
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            engine = create_async_engine(raw, connect_args={
                "server_settings": {"search_path": schema}})
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = session_factory(engine)
            async with factory() as db:
                async with db.begin():
                    author = User(telegram_user_id=101, telegram_chat_id=101,
                                  phone="+998101", name="Author", status="ACTIVE")
                    assignee = User(telegram_user_id=102, telegram_chat_id=102,
                                    phone="+998102", name="Assignee", status="ACTIVE")
                    db.add_all([author, assignee])
                    await db.flush()
                    project = Project(name="Concurrency", owner_user_id=author.id,
                                      description="")
                    db.add(project)
                    await db.flush()
                    db.add_all([ProjectMember(project_id=project.id, user_id=author.id),
                                ProjectMember(project_id=project.id, user_id=assignee.id),
                                TaskKeySequence(id=1, next_number=1001)])
                    await db.flush()
                    task = await create_task(db, author, project_id=project.id,
                                             title="Concurrent claim", description="",
                                             assignee_id=assignee.id, qa_reviewer_id=None,
                                             priority="MEDIUM", type="DEVELOPMENT",
                                             due_date=date(2027, 1, 1))
                    task_id = task.id
            instant = now() + timedelta(seconds=2)
            assert await process_outbox(factory, at=instant) == 1
            # Hold the row lock in a separate transaction. A competing worker
            # must skip it promptly instead of waiting for this transaction.
            async with factory() as locked_db:
                async with locked_db.begin():
                    locked = await locked_db.scalar(select(Notification).where(
                        Notification.task_id == task_id).with_for_update())
                    assert locked is not None
                    skipped = await asyncio.wait_for(claim_notification(
                        factory, at=instant, lease_seconds=10, max_attempts=3),
                        timeout=2)
                    assert skipped is None
            first, second = await asyncio.gather(
                claim_notification(factory, at=instant, lease_seconds=10, max_attempts=3),
                claim_notification(factory, at=instant, lease_seconds=10, max_attempts=3))
            assert sum(row is not None and row[3] for row in (first, second)) == 1
            original = first if first is not None else second
            successor = await claim_notification(factory, at=instant + timedelta(seconds=11),
                                                 lease_seconds=10, max_attempts=3)
            assert successor is not None and successor[5] == original[5] + 1
            await finish_notification(factory, original[0],
                                      at=instant + timedelta(seconds=12), outcome="SENT",
                                      error_code=None, claim_attempt=original[5],
                                      max_attempts=3, retry_base_seconds=30)
            async with factory() as db:
                row = await db.scalar(select(Notification).where(Notification.task_id == task_id))
                assert row.state == "SENDING" and row.attempts == successor[5]
            await finish_notification(factory, successor[0],
                                      at=instant + timedelta(seconds=12), outcome="SENT",
                                      error_code=None, claim_attempt=successor[5],
                                      max_attempts=3, retry_base_seconds=30)
            async with factory() as db:
                row = await db.scalar(select(Notification).where(Notification.task_id == task_id))
                assert row.state == "SENT"
        finally:
            if engine is not None:
                await engine.dispose()
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await admin.dispose()

    asyncio.run(scenario())
