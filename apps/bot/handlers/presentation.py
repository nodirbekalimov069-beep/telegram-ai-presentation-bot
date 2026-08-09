"""End-to-end presentation creation FSM handlers."""

import hashlib
import hmac
import json
import logging
from typing import Any
from uuid import uuid4

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from core.config import Settings
from modules.ai.factory import build_ai_provider, close_ai_provider
from modules.orders.models import Order
from modules.orders.service import OrderService
from modules.payments.factory import build_payment_provider
from modules.payments.providers.mock import MockPaymentProvider
from modules.payments.service import PaymentService
from modules.plans.domain import PlanLimitError
from modules.presentations.models import Presentation
from modules.presentations.service import PresentationService
from modules.users.models import User
from modules.users.service import FreeDemoService, FreeDemoUnavailable
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.bot.fsm import PresentationFlow
from apps.bot.handlers.common import current_user, show_main_menu
from apps.bot.keyboards import (
    choices_keyboard,
    generation_cancel_action,
    language_keyboard,
    outline_actions,
    resolve_choice,
)
from apps.bot.localization import Localizer
from apps.bot.options import (
    DESIGN_STYLES,
    KNOWLEDGE_LEVELS,
    PLAN_CODES,
    directions_for,
)
from apps.bot.quick_presentation import QuickPresentationRequestError, QuickPresentationService

router = Router(name="presentation-flow")
_localizer = Localizer.load_default()
logger = logging.getLogger(__name__)
_CANCELLABLE_INPUT_STATES = frozenset(
    {
        PresentationFlow.language.state,
        PresentationFlow.direction.state,
        PresentationFlow.topic.state,
        PresentationFlow.purpose.state,
        PresentationFlow.audience.state,
        PresentationFlow.knowledge_level.state,
        PresentationFlow.slide_count.state,
        PresentationFlow.design_style.state,
        PresentationFlow.plan.state,
    }
)


def labels(key: str) -> set[str]:
    return {_localizer.text(language, key) for language in _localizer.languages}


@router.message(F.text.in_(labels("common.cancel")))
async def cancel_flow(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    localizer: Localizer,
    settings: Settings,
) -> None:
    if message.from_user is None:
        return
    data = await state.get_data()
    current_state = await state.get_state()
    if current_state in {
        PresentationFlow.outline_review.state,
        PresentationFlow.outline_edit.state,
    } or (current_state is None and "presentation_id" in data):
        try:
            presentation_id = int(data["presentation_id"])
            await PresentationService().cancel_outline(
                session,
                presentation_id=presentation_id,
            )
        except ValueError as exc:
            language = await _language(session, message.from_user.id)
            if str(exc) == "outline is no longer cancellable":
                await message.answer(localizer.text(language, "error.outline_busy"))
                return
            logger.exception(
                "outline cancellation cleanup failed telegram_user_id=%s",
                message.from_user.id,
            )
            request_id = uuid4().hex[:12]
            await message.answer(localizer.text(language, "error.generic", request_id=request_id))
            return
        except (KeyError, TypeError, LookupError):
            logger.exception(
                "outline cancellation cleanup failed telegram_user_id=%s",
                message.from_user.id,
            )
            language = await _language(session, message.from_user.id)
            request_id = uuid4().hex[:12]
            await message.answer(localizer.text(language, "error.generic", request_id=request_id))
            return
    elif current_state == PresentationFlow.waiting_payment.state or "order_id" in data:
        try:
            await OrderService().cancel_waiting_payment(
                session,
                order_id=int(data["order_id"]),
                telegram_user_id=message.from_user.id,
            )
        except (KeyError, TypeError, ValueError, LookupError):
            logger.exception(
                "payment cancellation failed telegram_user_id=%s",
                message.from_user.id,
            )
            language = str(data.get("language") or "uz")
            await message.answer(localizer.text(language, "error.cancel_busy"))
            return
    elif current_state not in _CANCELLABLE_INPUT_STATES:
        language = await _language(session, message.from_user.id)
        await state.clear()
        await message.answer(localizer.text(language, "common.no_active_flow"))
        user = await current_user(session, message.from_user.id)
        await show_main_menu(message, user=user, localizer=localizer, settings=settings)
        return
    language = await _language(session, message.from_user.id)
    await state.clear()
    await message.answer(localizer.text(language, "create.cancelled"))
    user = await current_user(session, message.from_user.id)
    await show_main_menu(message, user=user, localizer=localizer, settings=settings)


