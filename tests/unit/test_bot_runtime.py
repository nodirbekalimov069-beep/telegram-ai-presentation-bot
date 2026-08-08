from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from apps.bot.main import run, validate_runtime_credentials
from core.config import Settings
from pydantic import SecretStr


def test_bot_is_disabled_by_default() -> None:
    assert Settings(app_env="test").bot_enabled is False


def test_disabled_runtime_does_not_require_external_credentials() -> None:
    validate_runtime_credentials(Settings(app_env="test", bot_enabled=False))


@pytest.mark.asyncio
async def test_disabled_runtime_stays_idle_without_creating_a_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wait = AsyncMock()
    event = SimpleNamespace(wait=wait)
    bot_constructor = Mock()
    monkeypatch.setattr("apps.bot.main.asyncio.Event", lambda: event)
    monkeypatch.setattr("apps.bot.main.Bot", bot_constructor)

    await run(Settings(app_env="test", bot_enabled=False))

    wait.assert_awaited_once_with()
    bot_constructor.assert_not_called()


def test_runtime_rejects_missing_telegram_token() -> None:
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        validate_runtime_credentials(
            Settings(app_env="test", bot_enabled=True, telegram_bot_token=SecretStr(""))
        )


def test_openai_runtime_rejects_missing_ai_key() -> None:
    settings = Settings(
        app_env="test",
        bot_enabled=True,
        telegram_bot_token=SecretStr("123456:abcdefghijklmnopqrstuvwxyzABCDE"),
        ai_provider="openai_compatible",
    )
    with pytest.raises(ValueError, match="AI_API_KEY"):
        validate_runtime_credentials(settings)
