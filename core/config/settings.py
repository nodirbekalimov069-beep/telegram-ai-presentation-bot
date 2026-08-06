"""Validated environment configuration."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_PLACEHOLDER_MARKERS = (
    "change-me",
    "changeme",
    "replace-with",
    "placeholder",
    "example",
    "your-",
    "your_",
    "development",
    "password",
)


def _validate_production_secret(name: str, value: str, *, min_length: int) -> None:
    stripped = value.strip()
    normalized = stripped.lower()
    if (
        len(stripped) < min_length
        or len(set(stripped)) < 12
        or any(marker in normalized for marker in _PLACEHOLDER_MARKERS)
    ):
        raise ValueError(f"{name} must be a strong, non-placeholder production secret")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Telegram AI Presentation Bot"
    app_env: Literal["development", "test", "production"] = "development"
    debug: bool = False
    secret_key: SecretStr = SecretStr("development-only-change-me")
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    public_base_url: str = "http://localhost:8000"

    database_url: str = "sqlite+aiosqlite:///./presentation_bot.db"
    redis_url: str = "redis://localhost:6379/0"

    telegram_bot_token: SecretStr = SecretStr("")
    telegram_mode: Literal["polling", "webhook"] = "polling"
    telegram_webhook_secret: SecretStr = SecretStr("")
    telegram_admin_ids: str = ""
    admin_api_token: SecretStr = SecretStr("")
    bot_private_mode: bool = True
    owner_telegram_id: int | None = None

    ai_provider: Literal["mock", "openai", "openai_compatible"] = "mock"
    ai_base_url: str = "https://api.openai.com/v1"
    ai_api_key: SecretStr = SecretStr("")
    ai_model: str = "gpt-4.1-mini"
    ai_timeout_seconds: float = 60.0
    ai_max_retries: int = Field(default=3, ge=0, le=10)
    ai_retry_base_delay_seconds: float = Field(default=0.5, ge=0, le=10)

    payment_provider: Literal["mock", "click", "payme"] = "mock"
    mock_payment_secret: SecretStr = SecretStr("development-mock-secret")
    click_merchant_id: SecretStr = SecretStr("")
    click_service_id: SecretStr = SecretStr("")
    click_secret_key: SecretStr = SecretStr("")
    payme_merchant_id: SecretStr = SecretStr("")
    payme_login: str = "Paycom"
    payme_secret_key: SecretStr = SecretStr("")

    storage_backend: Literal["local", "s3"] = "local"
    local_storage_root: Path = Path("./storage")
    s3_endpoint_url: str | None = None
    s3_bucket: str = "presentations"
    s3_access_key: SecretStr = SecretStr("")
    s3_secret_key: SecretStr = SecretStr("")
    s3_region: str = "us-east-1"

    libreoffice_binary: str = "soffice"
    presentation_renderer: Path = Path("apps/presentation_engine/render.mjs")
    generation_work_root: Path = Path("./tmp/generation")
    node_binary: str = "node"
    max_topic_length: int = Field(default=500, ge=20, le=2000)
    max_upload_bytes: int = Field(default=20_000_000, ge=1_000_000)
    generation_max_retries: int = Field(default=3, ge=0, le=10)

    @field_validator("owner_telegram_id", mode="before")
    @classmethod
    def normalize_blank_owner_id(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def admin_id_set(self) -> frozenset[int]:
        if not self.telegram_admin_ids.strip():
            return frozenset()
        return frozenset(
            int(item.strip()) for item in self.telegram_admin_ids.split(",") if item.strip()
        )

    @model_validator(mode="after")
    def production_credentials_must_be_explicit(self) -> "Settings":
        if self.app_env != "production":
            return self
        _validate_production_secret(
            "SECRET_KEY",
            self.secret_key.get_secret_value(),
            min_length=32,
        )
        if not self.telegram_bot_token.get_secret_value():
            raise ValueError("TELEGRAM_BOT_TOKEN is required in production")
        if self.ai_provider == "mock":
            raise ValueError("mock AI provider must not be enabled in production")
        if not self.ai_api_key.get_secret_value():
            raise ValueError("AI_API_KEY is required for the configured AI provider")
        if self.payment_provider == "mock":
            raise ValueError("mock payments must not be enabled in production")
        if self.payment_provider == "click" and not all(
            value.get_secret_value()
            for value in (self.click_merchant_id, self.click_service_id, self.click_secret_key)
        ):
            raise ValueError("Click credentials are required")
        if self.payment_provider == "payme" and not all(
            value.get_secret_value() for value in (self.payme_merchant_id, self.payme_secret_key)
        ):
            raise ValueError("Payme credentials are required")
        if self.telegram_mode == "webhook":
            _validate_production_secret(
                "TELEGRAM_WEBHOOK_SECRET",
                self.telegram_webhook_secret.get_secret_value(),
                min_length=16,
            )
        _validate_production_secret(
            "ADMIN_API_TOKEN",
            self.admin_api_token.get_secret_value(),
            min_length=24,
        )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
