"""Dramatiq actors for durable, idempotent presentation generation."""

import asyncio

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from sqlalchemy import select

from core.config import get_settings
from core.database.session import create_engine, create_session_factory
from modules.files.factory import build_storage
from modules.presentations.delivery import TelegramArtifactDelivery
from modules.presentations.engine import NodePresentationRenderer
from modules.presentations.generation import GenerationPipeline
from modules.presentations.pdf import LibreOfficeConverter
from modules.orders.domain import OrderStatus
from modules.orders.models import Order
from modules.presentations.models import Presentation
from modules.users.models import User

_settings = get_settings()
_broker = RedisBroker(url=_settings.redis_url)  # type: ignore[no-untyped-call]
dramatiq.set_broker(_broker)


async def _generate(presentation_id: int) -> None:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    renderer_path = settings.presentation_renderer.expanduser().resolve()
    storage = build_storage(settings)
    pipeline = GenerationPipeline(
        renderer=NodePresentationRenderer(
            renderer_path.parent,
            node_binary=settings.node_binary,
        ),
        pdf_converter=LibreOfficeConverter(binary=settings.libreoffice_binary),
        storage=storage,
        work_root=settings.generation_work_root,
    )
    try:
        result = await pipeline.run(factory, presentation_id=presentation_id)
        if result.completed:
            from aiogram import Bot

            token = settings.telegram_bot_token.get_secret_value()
            if not token:
                raise RuntimeError("TELEGRAM_BOT_TOKEN is required for artifact delivery")
            delivery = TelegramArtifactDelivery(
                storage=storage,
                work_root=settings.generation_work_root / "delivery",
                max_upload_bytes=settings.max_upload_bytes,
            )
            async with Bot(token=token) as bot:
                await delivery.deliver(
                    factory,
                    presentation_id=presentation_id,
                    bot=bot,
                )
    finally:
        await engine.dispose()


async def _process_paid_order(order_id: int) -> None:
    from aiogram import Bot

    from apps.bot.keyboards import outline_actions
    from apps.bot.localization import Localizer
    from modules.ai.factory import build_ai_provider
    from modules.presentations.service import PresentationService

    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    localizer = Localizer.load_default()
    try:
        async with factory() as session:
            order = await session.get(Order, order_id)
            if order is None:
                raise LookupError(f"order {order_id} not found")
            presentation: Presentation | None
            if OrderStatus(order.status) == OrderStatus.PAID:
                presentation = await PresentationService(
                    ai_provider=build_ai_provider(settings)
                ).generate_outline(session, order_id=order_id)
            elif OrderStatus(order.status) == OrderStatus.WAITING_OUTLINE_APPROVAL:
                presentation = await session.scalar(
                    select(Presentation).where(Presentation.order_id == order_id)
                )
                if presentation is None:
                    raise LookupError("paid order has no outline")
            else:
                return
            if presentation is None:
                raise LookupError("paid order has no presentation")
            user = await session.get(User, order.user_id)
            if user is None:
                raise LookupError("paid order user not found")
            telegram_user_id = user.telegram_user_id
            language = user.selected_language

        token = settings.telegram_bot_token.get_secret_value()
        if not token:
            return
        slides = presentation.outline.get("slides", [])
        lines = [localizer.text(language, "create.outline_ready")]
        lines.extend(
            f"{item.get('number', index)}. {item.get('title', '')}"
            for index, item in enumerate(slides, start=1)
        )
        text = "\n".join(lines)
        if len(text) > 3900:
            text = f"{text[:3880]}\n…"
        async with Bot(token=token) as bot:
            await bot.send_message(
                telegram_user_id,
                text,
                reply_markup=outline_actions(localizer, language, presentation.id),
            )
    finally:
        await engine.dispose()


@dramatiq.actor(
    queue_name="generation",
    max_retries=_settings.generation_max_retries,
    min_backoff=15_000,
    max_backoff=300_000,
    time_limit=600_000,
)
def generate_presentation(presentation_id: int) -> None:
    asyncio.run(_generate(presentation_id))


@dramatiq.actor(
    queue_name="generation_priority",
    max_retries=_settings.generation_max_retries,
    min_backoff=10_000,
    max_backoff=180_000,
    time_limit=600_000,
)
def generate_priority_presentation(presentation_id: int) -> None:
    asyncio.run(_generate(presentation_id))


@dramatiq.actor(
    queue_name="notifications",
    max_retries=_settings.generation_max_retries,
    min_backoff=10_000,
    max_backoff=180_000,
    time_limit=180_000,
)
def process_paid_order(order_id: int) -> None:
    asyncio.run(_process_paid_order(order_id))


@dramatiq.actor(queue_name="health", max_retries=0, time_limit=10_000)
def worker_health() -> str:
    return "ok"
