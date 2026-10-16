from functools import lru_cache
from datetime import time
from typing import Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TASKBOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    database_url: str | None = None
    database_connect_timeout_seconds: float = 3
    bot_token: SecretStr | None = None
    telegram_bot_username: str | None = None
    webapp_url: str | None = None
    web_origin: str | None = None
    session_cookie_secure: bool = True
    session_hours: int = 24
    login_max_age_seconds: int = 300
    app_env: Literal["development", "production"] = "production"
    dev_login_enabled: bool = False
    dev_login_telegram_id: int | None = None
    dev_login_secret: SecretStr | None = None
    notification_delivery_enabled: bool = False
    notification_transport: Literal["dry_run", "telegram"] = "dry_run"
    worker_poll_seconds: float = 15
    worker_batch_size: int = 50
    worker_max_attempts: int = 5
    worker_retry_base_seconds: int = 30
    worker_lease_seconds: int = 120
    workdays: str = "0,1,2,3,4,5"
    workday_start: time = time(10)
    workday_end: time = time(19)
    reaction_work_minutes: int = 240

    @model_validator(mode="after")
    def validate_web_security(self):
        if self.webapp_url:
            url = urlsplit(self.webapp_url)
            if (url.scheme != "https" or not url.hostname or
                    url.hostname in {"localhost", "127.0.0.1", "::1"} or
                    url.username or url.password or
                    url.fragment):
                raise ValueError("TASKBOT_WEBAPP_URL must be a public HTTPS URL without credentials or fragment")
        if self.dev_login_enabled:
            if (self.app_env != "development" or
                    self.web_origin not in {"http://localhost:3000", "http://127.0.0.1:3000"} or
                    self.session_cookie_secure or
                    not self.dev_login_telegram_id or self.dev_login_telegram_id <= 0 or
                    self.dev_login_secret is None or
                    len(self.dev_login_secret.get_secret_value()) < 32):
                raise ValueError("dev login requires development, loopback web origin, local cookies, configured user and a 32-character secret")
        if self.session_hours <= 0 or self.login_max_age_seconds <= 0:
            raise ValueError("session and Telegram proof lifetimes must be positive")
        if (self.worker_poll_seconds <= 0 or self.worker_batch_size <= 0 or
                self.worker_max_attempts <= 0 or self.worker_retry_base_seconds <= 0 or
                self.worker_lease_seconds <= 0):
            raise ValueError("worker settings must be positive")
        days = self.workdays.split(",")
        if (not days or any(day not in {"0", "1", "2", "3", "4", "5", "6"} for day in days)
                or len(days) != len(set(days)) or self.workday_start >= self.workday_end
                or self.reaction_work_minutes <= 0):
            raise ValueError("invalid working calendar or reaction interval")
        if self.notification_delivery_enabled and self.notification_transport == "telegram" and not self.bot_token:
            raise ValueError("TASKBOT_BOT_TOKEN is required for Telegram delivery")
        if self.web_origin:
            if not self.web_origin.startswith(("http://", "https://")):
                raise ValueError("TASKBOT_WEB_ORIGIN must use HTTP or HTTPS")
            if self.web_origin.endswith("/") or "/" in self.web_origin.split("://", 1)[-1]:
                raise ValueError("TASKBOT_WEB_ORIGIN must be an origin without a path")
            if not self.session_cookie_secure and self.web_origin not in {
                "http://localhost:3000", "http://127.0.0.1:3000"
            }:
                raise ValueError("insecure session cookies are permitted only for local development")
        return self

    @field_validator("database_url")
    @classmethod
    def require_async_postgres(cls, value: str | None) -> str | None:
        if not value:
            return None
        try:
            url = make_url(value)
        except ArgumentError as exc:
            raise ValueError("TASKBOT_DATABASE_URL must be a valid SQLAlchemy URL") from exc
        if url.drivername != "postgresql+asyncpg" or not url.database:
            raise ValueError(
                "TASKBOT_DATABASE_URL must use postgresql+asyncpg and name a database"
            )
        return value

    @field_validator("database_connect_timeout_seconds")
    @classmethod
    def positive_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("TASKBOT_DATABASE_CONNECT_TIMEOUT_SECONDS must be positive")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()