@router.message(F.text.in_(labels("common.back")))
async def back_flow(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    localizer: Localizer,
    settings: Settings,
) -> None:
    if message.from_user is None:
        return
    data = await state.get_data()
    current = await state.get_state()
    if current == PresentationFlow.waiting_payment.state:
        try:
            await OrderService().cancel_waiting_payment(
                session,
                order_id=int(data["order_id"]),
                telegram_user_id=message.from_user.id,
            )
        except (KeyError, TypeError, ValueError, LookupError):
            logger.exception(
                "payment cancellation failed while navigating back telegram_user_id=%s",
                message.from_user.id,
            )
            language = str(data.get("language") or await _language(session, message.from_user.id))
            await message.answer(localizer.text(language, "error.cancel_busy"))
            return
        language = str(data.get("language") or await _language(session, message.from_user.id))
        await state.set_data({key: value for key, value in data.items() if key != "order_id"})
        await state.set_state(PresentationFlow.plan)
        await message.answer(
            localizer.text(language, "create.plan"),
            reply_markup=choices_keyboard(PLAN_CODES, localizer, language),
        )
        return
    language = str(data.get("language") or await _language(session, message.from_user.id))
    if current is None and "presentation_id" in data:
        user = await current_user(session, message.from_user.id)
        await show_main_menu(message, user=user, localizer=localizer, settings=settings)
        return
    if current == PresentationFlow.direction.state:
        await state.set_state(PresentationFlow.language)
        await message.answer(
            localizer.text(language, "start.choose_language"),
            reply_markup=language_keyboard(),
        )
        return
    back_steps = {
        PresentationFlow.topic.state: (
            PresentationFlow.direction,
            "create.direction",
            directions_for(language),
        ),
        PresentationFlow.purpose.state: (PresentationFlow.topic, "create.topic", ()),
        PresentationFlow.audience.state: (PresentationFlow.purpose, "create.purpose", ()),
        PresentationFlow.knowledge_level.state: (PresentationFlow.audience, "create.audience", ()),
        PresentationFlow.slide_count.state: (
            PresentationFlow.knowledge_level,
            "create.knowledge",
            KNOWLEDGE_LEVELS,
        ),
        PresentationFlow.design_style.state: (
            PresentationFlow.slide_count,
            "create.slides",
            ("8", "10", "12", "15", "18", "20", "25", "30"),
        ),
        PresentationFlow.plan.state: (
            PresentationFlow.design_style,
            "create.design",
            DESIGN_STYLES,
        ),
    }
    target = back_steps.get(current)
    if target is None:
        await state.clear()
        user = await current_user(session, message.from_user.id)
        await show_main_menu(message, user=user, localizer=localizer, settings=settings)
        return
    previous, prompt_key, options = target
    await state.set_state(previous)
    keyboard = choices_keyboard(options, localizer, language) if options else None
    await message.answer(localizer.text(language, prompt_key), reply_markup=keyboard)


async def _language(session: AsyncSession, telegram_user_id: int) -> str:
    return (await current_user(session, telegram_user_id)).selected_language


async def _authorized_presentation_context(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    action: str,
) -> tuple[int, int, str] | None:
    callback_data = callback.data or ""
    parts = callback_data.split(":")
    state_data = await state.get_data()
    try:
        presentation_id = int(parts[2]) if len(parts) == 3 else int(state_data["presentation_id"])
    except (KeyError, TypeError, ValueError):
        return None
    async with session.begin():
        row = await session.execute(
            select(Presentation.order_id, Presentation.language)
            .join(User, User.id == Presentation.user_id)
            .where(
                Presentation.id == presentation_id,
                User.telegram_user_id == callback.from_user.id,
            )
        )
        context = row.one_or_none()
    if context is None:
        return None
    order_id, language = context
    await state.update_data(
        presentation_id=presentation_id,
        order_id=int(order_id),
        language=str(language),
        outline_action=action,
    )
    return presentation_id, int(order_id), str(language)


