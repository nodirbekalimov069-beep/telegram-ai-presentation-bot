"""FastAPI app factory and production entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from apps.api.routes import admin, health, payments, plans, telegram
from apps.bot.main import build_dispatcher, validate_runtime_credentials
from core.config import Settings, get_settings
from core.database.session import create_engine, create_session_factory
from core.logging import configure_logging


def create_app(
    *,
    settings: Settings | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    owned_engine: AsyncEngine | None = None
    if session_factory is None:
        owned_engine = create_engine(resolved_settings)
        session_factory = create_session_factory(owned_engine)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        configure_logging(debug=resolved_settings.debug)
        if resolved_settings.bot_enabled and resolved_settings.telegram_mode == "webhook":
            validate_runtime_credentials(resolved_settings)
            from aiogram import Bot

            bot = Bot(token=resolved_settings.telegram_bot_token.get_secret_value())
            dispatcher = build_dispatcher(
                settings=resolved_settings,
                session_factory=session_factory,
            )
            application.state.telegram_bot = bot
            application.state.telegram_dispatcher = dispatcher
            await bot.set_webhook(
                url=f"{resolved_settings.public_base_url.rstrip('/')}/api/v1/telegram/webhook",
                secret_token=resolved_settings.telegram_webhook_secret.get_secret_value(),
                allowed_updates=dispatcher.resolve_used_update_types(),
            )
        yield
        if resolved_settings.bot_enabled and resolved_settings.telegram_mode == "webhook":
            await application.state.telegram_dispatcher.storage.close()
            await application.state.telegram_bot.session.close()
        if owned_engine is not None:
            await owned_engine.dispose()

    app = FastAPI(
        title=resolved_settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if resolved_settings.app_env != "production" else None,
    )
    app.state.settings = resolved_settings
    app.state.session_factory = session_factory

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request.headers.get("x-request-id", str(uuid4()))[:64]
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            structlog.contextvars.clear_contextvars()

    app.include_router(health.router)
    app.include_router(plans.router)
    app.include_router(payments.router)
    if resolved_settings.app_env != "production" and resolved_settings.payment_provider == "mock":
        app.include_router(payments.mock_router)
    app.include_router(admin.router)
    app.include_router(telegram.router)
    return app


app = create_app()
