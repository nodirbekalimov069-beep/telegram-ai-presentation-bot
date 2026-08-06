"""Start, onboarding and language selection handlers."""

from aiogram import F, Router
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from core.config import Settings
from modules.users.service import UserService
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.handlers.common import current_user, show_main_menu
from apps.bot.keyboards import language_keyboard
from apps.bot.localization import Localizer

router = Router(name="start")


@router.message(CommandStart())
async def start(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    localizer: Localizer,
) -> None:
    if message.from_user is None:
        return
    await UserService().register(
        session,
        telegram_user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        language_code=message.from_user.language_code,
    )
    await state.clear()
    await message.answer(
        localizer.text("uz", "start.choose_language"),
        reply_markup=language_keyboard(),
    )


@router.callback_query(StateFilter(None), F.data.startswith("lang:"))
async def select_language(
    callback: CallbackQuery,
    session: AsyncSession,
    localizer: Localizer,
    settings: Settings,
) -> None:
    if callback.from_user is None or callback.data is None:
        return
    language = callback.data.split(":", 1)[1]
    if language not in localizer.languages:
        await callback.answer()
        return
    async with session.begin():
        user = await current_user(session, callback.from_user.id)
        user.selected_language = language
    if isinstance(callback.message, Message):
        await show_main_menu(
            callback.message,
            user=user,
            localizer=localizer,
            settings=settings,
        )
    await callback.answer()
