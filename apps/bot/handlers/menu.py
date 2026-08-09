"""Main menu, pricing, orders, help, settings and admin handlers."""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.handlers.common import current_user, show_main_menu
from apps.bot.keyboards import language_keyboard
from apps.bot.localization import Localizer
from apps.bot.menu_labels import menu_labels
from core.config import Settings
from modules.orders.domain import OrderStatus
from modules.orders.models import Order
from modules.plans.models import Plan
from modules.service_orders.catalog import PRICING_CATEGORIES, SERVICE_CATALOG, ServiceType
from modules.service_orders.dashboard import friendly_order_status
from modules.service_orders.models import ServiceOrder
from modules.users.models import FreeDemoClaim, User
from modules.users.service import MAX_FREE_DEMO_ATTEMPTS

router = Router(name="menu")
_localizer = Localizer.load_default()
_ORDERS_PER_PAGE = 5


def labels(key: str) -> set[str]:
    return menu_labels(_localizer, key)


def _home_keyboard(localizer: Localizer, language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=localizer.text(language, "common.main_menu"), callback_data="nav:home"
                )
            ]
        ]
    )


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


@router.callback_query(F.data == "nav:home")
async def callback_main_menu(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    localizer: Localizer,
    settings: Settings,
) -> None:
    await callback.answer()
    await state.clear()
    if isinstance(callback.message, Message):
        user = await current_user(session, callback.from_user.id)
        await show_main_menu(callback.message, user=user, localizer=localizer, settings=settings)


@router.message(F.text.in_(labels("menu.plans")))
async def pricing_menu(
    message: Message, state: FSMContext, session: AsyncSession, localizer: Localizer
) -> None:
    if message.from_user is None:
        return
    await state.clear()
    user = await current_user(session, message.from_user.id)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=localizer.text(user.selected_language, item.label_key),
                    callback_data=f"price:{item.code}",
                )
            ]
            for item in PRICING_CATEGORIES
        ]
        + _home_keyboard(localizer, user.selected_language).inline_keyboard
    )
    await message.answer(
        localizer.text(user.selected_language, "pricing.title"), reply_markup=keyboard
    )


@router.callback_query(F.data.startswith("price:"))
async def pricing_category(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    localizer: Localizer,
) -> None:
    await callback.answer()
    await state.clear()
    if callback.data is None or not isinstance(callback.message, Message):
        return
    user = await current_user(session, callback.from_user.id)
    code = callback.data.split(":", 1)[1]
    category = next((item for item in PRICING_CATEGORIES if item.code == code), None)
    if category is None:
        return
    title = localizer.text(user.selected_language, category.label_key)
    if category.price_source == "database_plans":
        plans = (
            await session.scalars(
                select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.price_uzs)
            )
        ).all()
        body = "\n".join(
            f"• {plan.name}: {plan.price_uzs:,} UZS · {plan.minimum_slides}–{plan.maximum_slides}"
            for plan in plans
        ) or localizer.text(user.selected_language, "pricing.not_configured")
    else:
        body = localizer.text(user.selected_language, "pricing.not_configured")
    await callback.message.answer(
        f"{title}\n\n{body}", reply_markup=_home_keyboard(localizer, user.selected_language)
    )


@router.message(F.text.in_(labels("menu.profile")))
async def profile(
    message: Message, state: FSMContext, session: AsyncSession, localizer: Localizer
) -> None:
    """Backward-compatible profile entry for old reply keyboards."""
    if message.from_user is None:
        return
    await state.clear()
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
async def settings_menu(
    message: Message, state: FSMContext, session: AsyncSession, localizer: Localizer
) -> None:
    if message.from_user is None:
        return
    await state.clear()
    user = await current_user(session, message.from_user.id)
    await message.answer(
        localizer.text(user.selected_language, "settings.title"),
        reply_markup=_settings_keyboard(localizer, user),
    )


