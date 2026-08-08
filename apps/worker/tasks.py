"""Dramatiq actors for durable, idempotent presentation generation."""

import asyncio
import logging

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dramatiq.middleware import CurrentMessage
from sqlalchemy import select

from core.config import get_settings
from core.database.session import create_engine, create_session_factory
from modules.files.factory import build_storage
from modules.presentations.delivery import (
    DeliveryInProgress,
    DeliveryLeaseLost,
    TelegramArtifactDelivery,
)
from modules.presentations.engine import NodePresentationRenderer
from modules.presentations.generation import (
    GenerationLeaseActive,
    GenerationLeaseLost,
    GenerationPipeline,
    mark_terminal_generation_failure,
)
from modules.presentations.pdf import LibreOfficeConverter
from modules.orders.domain import OrderStatus
from modules.orders.models import Order
from modules.presentations.models import Presentation
from modules.presentations.progress import TelegramProgressReporter, load_progress_destination
from modules.users.models import User

logger = logging.getLogger(__name__)

_settings = get_settings()
_broker = RedisBroker(url=_settings.redis_url)  # type: ignore[no-untyped-call]
_broker.add_middleware(CurrentMessage())  # type: ignore[no-untyped-call]
dramatiq.set_broker(_broker)

_RUNTIME_FAILURES_OPTION = "generation_runtime_failures"


class GenerationAttemptError(RuntimeError):
    def __init__(self, *, failures: int) -> None:
        super().__init__(f"generation attempt failed ({failures})")
        self.failures = failures


def _coordination_retry(
    error: DeliveryInProgress | DeliveryLeaseLost | GenerationLeaseActive | GenerationLeaseLost,
) -> dramatiq.Retry:
    retry_after_ms = max(1_000, int(getattr(error, "retry_after_ms", 1_000)))
    return dramatiq.Retry(  # type: ignore[no-untyped-call]
        "presentation processing coordination changed",
        delay=retry_after_ms,
    )


def _generation_retry_when(retries: int, exception: Exception) -> bool:
    del retries
    if isinstance(exception, dramatiq.Retry):
        return True
    if isinstance(exception, GenerationAttemptError):
        return exception.failures <= _settings.generation_max_retries
    return False


def _failure_progress_step(*, retries: int, max_retries: int) -> str:
    return "failed" if retries >= max_retries else "retrying"


def _current_failure_progress_step() -> str:
    message = CurrentMessage.get_current_message()
    retries = 0 if message is None else int(message.options.get(_RUNTIME_FAILURES_OPTION, 0))
    return _failure_progress_step(
        retries=max(0, retries),
        max_retries=_settings.generation_max_retries,
    )


def _record_runtime_failure() -> int:
    message = CurrentMessage.get_current_message()
    current = 0 if message is None else int(message.options.get(_RUNTIME_FAILURES_OPTION, 0))
    failures = max(0, current) + 1
    if message is not None:
        message.options[_RUNTIME_FAILURES_OPTION] = failures
    return failures


async def _generate(presentation_id: int, *, terminal_failure: bool = False) -> None:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    destination = None
    progress_reporter = None
    try:
        from aiogram import Bot

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
        token = settings.telegram_bot_token.get_secret_value()
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required for artifact delivery")
        destination = await load_progress_destination(
            factory,
            presentation_id=presentation_id,
        )
        async with Bot(token=token) as bot:
            progress_reporter = (
                TelegramProgressReporter(
                    bot=bot,
                    chat_id=destination.chat_id,
                    message_id=destination.message_id,
                    language=destination.language,
                )
                if destination is not None
                else None
            )
            result = await pipeline.run(
                factory,
                presentation_id=presentation_id,
                progress_callback=progress_reporter,
                terminal_failure=terminal_failure,
            )
            if not result.completed:
                return
            delivery = TelegramArtifactDelivery(
                storage=storage,
                work_root=settings.generation_work_root / "delivery",
                max_upload_bytes=settings.max_upload_bytes,
            )
            await delivery.deliver(
                factory,
                presentation_id=presentation_id,
                bot=bot,
            )
            if progress_reporter is not None:
                await progress_reporter(100, "delivered")
    except (
        DeliveryInProgress,
        DeliveryLeaseLost,
        GenerationLeaseActive,
        GenerationLeaseLost,
    ) as exc:
        retry = _coordination_retry(exc)
        logger.info(
            "presentation lease is unavailable presentation_id=%s retry_after_ms=%s",
            presentation_id,
            retry.delay,
        )
        raise retry from exc
    except Exception as error:
        logger.exception(
            "presentation generation failed job_id=%s presentation_id=%s "
            "telegram_user_id=%s correlation_id=generation-%s",
            destination.generation_job_id if destination is not None else None,
            presentation_id,
            destination.chat_id if destination is not None else None,
            presentation_id,
        )
        if terminal_failure:
            try:
                await mark_terminal_generation_failure(
                    factory,
                    presentation_id=presentation_id,
                    error=error,
                )
            except Exception:
                logger.exception(
                    "terminal generation failure could not be persisted presentation_id=%s",
                    presentation_id,
                )
        if progress_reporter is not None:
            await progress_reporter(0, "failed" if terminal_failure else "retrying")
        raise
    finally:
        await engine.dispose()


def _run_generation_actor(presentation_id: int) -> None:
    try:
        asyncio.run(
            _generate(
                presentation_id,
                terminal_failure=_current_failure_progress_step() == "failed",
            )
        )
    except dramatiq.Retry:
        raise
    except Exception as exc:
        raise GenerationAttemptError(failures=_record_runtime_failure()) from exc


async def _process_paid_order(order_id: int) -> None:
    from aiogram import Bot

    from apps.bot.keyboards import outline_actions
    from apps.bot.localization import Localizer
    from modules.ai.factory import build_ai_provider, close_ai_provider
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
                provider = build_ai_provider(settings)
                try:
                    presentation = await PresentationService(ai_provider=provider).generate_outline(
                        session, order_id=order_id
                    )
                finally:
                    await close_ai_provider(provider)
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
            language = presentation.language

        token = settings.telegram_bot_token.get_secret_value()
        if not token:
            return
        slides = presentation.outline.get("slides", [])
        lines = [localizer.text(language, "create.outline")]
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
    retry_when=_generation_retry_when,
    min_backoff=15_000,
    max_backoff=300_000,
    time_limit=600_000,
)
def generate_presentation(presentation_id: int) -> None:
    _run_generation_actor(presentation_id)


@dramatiq.actor(
    queue_name="generation_priority",
    max_retries=_settings.generation_max_retries,
    retry_when=_generation_retry_when,
    min_backoff=10_000,
    max_backoff=180_000,
    time_limit=600_000,
)
def generate_priority_presentation(presentation_id: int) -> None:
    _run_generation_actor(presentation_id)


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
