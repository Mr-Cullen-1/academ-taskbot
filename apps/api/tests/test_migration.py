"""Apply the real Phase 2 Alembic revision to a disposable SQLite database."""

import importlib.util
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
                         "0003_phase4_notifications.py", "0004_profile_avatar.py"):
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
