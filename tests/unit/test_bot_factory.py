from pathlib import Path

from aiogram.fsm.middleware import FSMContextMiddleware
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apps.bot.main import build_dispatcher
from apps.bot.middleware import DatabaseSessionMiddleware, OwnerOnlyMiddleware
from core.config import Settings


def test_dispatcher_builds_without_telegram_or_redis_network(tmp_path: Path) -> None:
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}",
        secret_key=SecretStr("x" * 32),
    )
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    dispatcher = build_dispatcher(settings=settings, session_factory=factory)

    assert "message" in dispatcher.resolve_used_update_types()
    assert "callback_query" in dispatcher.resolve_used_update_types()
    middlewares = dispatcher.update.outer_middleware._middlewares
    owner_index = next(
        index
        for index, middleware in enumerate(middlewares)
        if isinstance(middleware, OwnerOnlyMiddleware)
    )
    database_index = next(
        index
        for index, middleware in enumerate(middlewares)
        if isinstance(middleware, DatabaseSessionMiddleware)
    )
    fsm_index = next(
        index
        for index, middleware in enumerate(middlewares)
        if isinstance(middleware, FSMContextMiddleware)
    )
    assert owner_index < fsm_index < database_index