def _settings_keyboard(localizer: Localizer, user: User) -> InlineKeyboardMarkup:
    state_key = (
        "settings.notifications_on" if user.notifications_enabled else "settings.notifications_off"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=localizer.text(user.selected_language, "settings.language"),
                    callback_data="settings:language",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"{localizer.text(user.selected_language, 'settings.notifications')} · "
                    f"{localizer.text(user.selected_language, state_key)}",
                    callback_data="settings:notifications",
                )
            ],
            [
                InlineKeyboardButton(
                    text=localizer.text(user.selected_language, "common.main_menu"),
                    callback_data="nav:home",
                )
            ],
        ]
    )


@router.callback_query(F.data == "settings:language")
async def settings_language(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    localizer: Localizer,
) -> None:
    await callback.answer()
    await state.clear()
    if isinstance(callback.message, Message):
        user = await current_user(session, callback.from_user.id)
        await callback.message.answer(
            localizer.text(user.selected_language, "start.choose_language"),
            reply_markup=language_keyboard(localizer, user.selected_language),
        )


@router.callback_query(F.data == "settings:notifications")
async def settings_notifications(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    localizer: Localizer,
) -> None:
    await callback.answer()
    await state.clear()
    async with session.begin():
        user = await session.scalar(
            select(User).where(User.telegram_user_id == callback.from_user.id).with_for_update()
        )
        if user is None:
            return
        user.notifications_enabled = not user.notifications_enabled
    if isinstance(callback.message, Message):
        await callback.message.answer(
            localizer.text(user.selected_language, "settings.updated"),
            reply_markup=_settings_keyboard(localizer, user),
        )


@router.message(F.text.in_(labels("menu.help")))
async def help_menu(
    message: Message, state: FSMContext, session: AsyncSession, localizer: Localizer
) -> None:
    if message.from_user is None:
        return
    await state.clear()
    user = await current_user(session, message.from_user.id)
    items = (
        ("how_bot", "help.how_bot"),
        ("presentation", "help.presentation"),
        ("ai", "help.ai"),
        ("payment", "help.payment"),
        ("support", "help.support"),
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=localizer.text(user.selected_language, key),
                    callback_data=f"help:{code}",
                )
            ]
            for code, key in items
        ]
        + _home_keyboard(localizer, user.selected_language).inline_keyboard
    )
    await message.answer(
        localizer.text(user.selected_language, "help.title"), reply_markup=keyboard
    )


@router.callback_query(F.data.startswith("help:"))
async def help_detail(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    localizer: Localizer,
) -> None:
    await callback.answer()
    await state.clear()
    if callback.data is None or not isinstance(callback.message, Message):
        return
    user = await current_user(session, callback.from_user.id)
    code = callback.data.split(":", 1)[1]
    key = {
        "how_bot": "help.how_bot_text",
        "presentation": "help.presentation_text",
        "ai": "help.ai_text",
        "payment": "help.payment_text",
        "support": "help.support_text",
    }.get(code)
    if key is None:
        return
    await callback.message.answer(
        localizer.text(user.selected_language, key),
        reply_markup=_home_keyboard(localizer, user.selected_language),
    )


@router.message(F.text.in_(labels("menu.mine")))
async def my_orders(
    message: Message, state: FSMContext, session: AsyncSession, localizer: Localizer
) -> None:
    if message.from_user is None:
        return
    await state.clear()
    user = await current_user(session, message.from_user.id)
    await _send_orders(message, session, user, localizer, page=0)


@router.callback_query(F.data.startswith("orders:"))
async def orders_page(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    localizer: Localizer,
) -> None:
    await callback.answer()
    await state.clear()
    if callback.data is None or not isinstance(callback.message, Message):
        return
    try:
        page = int(callback.data.split(":", 1)[1])
    except ValueError:
        return
    user = await current_user(session, callback.from_user.id)
    await _send_orders(callback.message, session, user, localizer, page=page)


