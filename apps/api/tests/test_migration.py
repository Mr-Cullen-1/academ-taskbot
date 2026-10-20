"""Apply all Alembic revisions to a disposable SQLite database."""

import importlib.util
from io import StringIO
from pathlib import Path

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from taskbot_api.db import Base
import taskbot_api.models  # noqa: F401


def test_migrations_create_mapped_tables():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            for name in ("0001_phase2_access.py", "0002_phase3_tasks.py",
                         "0003_phase4_notifications.py", "0004_profile_avatar.py",
                         "0005_outbox_retry.py", "0006_mvp_entities.py"):
                path = Path(__file__).parents[1] / "migrations" / "versions" / name
                spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.upgrade()
        inspector = inspect(connection)
        assert set(inspector.get_table_names()) == set(Base.metadata.tables)
        for table in Base.metadata.tables.values():
            assert {column["name"] for column in inspector.get_columns(table.name)} == set(table.columns.keys())
    engine.dispose()


def test_outbox_retry_migration_generates_postgres_sql_offline():
    path = Path(__file__).parents[1] / "migrations" / "versions" / "0005_outbox_retry.py"
    spec = importlib.util.spec_from_file_location("outbox_retry", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    with Operations.context(context):
        module.upgrade()
    sql = output.getvalue()
    assert "ALTER TABLE outbox_events ADD COLUMN attempts INTEGER DEFAULT '0' NOT NULL" in sql
    assert "ALTER TABLE outbox_events ADD COLUMN last_error_code VARCHAR(80)" in sql


def test_mvp_migration_generates_postgres_sql_offline():
    path = Path(__file__).parents[1] / "migrations" / "versions" / "0006_mvp_entities.py"
    spec = importlib.util.spec_from_file_location("mvp_entities", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "0005_outbox_retry"
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    with Operations.context(context):
        module.upgrade()
    sql = output.getvalue()
    for name in ("project_teams", "sprints", "task_watchers", "task_dependencies",
                 "deadline_requests", "deadline_changes", "personal_reminders",
                 "task_attachments"):
        assert f"CREATE TABLE {name}" in sql
    assert "ALTER TABLE notifications ADD COLUMN read_at" in sql
    assert "DROP TABLE" not in sql
