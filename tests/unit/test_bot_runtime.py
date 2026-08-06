import pytest
from apps.bot.main import validate_runtime_credentials
from core.config import Settings
from pydantic import SecretStr


def test_runtime_rejects_missing_telegram_token() -> None:
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        validate_runtime_credentials(Settings(app_env="test", telegram_bot_token=SecretStr("")))


def test_openai_runtime_rejects_missing_ai_key() -> None:
    settings = Settings(
        app_env="test",
        telegram_bot_token=SecretStr("123456:abcdefghijklmnopqrstuvwxyzABCDE"),
        ai_provider="openai_compatible",
    )
    with pytest.raises(ValueError, match="AI_API_KEY"):
        validate_runtime_credentials(settings)