async def _send_outline(
    message: Message,
    presentation: Presentation,
    localizer: Localizer,
    language: str,
    *,
    edit: bool = False,
) -> None:
    slides = presentation.outline.get("slides", [])
    lines = [f"{item['number']}. {item['title']}" for item in slides]
    body = "\n".join(lines)
    if len(body) > 3400:
        body = f"{body[:3390]}…"
    text = f"{localizer.text(language, 'create.outline')}\n\n{body}"
    reply_markup = outline_actions(localizer, language, presentation.id)
    if edit:
        await message.edit_text(text, reply_markup=reply_markup)
    else:
        await message.answer(text, reply_markup=reply_markup)


async def _generate_outline_safely(
    *,
    responder: Any,
    session: AsyncSession,
    settings: Settings,
    localizer: Localizer,
    language: str,
    order_id: int,
    telegram_user_id: int,
) -> Presentation | None:
    correlation_id = uuid4().hex
    provider: Any = None
    try:
        provider = build_ai_provider(settings)
        return await PresentationService(ai_provider=provider).generate_outline(
            session, order_id=order_id
        )
    except Exception:
        await PresentationService().fail_outline_before_start(
            session,
            order_id=order_id,
            reason="AI provider initialization or outline generation failed",
        )
        logger.exception(
            "AI outline generation failed order_id=%s telegram_user_id=%s correlation_id=%s",
            order_id,
            telegram_user_id,
            correlation_id,
        )
        error_text = localizer.text(language, "error.ai_temporary")
        if hasattr(responder, "edit_text"):
            await responder.edit_text(error_text)
        else:
            await responder.answer(error_text)
        return None
    finally:
        if provider is not None:
            await close_ai_provider(provider)


@router.message(F.text.in_(labels("menu.new")))
async def new_presentation(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    localizer: Localizer,
) -> None:
    if message.from_user is None:
        return
    language = await _language(session, message.from_user.id)
    await state.clear()
    await state.set_state(PresentationFlow.language)
    await message.answer(
        localizer.text(language, "start.choose_language"),
        reply_markup=language_keyboard(),
    )


@router.callback_query(PresentationFlow.language, F.data.startswith("lang:"))
async def flow_language(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    localizer: Localizer,
) -> None:
    if callback.data is None or not isinstance(callback.message, Message):
        return
    language = callback.data.split(":", 1)[1]
    await state.update_data(language=language)
    await state.set_state(PresentationFlow.direction)
    await callback.message.answer(
        localizer.text(language, "create.direction"),
        reply_markup=choices_keyboard(directions_for(language), localizer, language),
    )
    await callback.answer()


@router.message(PresentationFlow.direction)
async def direction(message: Message, state: FSMContext, localizer: Localizer) -> None:
    data = await state.get_data()
    language = str(data["language"])
    await state.update_data(direction=message.text or "")
    await state.set_state(PresentationFlow.topic)
    await message.answer(localizer.text(language, "create.topic"))


@router.message(PresentationFlow.topic)
async def topic(message: Message, state: FSMContext, localizer: Localizer) -> None:
    data = await state.get_data()
    language = str(data["language"])
    value = (message.text or "").strip()
    if not 2 <= len(value) <= 500:
        await message.answer(localizer.text(language, "error.topic_length"))
        return
    await state.update_data(topic=value)
    await state.set_state(PresentationFlow.purpose)
    await message.answer(localizer.text(language, "create.purpose"))


@router.message(PresentationFlow.purpose)
async def purpose(message: Message, state: FSMContext, localizer: Localizer) -> None:
    data = await state.get_data()
    language = str(data["language"])
    value = (message.text or "").strip()
    if not 2 <= len(value) <= 300:
        await message.answer(localizer.text(language, "error.field_length", minimum=2, maximum=300))
        return
    await state.update_data(purpose=value)
    await state.set_state(PresentationFlow.audience)
    await message.answer(localizer.text(language, "create.audience"))


@router.message(PresentationFlow.audience)
async def audience(message: Message, state: FSMContext, localizer: Localizer) -> None:
    data = await state.get_data()
    language = str(data["language"])
    value = (message.text or "").strip()
    if not 2 <= len(value) <= 200:
        await message.answer(localizer.text(language, "error.field_length", minimum=2, maximum=200))
        return
    await state.update_data(audience=value)
    await state.set_state(PresentationFlow.knowledge_level)
    await message.answer(
        localizer.text(language, "create.knowledge"),
        reply_markup=choices_keyboard(KNOWLEDGE_LEVELS, localizer, language),
    )


