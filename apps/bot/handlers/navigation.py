"""Top-level feature navigation that must run before state-specific routers."""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.handlers.presentation import new_presentation
from apps.bot.handlers.services import service_catalog
from apps.bot.localization import Localizer
from apps.bot.menu_labels import menu_labels

router = Router(name="feature-navigation")
_localizer = Localizer.load_default()


def labels(key: str) -> set[str]:
    return menu_labels(_localizer, key)


@router.message(F.text.in_(labels("menu.new")))
async def presentation_entry(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    localizer: Localizer,
) -> None:
    await new_presentation(message, state, session, localizer)


@router.message(F.text.in_(labels("menu.ai_services")))
async def services_entry(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    localizer: Localizer,
) -> None:
    await service_catalog(message, state, session, localizer)
