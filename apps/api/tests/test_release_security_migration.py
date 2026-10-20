"""Offline checks for the release access-control migration."""

import importlib.util
from io import StringIO
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

from taskbot_api.db import Base
import taskbot_api.models  # noqa: F401


def load_migration():
    path = Path(__file__).parents[1] / "migrations" / "versions" / "0007_restrict_data_api.py"
    spec = importlib.util.spec_from_file_location("restrict_data_api", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_migration_covers_every_taskbot_table():
    migration = load_migration()
    assert migration.down_revision == "0006_mvp_entities"
    assert set(migration.TASKBOT_TABLES) == set(Base.metadata.tables)
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    with Operations.context(context):
        migration.upgrade()
    sql = output.getvalue()
    for table in migration.TASKBOT_TABLES:
        assert f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY' in sql
        assert f'REVOKE ALL PRIVILEGES ON TABLE public."{table}" FROM anon, authenticated' in sql
    assert "ALTER DEFAULT PRIVILEGES IN SCHEMA public" in sql
    assert "GRANT " not in sql


def test_release_migration_does_not_reopen_data_api_on_downgrade():
    with pytest.raises(RuntimeError, match="not reversible"):
        load_migration().downgrade()