async def _send_orders(
    message: Message,
    session: AsyncSession,
    user: User,
    localizer: Localizer,
    *,
    page: int,
) -> None:
    presentation_orders = select(
        Order.created_at.label("created_at"),
        literal("presentation").label("service_type"),
        Order.topic.label("title"),
        Order.status.label("status"),
    ).where(Order.user_id == user.id)
    service_orders = select(
        ServiceOrder.created_at.label("created_at"),
        ServiceOrder.service_type.label("service_type"),
        ServiceOrder.title.label("title"),
        ServiceOrder.status.label("status"),
    ).where(ServiceOrder.user_id == user.id)
    combined = union_all(presentation_orders, service_orders).subquery()
    total_items = int(await session.scalar(select(func.count()).select_from(combined)) or 0)
    total_pages = max(1, (total_items + _ORDERS_PER_PAGE - 1) // _ORDERS_PER_PAGE)
    bounded_page = min(max(page, 0), total_pages - 1)
    rows = (
        await session.execute(
            select(combined)
            .order_by(combined.c.created_at.desc())
            .offset(bounded_page * _ORDERS_PER_PAGE)
            .limit(_ORDERS_PER_PAGE)
        )
    ).all()
    if total_items == 0:
        body = localizer.text(user.selected_language, "orders.empty")
    else:
        lines = []
        for row in rows:
            service_type = str(row.service_type)
            if service_type == "presentation":
                label = localizer.text(user.selected_language, "pricing.presentation")
            else:
                definition = SERVICE_CATALOG.get(ServiceType(service_type))
                label = (
                    localizer.text(user.selected_language, definition.label_key)
                    if definition is not None
                    else service_type
                )
            lines.append(
                f"• {label} · {str(row.title)[:80]}\n"
                f"  {friendly_order_status(str(row.status), user.selected_language)}"
            )
        body = "\n".join(lines)
    nav: list[InlineKeyboardButton] = []
    if bounded_page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"orders:{bounded_page - 1}"))
    if bounded_page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"orders:{bounded_page + 1}"))
    keyboard_rows = [nav] if nav else []
    keyboard_rows.extend(_home_keyboard(localizer, user.selected_language).inline_keyboard)
    await message.answer(
        f"{localizer.text(user.selected_language, 'orders.title')}\n\n{body}\n\n"
        f"{localizer.text(user.selected_language, 'orders.page', page=bounded_page + 1, total=total_pages)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
    )


def _free_demo_status_key(user: User, claim: FreeDemoClaim | None) -> str:
    if user.free_demo_used or (claim is not None and claim.status == "CONSUMED"):
        return "demo.used"
    if claim is not None and claim.status == "RESERVED":
        return "demo.reserved"
    if claim is not None and claim.attempt_count >= MAX_FREE_DEMO_ATTEMPTS:
        return "demo.used"
    return "demo.available"


@router.message(F.text.in_(labels("menu.free_demo")))
async def free_demo_status(
    message: Message, state: FSMContext, session: AsyncSession, localizer: Localizer
) -> None:
    if message.from_user is None:
        return
    await state.clear()
    user = await current_user(session, message.from_user.id)
    claim = await session.scalar(
        select(FreeDemoClaim).where(FreeDemoClaim.telegram_user_id == user.telegram_user_id)
    )
    key = _free_demo_status_key(user, claim)
    await message.answer(
        localizer.text(user.selected_language, key),
        reply_markup=_home_keyboard(localizer, user.selected_language),
    )


@router.message(F.text.in_(labels("menu.admin")))
async def admin_panel(
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
    if not (user.is_admin or user.telegram_user_id in settings.admin_id_set):
        return
    users = int(await session.scalar(select(func.count(User.id))) or 0)
    orders = int(await session.scalar(select(func.count(Order.id))) or 0)
    service_orders = int(await session.scalar(select(func.count(ServiceOrder.id))) or 0)
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
        f"Users: {users}\nOrders: {orders + service_orders}\nRevenue: {revenue:,} UZS"
    )
