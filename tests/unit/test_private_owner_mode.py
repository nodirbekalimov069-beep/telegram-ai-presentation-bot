from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from apps.bot.main import validate_runtime_credentials
from apps.bot.middleware import OwnerOnlyMiddleware
from core.config import Settings


class RecordingMessage:
    def __init__(self) -> None:
        self.answers: list[str] = []

    async def answer(self, text: str) -> None:
        self.answers.append(text)


@pytest.mark.asyncio
async def test_private_mode_blocks_non_owner_before_downstream_handler() -> None:
    message = RecordingMessage()
    event = SimpleNamespace(message=message, callback_query=None)
    downstream = AsyncMock(return_value="handled")
    middleware = OwnerOnlyMiddleware(private_mode=True, owner_telegram_id=42)

    result = await middleware(
        downstream,
        event,
        {"event_from_user": SimpleNamespace(id=99)},
    )

    assert result is None
    downstream.assert_not_awaited()
    assert message.answers == ["🔒 Bot hozir faqat loyiha egasi uchun private test rejimida."]


@pytest.mark.asyncio
async def test_private_mode_allows_owner_update() -> None:
    event = SimpleNamespace(message=RecordingMessage(), callback_query=None)
    downstream = AsyncMock(return_value="handled")
    middleware = OwnerOnlyMiddleware(private_mode=True, owner_telegram_id=42)

    result = await middleware(
        downstream,
        event,
        {"event_from_user": SimpleNamespace(id=42)},
    )

    assert result == "handled"
    downstream.assert_awaited_once_with(event, {"event_from_user": SimpleNamespace(id=42)})


def test_runtime_rejects_private_mode_without_owner_id() -> None:
    settings = Settings(
        app_env="test",
        telegram_bot_token=SecretStr("123456:abcdefghijklmnopqrstuvwxyzABCDE"),
        bot_private_mode=True,
        owner_telegram_id=None,
    )

    with pytest.raises(ValueError, match="OWNER_TELEGRAM_ID"):
        validate_runtime_credentials(settings)


def test_runtime_accepts_private_owner_with_real_ai_configuration() -> None:
    settings = Settings(
        app_env="test",
        telegram_bot_token=SecretStr("123456:abcdefghijklmnopqrstuvwxyzABCDE"),
        bot_private_mode=True,
        owner_telegram_id=42,
        ai_provider="openai",
        ai_api_key=SecretStr("test-openai-key"),
    )

    validate_runtime_credentials(settings)


def test_blank_owner_env_value_reaches_clear_runtime_validation() -> None:
    settings = Settings(
        app_env="test",
        telegram_bot_token=SecretStr("123456:abcdefghijklmnopqrstuvwxyzABCDE"),
        bot_private_mode=True,
        owner_telegram_id="",  # type: ignore[arg-type]
    )

    assert settings.owner_telegram_id is None
    with pytest.raises(ValueError, match="OWNER_TELEGRAM_ID"):
        validate_runtime_credentials(settings)
