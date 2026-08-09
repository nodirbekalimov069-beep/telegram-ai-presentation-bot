"""Main menu, pricing, orders, help, settings and admin handlers."""

import tempfile
from datetime import UTC, datetime
from pathlib import Path

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import and_, func, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.handlers.common import current_user, show_main_menu
from apps.bot.keyboards import language_keyboard
from apps.bot.localization import Localizer
from apps.bot.menu_labels import menu_labels
from core.config import Settings
from modules.files.factory import build_storage
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


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_orders_callback(data: str) -> tuple[int, tuple[datetime, int, int] | None] | None:
    parts = data.split(":")
    if len(parts) not in {2, 5} or parts[0] != "orders":
        return None
    if not parts[1].isdigit() or len(parts[1]) > 6:
        return None
    page = int(parts[1])
    if len(parts) == 2:
        return page, None
    if any(not value.isdigit() for value in parts[2:]):
        return None
    if len(parts[2]) > 18 or len(parts[3]) > 1 or len(parts[4]) > 19:
        return None
    micros, source_rank, item_id = map(int, parts[2:])
    if micros > 253_402_300_799_999_999 or source_rank not in {0, 1}:
        return None
    if item_id < 1 or item_id > 9_223_372_036_854_775_807:
        return None
    try:
        timestamp = datetime.fromtimestamp(micros / 1_000_000, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None
    return page, (timestamp, source_rank, item_id)


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
    parsed = _parse_orders_callback(callback.data)
    if parsed is None:
        return
    page, watermark = parsed
    user = await current_user(session, callback.from_user.id)
    await _send_orders(callback.message, session, user, localizer, page=page, watermark=watermark)


async def _send_orders(
    message: Message,
    session: AsyncSession,
    user: User,
    localizer: Localizer,
    *,
    page: int,
    watermark: tuple[datetime, int, int] | None = None,
) -> None:
    presentation_orders = select(
        Order.id.label("item_id"),
        Order.created_at.label("created_at"),
        literal("presentation").label("service_type"),
        Order.topic.label("title"),
        Order.status.label("status"),
        literal(False).label("artifact_ready"),
        literal(0).label("source_rank"),
    ).where(Order.user_id == user.id)
    service_orders = select(
        ServiceOrder.id.label("item_id"),
        ServiceOrder.created_at.label("created_at"),
        ServiceOrder.service_type.label("service_type"),
        ServiceOrder.title.label("title"),
        ServiceOrder.status.label("status"),
        ServiceOrder.docx_storage_key.is_not(None).label("artifact_ready"),
        literal(1).label("source_rank"),
    ).where(ServiceOrder.user_id == user.id)
    combined = union_all(presentation_orders, service_orders).subquery()
    ordering = (
        combined.c.created_at.desc(),
        combined.c.source_rank.asc(),
        combined.c.item_id.desc(),
    )
    if watermark is None:
        first = (await session.execute(select(combined).order_by(*ordering).limit(1))).first()
        if first is not None:
            watermark = (
                _as_utc(first.created_at),
                int(first.source_rank),
                int(first.item_id),
            )
    if watermark is not None:
        watermark_time, watermark_rank, watermark_id = watermark
        snapshot_filter = or_(
            combined.c.created_at < watermark_time,
            and_(
                combined.c.created_at == watermark_time,
                or_(
                    combined.c.source_rank > watermark_rank,
                    and_(
                        combined.c.source_rank == watermark_rank,
                        combined.c.item_id <= watermark_id,
                    ),
                ),
            ),
        )
        combined_query = select(combined).where(snapshot_filter)
        count_query = select(func.count()).select_from(combined).where(snapshot_filter)
    else:
        combined_query = select(combined)
        count_query = select(func.count()).select_from(combined)
    total_items = int(await session.scalar(count_query) or 0)
    total_pages = max(1, (total_items + _ORDERS_PER_PAGE - 1) // _ORDERS_PER_PAGE)
    bounded_page = min(max(page, 0), total_pages - 1)
    rows = (
        await session.execute(
            combined_query.order_by(*ordering)
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
                try:
                    definition = SERVICE_CATALOG.get(ServiceType(service_type))
                except ValueError:
                    definition = None
                label = (
                    localizer.text(user.selected_language, definition.label_key)
                    if definition is not None
                    else localizer.text(user.selected_language, "service.unknown")
                )
            lines.append(
                f"• {label} · {str(row.title)[:80]}\n"
                f"  {row.created_at:%Y-%m-%d} · "
                f"{friendly_order_status(str(row.status), user.selected_language)}"
            )
        body = "\n".join(lines)
    nav: list[InlineKeyboardButton] = []
    watermark_suffix = ""
    if watermark is not None:
        watermark_suffix = (
            f":{int(_as_utc(watermark[0]).timestamp() * 1_000_000)}:{watermark[1]}:{watermark[2]}"
        )
    if bounded_page > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️", callback_data=f"orders:{bounded_page - 1}{watermark_suffix}"
            )
        )
    if bounded_page + 1 < total_pages:
        nav.append(
            InlineKeyboardButton(
                text="➡️", callback_data=f"orders:{bounded_page + 1}{watermark_suffix}"
            )
        )
    keyboard_rows = [nav] if nav else []
    for row in rows:
        if str(row.service_type) == ServiceType.ESSAY.value and bool(row.artifact_ready):
            keyboard_rows.append(
                [
                    InlineKeyboardButton(
                        text=(
                            f"📎 {localizer.text(user.selected_language, 'service.essay')} "
                            f"#{row.item_id}"
                        ),
                        callback_data=f"sva:{row.item_id}",
                    )
                ]
            )
    keyboard_rows.extend(_home_keyboard(localizer, user.selected_language).inline_keyboard)
    await message.answer(
        f"{localizer.text(user.selected_language, 'orders.title')}\n\n{body}\n\n"
        f"{localizer.text(user.selected_language, 'orders.page', page=bounded_page + 1, total=total_pages)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
    )


@router.callback_query(F.data.startswith("sva:"))
async def retrieve_service_artifacts(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    localizer: Localizer,
    settings: Settings,
) -> None:
    del state
    if callback.data is None or not isinstance(callback.message, Message):
        await callback.answer()
        return
    raw_id = callback.data.split(":", 1)[1]
    if not raw_id.isdigit() or len(raw_id) > 19:
        await callback.answer()
        return
    order_id = int(raw_id)
    if order_id < 1 or order_id > 9_223_372_036_854_775_807:
        await callback.answer()
        return
    user = await current_user(session, callback.from_user.id)
    order = await session.scalar(
        select(ServiceOrder).where(
            ServiceOrder.id == order_id,
            ServiceOrder.user_id == user.id,
            ServiceOrder.service_type == ServiceType.ESSAY.value,
        )
    )
    if order is None or order.docx_storage_key is None:
        await callback.answer(
            localizer.text(user.selected_language, "orders.artifact_unavailable"),
            show_alert=True,
        )
        return
    await callback.answer()
    storage = build_storage(settings)
    root = settings.generation_work_root.expanduser().resolve() / "service-order-retrieval"
    root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix=f"retrieve-{order.id}-", dir=root) as directory:
            workdir = Path(directory)
            sent_count = 0
            oversized_count = 0
            for artifact_type, key in (
                ("docx", order.docx_storage_key),
                ("pdf", order.pdf_storage_key),
            ):
                if key is None:
                    continue
                path = await storage.download_for_user(
                    key,
                    workdir / f"referat_{order.id}.{artifact_type}",
                    user_id=user.id,
                )
                if path.stat().st_size > settings.max_upload_bytes:
                    oversized_count += 1
                    continue
                await callback.message.answer_document(
                    FSInputFile(path, filename=f"referat_{order.id}.{artifact_type}"),
                    caption=localizer.text(
                        user.selected_language,
                        "orders.artifact_caption",
                        artifact=artifact_type.upper(),
                    ),
                )
                sent_count += 1
            if oversized_count:
                await callback.message.answer(
                    localizer.text(user.selected_language, "orders.artifact_too_large")
                )
            elif sent_count == 0:
                await callback.message.answer(
                    localizer.text(user.selected_language, "orders.artifact_unavailable")
                )
    except Exception:
        await callback.message.answer(
            localizer.text(user.selected_language, "orders.artifact_unavailable")
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
