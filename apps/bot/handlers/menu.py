"""Main menu, profile, plan and admin handlers."""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.handlers.common import current_user, show_main_menu
from apps.bot.keyboards import language_keyboard
from apps.bot.localization import Localizer
from core.config import Settings
from modules.orders.domain import OrderStatus
from modules.orders.models import Order
from modules.plans.models import Plan
from modules.presentations.models import Presentation
from modules.users.models import User

router = Router(name="menu")
_localizer = Localizer.load_default()


def labels(key: str) -> set[str]:
    return {_localizer.text(language, key) for language in _localizer.languages}


@router.message(F.text.in_(labels("common.main_menu")))
async def main_menu(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    localizer: Localizer,
    settings: Settings,
) -> None:
    if message.from_user is None:
        return
    await state.clear()
    user = await current_user(session, message.from_user.id)
    await show_main_menu(message, user=user, localizer=localizer, settings=settings)


@router.message(F.text.in_(labels("menu.plans")))
async def plans(message: Message, session: AsyncSession, localizer: Localizer) -> None:
    if message.from_user is None:
        return
    user = await current_user(session, message.from_user.id)
    items = (
        await session.scalars(select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.price_uzs))
    ).all()
    body = "\n".join(
        f"• {plan.name}: {plan.price_uzs:,} UZS · {plan.minimum_slides}–{plan.maximum_slides}"
        for plan in items
    )
    await message.answer(f"{localizer.text(user.selected_language, 'plans.title')}\n\n{body}")


@router.message(F.text.in_(labels("menu.profile")))
async def profile(message: Message, session: AsyncSession, localizer: Localizer) -> None:
    if message.from_user is None:
        return
    user = await current_user(session, message.from_user.id)
    await message.answer(
        localizer.text(
            user.selected_language,
            "profile.summary",
            telegram_id=user.telegram_user_id,
            selected_language=user.selected_language,
            count=user.presentations_count,
            demo=user.free_demo_used,
        )
    )


@router.message(F.text.in_(labels("menu.settings")))
async def settings_menu(message: Message, session: AsyncSession, localizer: Localizer) -> None:
    if message.from_user is None:
        return
    user = await current_user(session, message.from_user.id)
    await message.answer(
        localizer.text(user.selected_language, "start.choose_language"),
        reply_markup=language_keyboard(),
    )


@router.message(F.text.in_(labels("menu.help")))
async def help_menu(message: Message, session: AsyncSession, localizer: Localizer) -> None:
    if message.from_user is None:
        return
    user = await current_user(session, message.from_user.id)
    await message.answer(localizer.text(user.selected_language, "help.text"))


@router.message(F.text.in_(labels("menu.mine")))
async def my_presentations(
    message: Message,
    session: AsyncSession,
    localizer: Localizer,
) -> None:
    if message.from_user is None:
        return
    user = await current_user(session, message.from_user.id)
    presentations = (
        await session.scalars(
            select(Presentation)
            .where(Presentation.user_id == user.id)
            .order_by(Presentation.created_at.desc())
            .limit(10)
        )
    ).all()
    body = "\n".join(f"• {item.title} — {item.status}" for item in presentations) or "—"
    await message.answer(f"{localizer.text(user.selected_language, 'menu.mine')}\n\n{body}")


@router.message(F.text.in_(labels("menu.free_demo")))
async def free_demo_status(message: Message, session: AsyncSession, localizer: Localizer) -> None:
    if message.from_user is None:
        return
    user = await current_user(session, message.from_user.id)
    status_text = "✅" if not user.free_demo_used else "❌"
    await message.answer(
        f"{localizer.text(user.selected_language, 'menu.free_demo')}: {status_text}"
    )


@router.message(F.text.in_(labels("menu.admin")))
async def admin_panel(
    message: Message,
    session: AsyncSession,
    localizer: Localizer,
    settings: Settings,
) -> None:
    if message.from_user is None:
        return
    user = await current_user(session, message.from_user.id)
    if not (user.is_admin or user.telegram_user_id in settings.admin_id_set):
        return
    users = int(await session.scalar(select(func.count(User.id))) or 0)
    orders = int(await session.scalar(select(func.count(Order.id))) or 0)
    revenue = int(
        await session.scalar(
            select(func.coalesce(func.sum(Order.amount), 0)).where(
                Order.status.in_([OrderStatus.PAID, OrderStatus.COMPLETED])
            )
        )
        or 0
    )
    await message.answer(
        f"{localizer.text(user.selected_language, 'menu.admin')}\n"
        f"Users: {users}\nOrders: {orders}\nRevenue: {revenue:,} UZS"
    )