@router.message(PresentationFlow.knowledge_level)
async def knowledge_level(message: Message, state: FSMContext, localizer: Localizer) -> None:
    data = await state.get_data()
    language = str(data["language"])
    value = resolve_choice(message.text, KNOWLEDGE_LEVELS, localizer, language)
    if value is None:
        await message.answer(localizer.text(language, "create.knowledge"))
        return
    await state.update_data(knowledge_level=value)
    await state.set_state(PresentationFlow.slide_count)
    await message.answer(
        localizer.text(language, "create.slides"),
        reply_markup=choices_keyboard(
            ("8", "10", "12", "15", "18", "20", "25", "30"),
            localizer,
            language,
            columns=4,
        ),
    )


@router.message(PresentationFlow.slide_count)
async def slide_count(message: Message, state: FSMContext, localizer: Localizer) -> None:
    data = await state.get_data()
    language = str(data["language"])
    try:
        value = int(message.text or "")
    except ValueError:
        await message.answer(localizer.text(language, "create.slides"))
        return
    if value not in {8, 10, 12, 15, 18, 20, 25, 30}:
        await message.answer(localizer.text(language, "create.slides"))
        return
    await state.update_data(slide_count=value)
    await state.set_state(PresentationFlow.design_style)
    await message.answer(
        localizer.text(language, "create.design"),
        reply_markup=choices_keyboard(DESIGN_STYLES, localizer, language, columns=3),
    )


@router.message(PresentationFlow.design_style)
async def design_style(message: Message, state: FSMContext, localizer: Localizer) -> None:
    data = await state.get_data()
    language = str(data["language"])
    value = resolve_choice(message.text, DESIGN_STYLES, localizer, language)
    if value is None:
        await message.answer(localizer.text(language, "create.design"))
        return
    await state.update_data(design_style=value)
    await state.set_state(PresentationFlow.plan)
    await message.answer(
        localizer.text(language, "create.plan"),
        reply_markup=choices_keyboard(PLAN_CODES, localizer, language),
    )


@router.message(PresentationFlow.plan)
async def choose_plan(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    localizer: Localizer,
    settings: Settings,
) -> None:
    if message.from_user is None:
        return
    data = await state.get_data()
    language = str(data["language"])
    plan_code = resolve_choice(message.text, PLAN_CODES, localizer, language)
    if plan_code is None:
        return
    try:
        if plan_code == "free_demo":
            order = await FreeDemoService().claim(
                session,
                telegram_user_id=message.from_user.id,
                topic=str(data["topic"]),
                settings=dict(data),
                private_owner_test=(
                    settings.bot_private_mode and message.from_user.id == settings.owner_telegram_id
                ),
            )
        else:
            order = await OrderService().create_paid_order(
                session,
                telegram_user_id=message.from_user.id,
                plan_code=plan_code,
                topic=str(data["topic"]),
                settings=dict(data),
            )
    except (FreeDemoUnavailable, PlanLimitError, ValueError) as exc:
        await message.answer(f"{localizer.text(language, 'create.plan')}\n{exc}")
        return
    await state.update_data(order_id=order.id)

    if plan_code == "free_demo":
        progress_message = await message.answer(localizer.text(language, "progress.ai_outline"))
        presentation = await _generate_outline_safely(
            responder=progress_message,
            session=session,
            settings=settings,
            localizer=localizer,
            language=language,
            order_id=order.id,
            telegram_user_id=message.from_user.id,
        )
        if presentation is None:
            return
        await state.update_data(presentation_id=presentation.id)
        await state.set_state(PresentationFlow.outline_review)
        await _send_outline(progress_message, presentation, localizer, language, edit=True)
        return

    await state.set_state(PresentationFlow.waiting_payment)
    provider = build_payment_provider(settings)
    invoice = await provider.create_invoice(
        order_public_id=order.public_id,
        amount=order.amount,
        currency=order.currency,
        idempotency_key=f"invoice:{provider.name}:{order.id}",
    )
    if provider.name == "mock":
        payment_button = InlineKeyboardButton(
            text=localizer.text(language, "payment.mock_pay"),
            callback_data=f"mockpay:{order.public_id}",
        )
    else:
        payment_button = InlineKeyboardButton(
            text=localizer.text(language, "payment.pay_now"),
            url=invoice.payment_url,
        )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[payment_button]])
    await message.answer(localizer.text(language, "payment.waiting"), reply_markup=keyboard)


