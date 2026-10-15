from pathlib import Path

import pytest
from pydantic import ValidationError

from taskbot_api.config import Settings


def test_database_url_can_be_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TASKBOT_DATABASE_URL", raising=False)
    settings = Settings(_env_file=None)
    assert settings.database_url is None


def test_database_url_rejects_non_postgres_driver() -> None:
    with pytest.raises(ValidationError, match="postgresql\\+asyncpg"):
        Settings(_env_file=None, database_url="sqlite:///taskbot.db")


def test_database_url_accepts_async_postgres() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://user:password@localhost:5432/taskbot",
    )
    assert settings.database_url.endswith("/taskbot")


def test_database_timeout_must_be_positive() -> None:
    with pytest.raises(ValidationError, match="must be positive"):
        Settings(_env_file=None, database_connect_timeout_seconds=0)


def test_example_env_is_valid() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    settings = Settings(_env_file=repository_root / ".env.example")
    assert settings.database_url is not None
    assert settings.database_url.startswith("postgresql+asyncpg://")

