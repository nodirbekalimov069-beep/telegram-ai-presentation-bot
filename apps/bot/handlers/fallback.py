"""Last-resort Telegram callback response; this router must be registered last."""

from aiogram import Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.handlers.common import find_user, preferred_language
from apps.bot.localization import Localizer

router = Router(name="fallback")


@router.callback_query()
async def unknown_callback(
    callback: CallbackQuery,
    session: AsyncSession,
    localizer: Localizer,
) -> None:
    user = await find_user(session, callback.from_user.id)
    language = preferred_language(user, callback.from_user.language_code, localizer)
    await callback.answer(localizer.text(language, "service.expired"))
