"""Shared bot handler helpers."""

from html import escape

from aiogram.types import Message
from core.config import Settings
from modules.users.models import User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.keyboards import main_menu_keyboard
from apps.bot.localization import Localizer


def main_menu_text(
    localizer: Localizer,
    *,
    language: str,
    first_name: str | None,
    presentations_count: int,
    free_demo_used: bool,
) -> str:
    name = escape(first_name or localizer.text(language, "menu.friend"))
    demo_key = "menu.demo_used" if free_demo_used else "menu.demo_available"
    return localizer.text(
        language,
        "menu.dashboard",
        name=name,
        count=presentations_count,
        demo_status=localizer.text(language, demo_key),
    )


async def current_user(session: AsyncSession, telegram_user_id: int) -> User:
    user = await session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
    if user is None:
        raise LookupError("Telegram user is not registered")
    return user


async def show_main_menu(
    message: Message,
    *,
    user: User,
    localizer: Localizer,
    settings: Settings,
) -> None:
    await message.answer(
        main_menu_text(
            localizer,
            language=user.selected_language,
            first_name=user.first_name,
            presentations_count=user.presentations_count,
            free_demo_used=user.free_demo_used,
        ),
        reply_markup=main_menu_keyboard(
            localizer,
            user.selected_language,
            is_admin=user.is_admin or user.telegram_user_id in settings.admin_id_set,
        ),
        parse_mode="HTML",
    )
