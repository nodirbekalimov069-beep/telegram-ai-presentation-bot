from types import SimpleNamespace
from unittest.mock import AsyncMock

import core.database.models  # noqa: F401
import pytest
from aiogram import Bot
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apps.api.main import create_app
from apps.bot.models import TelegramUpdateInbox
from core.config import Settings
from core.database.base import Base


@pytest.mark.asyncio
async def test_webhook_startup_rejects_private_mode_without_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(
        app_env="test",
        bot_enabled=True,
        telegram_mode="webhook",
        telegram_bot_token=SecretStr("123456:abcdefghijklmnopqrstuvwxyzABCDE"),
        telegram_webhook_secret=SecretStr("webhook-secret"),
        ai_provider="mock",
        bot_private_mode=True,
        owner_telegram_id=None,
    )
    set_webhook = AsyncMock()
    monkeypatch.setattr(Bot, "set_webhook", set_webhook)
    app = create_app(settings=settings, session_factory=factory)

    with pytest.raises(ValueError, match="OWNER_TELEGRAM_ID is required"):
        async with app.router.lifespan_context(app):
            pass

    set_webhook.assert_not_awaited()
    await engine.dispose()


@pytest.mark.asyncio
async def test_private_webhook_rejects_non_owner_before_durable_inbox() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(
        app_env="test",
        bot_enabled=True,
        telegram_mode="webhook",
        telegram_bot_token=SecretStr("123456:abcdefghijklmnopqrstuvwxyzABCDE"),
        telegram_webhook_secret=SecretStr("webhook-secret"),
        ai_provider="mock",
        bot_private_mode=True,
        owner_telegram_id=42,
    )
    app = create_app(settings=settings, session_factory=factory)
    feed_update = AsyncMock()
    app.state.telegram_dispatcher = SimpleNamespace(feed_update=feed_update)
    app.state.telegram_bot = SimpleNamespace()
    payload = {
        "update_id": 1001,
        "message": {
            "message_id": 1,
            "date": 1,
            "chat": {"id": 99, "type": "private"},
            "from": {"id": 99, "is_bot": False, "first_name": "Other"},
            "text": "/start",
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/telegram/webhook",
            json=payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    feed_update.assert_awaited_once()
    async with factory() as session:
        inbox_count = await session.scalar(select(func.count(TelegramUpdateInbox.id)))
    assert inbox_count == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_disabled_bot_rejects_webhook_without_processing() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app(
        settings=Settings(app_env="test", bot_enabled=False),
        session_factory=factory,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/telegram/webhook", content=b"{}")

    assert response.status_code == 503
    assert response.json()["detail"] == "Telegram bot is disabled"
    await engine.dispose()
