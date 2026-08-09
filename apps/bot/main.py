"""Aiogram dispatcher factory and bot process entry point."""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import BaseStorage, DefaultKeyBuilder
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from core.config import Settings, get_settings
from core.database.session import create_engine, create_session_factory
from core.logging import configure_logging
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot.handlers import fallback, menu, navigation, presentation, services, start
from apps.bot.localization import Localizer
from apps.bot.middleware import DatabaseSessionMiddleware, OwnerOnlyMiddleware

logger = logging.getLogger(__name__)


def validate_runtime_credentials(settings: Settings) -> None:
    if not settings.bot_enabled:
        return
    if not settings.telegram_bot_token.get_secret_value():
        raise ValueError("TELEGRAM_BOT_TOKEN is required")
    if (
        settings.ai_provider in {"openai", "openai_compatible"}
        and not settings.ai_api_key.get_secret_value()
    ):
        raise ValueError("AI_API_KEY is required for the configured AI provider")
    if settings.bot_private_mode and settings.owner_telegram_id is None:
        raise ValueError("OWNER_TELEGRAM_ID is required when BOT_PRIVATE_MODE=true")


def build_dispatcher(
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> Dispatcher:
    storage: BaseStorage
    if settings.app_env == "test":
        storage = MemoryStorage()
    else:
        storage = RedisStorage.from_url(
            settings.redis_url,
            key_builder=DefaultKeyBuilder(with_bot_id=True),
            state_ttl=60 * 60 * 24,
            data_ttl=60 * 60 * 24,
        )
    dispatcher = Dispatcher(storage=storage, disable_fsm=True)
    dispatcher.update.outer_middleware(
        OwnerOnlyMiddleware(
            private_mode=settings.bot_private_mode,
            owner_telegram_id=settings.owner_telegram_id,
        )
    )
    dispatcher.update.outer_middleware(dispatcher.fsm)
    dispatcher.update.outer_middleware(DatabaseSessionMiddleware(session_factory))
    dispatcher["localizer"] = Localizer.load_default()
    dispatcher["settings"] = settings
    dispatcher.include_router(start.router)
    dispatcher.include_router(menu.router)
    # Top-level feature entries must precede state-specific catch-all handlers.
    dispatcher.include_router(navigation.router)
    dispatcher.include_router(services.router)
    dispatcher.include_router(presentation.router)
    # Catch-all must stay last so valid callbacks reach their feature router.
    dispatcher.include_router(fallback.router)
    return dispatcher


async def run(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    configure_logging(debug=settings.debug)
    if not settings.bot_enabled:
        logger.warning("Telegram bot consumer is disabled by BOT_ENABLED=false")
        await asyncio.Event().wait()
        return
    validate_runtime_credentials(settings)
    token = settings.telegram_bot_token.get_secret_value()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    bot = Bot(token=token)
    dispatcher = build_dispatcher(settings=settings, session_factory=factory)
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