@router.callback_query(PresentationFlow.waiting_payment, F.data.startswith("mockpay:"))
async def mock_payment(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    localizer: Localizer,
    settings: Settings,
) -> None:
    if callback.data is None or not isinstance(callback.message, Message):
        return
    data = await state.get_data()
    language = str(data["language"])
    if settings.app_env == "production":
        await callback.answer(localizer.text(language, "payment.disabled"), show_alert=True)
        return
    # Telegram callback queries expire quickly. Acknowledge before payment and
    # AI outline work, which may take tens of seconds on a real provider.
    await callback.answer()
    public_id = callback.data.split(":", 1)[1]
    async with session.begin():
        order = await session.scalar(
            select(Order)
            .join(User, User.id == Order.user_id)
            .where(
                Order.public_id == public_id,
                User.telegram_user_id == callback.from_user.id,
            )
        )
        if order is None:
            return
        order_id = order.id
        order_public_id = order.public_id
        order_amount = order.amount
        order_currency = order.currency
    payload = json.dumps(
        {
            "event_id": f"telegram-{order_id}",
            "transaction_id": f"mock-{order_id}",
            "order_public_id": order_public_id,
            "amount": order_amount,
            "currency": order_currency,
            "status": "paid",
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    secret = settings.mock_payment_secret.get_secret_value()
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    await PaymentService().handle_webhook(
        session,
        MockPaymentProvider(secret=secret),
        payload,
        {"X-Mock-Signature": signature},
    )
    await callback.message.edit_text(localizer.text(language, "progress.ai_outline"))
    presentation = await _generate_outline_safely(
        responder=callback.message,
        session=session,
        settings=settings,
        localizer=localizer,
        language=language,
        order_id=order_id,
        telegram_user_id=callback.from_user.id,
    )
    if presentation is None:
        return
    await state.update_data(presentation_id=presentation.id)
    await state.set_state(PresentationFlow.outline_review)
    await callback.message.answer(localizer.text(language, "payment.confirmed"))
    await _send_outline(callback.message, presentation, localizer, language, edit=True)


@router.callback_query(F.data.startswith("outline:approve"))
async def approve_outline(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    localizer: Localizer,
    settings: Settings,
) -> None:
    if not isinstance(callback.message, Message):
        return
    context = await _authorized_presentation_context(callback, state, session, "approve")
    if context is None:
        await callback.answer(show_alert=True)
        return
    presentation_id, _, language = context
    job = await PresentationService().approve_outline(
        session,
        presentation_id=presentation_id,
        progress_chat_id=callback.message.chat.id,
        progress_message_id=callback.message.message_id,
    )
    if job.status == "QUEUED":
        await callback.message.edit_text(
            f"0% · ⏳ {localizer.text(language, 'create.queued')}",
            reply_markup=generation_cancel_action(localizer, language, presentation_id),
        )
    await state.set_data({"presentation_id": presentation_id, "language": language})
    await state.set_state(None)
    await callback.answer()


@router.callback_query(F.data.startswith("outline:regenerate"))
async def regenerate_outline(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    localizer: Localizer,
    settings: Settings,
) -> None:
    if not isinstance(callback.message, Message):
        return
    context = await _authorized_presentation_context(callback, state, session, "regenerate")
    if context is None:
        await callback.answer(show_alert=True)
        return
    _, order_id, language = context
    await callback.message.edit_text(localizer.text(language, "progress.ai_outline"))
    presentation = await _generate_outline_safely(
        responder=callback.message,
        session=session,
        settings=settings,
        localizer=localizer,
        language=language,
        order_id=order_id,
        telegram_user_id=callback.from_user.id,
    )
    if presentation is None:
        await callback.answer()
        return
    await state.set_state(PresentationFlow.outline_review)
    await _send_outline(callback.message, presentation, localizer, language, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("outline:edit"))
async def edit_outline(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    localizer: Localizer,
) -> None:
    if not isinstance(callback.message, Message):
        return
    context = await _authorized_presentation_context(callback, state, session, "edit")
    if context is None:
        await callback.answer(show_alert=True)
        return
    _, _, language = context
    await state.set_state(PresentationFlow.outline_edit)
    await callback.message.answer(localizer.text(language, "create.edit"))
    await callback.answer()


@router.callback_query(F.data.startswith("outline:cancel"))
async def cancel_outline(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    localizer: Localizer,
    settings: Settings,
) -> None:
    if not isinstance(callback.message, Message):
        return
    context = await _authorized_presentation_context(callback, state, session, "cancel")
    if context is None:
        await callback.answer(show_alert=True)
        return
    presentation_id, _, language = context
    try:
        cancelled = await PresentationService().cancel_outline(
            session,
            presentation_id=presentation_id,
        )
    except ValueError as exc:
        if str(exc) != "outline is no longer cancellable":
            raise
        await callback.answer(localizer.text(language, "error.outline_busy"), show_alert=True)
        return
    await state.clear()
    if not cancelled:
        await callback.answer()
        return
    await callback.message.edit_text(localizer.text(language, "create.cancelled"))
    user = await current_user(session, callback.from_user.id)
    await show_main_menu(callback.message, user=user, localizer=localizer, settings=settings)
    await callback.answer()


@router.message(PresentationFlow.outline_edit)
async def apply_outline_edit(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    localizer: Localizer,
    settings: Settings,
) -> None:
    if message.from_user is None:
        return
    data = await state.get_data()
    order_id = int(data["order_id"])
    instruction = (message.text or "").strip()[:1000]
    if len(instruction) < 2:
        await message.answer(localizer.text(str(data["language"]), "create.edit"))
        return
    async with session.begin():
        order = await session.get(Order, order_id)
        if order is None:
            return
        revised: dict[str, Any] = dict(order.settings)
        revised["outline_edit_request"] = instruction
        order.settings = revised
    progress_message = await message.answer(
        localizer.text(str(data["language"]), "progress.ai_outline")
    )
    presentation = await _generate_outline_safely(
        responder=progress_message,
        session=session,
        settings=settings,
        localizer=localizer,
        language=str(data["language"]),
        order_id=order_id,
        telegram_user_id=message.from_user.id,
    )
    if presentation is None:
        return
    await state.set_state(PresentationFlow.outline_review)
    await _send_outline(
        progress_message,
        presentation,
        localizer,
        str(data["language"]),
        edit=True,
    )


@router.message(
    StateFilter(None),
    F.text,
)
async def quick_presentation(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    localizer: Localizer,
    settings: Settings,
) -> None:
    """Turn one natural-language command into delivered PPTX and PDF files."""
    if message.from_user is None or not message.text:
        return
    if settings.app_env != "test":
        await new_presentation(message, state, session, localizer)
        return
    language = await _language(session, message.from_user.id)
    await state.clear()
    progress = await message.answer(localizer.text(language, "quick.started"))
    try:
        artifacts = await QuickPresentationService(settings).run(
            session_factory,
            telegram_user_id=message.from_user.id,
            text=message.text,
        )
        await progress.edit_text(localizer.text(language, "progress.sending"))
        await message.answer_document(
            FSInputFile(
                artifacts.pptx_path,
                filename=f"presentation-{artifacts.presentation_id}.pptx",
            ),
            caption=localizer.text(language, "quick.pptx_caption"),
        )
        await message.answer_document(
            FSInputFile(
                artifacts.pdf_path,
                filename=f"presentation-{artifacts.presentation_id}.pdf",
            ),
            caption=localizer.text(language, "quick.pdf_caption"),
        )
        await progress.edit_text(localizer.text(language, "quick.completed"))
    except QuickPresentationRequestError:
        logger.info(
            "quick presentation request rejected telegram_user_id=%s",
            message.from_user.id,
        )
        await progress.edit_text(localizer.text(language, "quick.invalid_request"))
    except Exception:
        logger.exception(
            "quick presentation generation failed for telegram user %s",
            message.from_user.id,
        )
        await progress.edit_text(localizer.text(language, "quick.failed"))
