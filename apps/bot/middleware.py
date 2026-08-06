"""Bot middleware dependencies."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_PRIVATE_MODE_MESSAGE = "🔒 Bot hozir faqat loyiha egasi uchun private test rejimida."


class OwnerOnlyMiddleware(BaseMiddleware):
    """Reject non-owner Telegram updates before any database-backed handler runs."""

    def __init__(self, *, private_mode: bool, owner_telegram_id: int | None) -> None:
        self._private_mode = private_mode
        self._owner_telegram_id = owner_telegram_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not self._private_mode:
            return await handler(event, data)
        user = data.get("event_from_user")
        if user is not None and getattr(user, "id", None) == self._owner_telegram_id:
            return await handler(event, data)

        message = getattr(event, "message", None)
        if message is not None:
            await message.answer(_PRIVATE_MODE_MESSAGE)
        else:
            callback = getattr(event, "callback_query", None)
            if callback is not None:
                await callback.answer(_PRIVATE_MODE_MESSAGE, show_alert=True)
        return None


class DatabaseSessionMiddleware(BaseMiddleware):
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self._factory() as session:
            data["session"] = session
            data["session_factory"] = self._factory
            return await handler(event, data)
