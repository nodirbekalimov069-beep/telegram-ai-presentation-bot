"""Dramatiq actors for durable, idempotent presentation generation."""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dramatiq.middleware import CurrentMessage, TimeLimitExceeded
from sqlalchemy import select

from core.config import get_settings
from core.database.session import create_engine, create_session_factory
from core.outbox.models import OutboxEvent
from modules.ai.factory import build_ai_provider, close_ai_provider
from modules.files.factory import build_storage
from modules.presentations.delivery import (
    DeliveryInProgress,
    DeliveryLeaseLost,
    TelegramArtifactDelivery,
    mark_terminal_delivery_timeout,
)
from modules.presentations.engine import NodePresentationRenderer
from modules.presentations.generation import (
    GenerationLeaseActive,
    GenerationLeaseLost,
    GenerationPipeline,
    mark_terminal_generation_failure,
)
from modules.presentations.pdf import LibreOfficeConverter
from modules.service_orders.essay_delivery import (
    EssayDeliveryAmbiguous,
    EssayDeliveryAmbiguousPersistencePending,
    EssayDeliveryInProgress,
    EssayDeliveryLeaseLost,
    TelegramCourseWorkDelivery,
    TelegramEssayDelivery,
    TelegramIndependentWorkDelivery,
    TelegramQuizDelivery,
    TelegramRewriteDelivery,
    TelegramTranslationDelivery,
    TelegramDocumentDelivery,
    TelegramSummaryDelivery,
    TelegramWriteTextDelivery,
    mark_terminal_course_work_delivery_failure,
    mark_terminal_essay_delivery_failure,
    mark_terminal_independent_work_delivery_failure,
    mark_terminal_quiz_delivery_failure,
    mark_terminal_rewrite_delivery_failure,
    mark_terminal_translation_delivery_failure,
    mark_terminal_document_service_delivery_failure,
    mark_terminal_summary_delivery_failure,
    mark_terminal_write_text_delivery_failure,
)
from modules.service_orders.essay_pipeline import (
    CourseWorkPipeline,
    EssayLeaseBusy,
    EssayPipeline,
    EssayTerminalError,
    IndependentWorkPipeline,
    QuizPipeline,
    RewritePipeline,
    TranslationPipeline,
    DocumentPipeline,
    SummaryPipeline,
    WriteTextPipeline,
    cleanup_course_work_orphan_artifacts,
    cleanup_essay_orphan_artifacts,
    cleanup_independent_work_orphan_artifacts,
    cleanup_quiz_orphan_artifacts,
    cleanup_rewrite_orphan_artifacts,
    cleanup_translation_orphan_artifacts,
    cleanup_document_service_orphan_artifacts,
    cleanup_summary_orphan_artifacts,
    cleanup_write_text_orphan_artifacts,
    mark_terminal_course_work_failure,
    mark_terminal_essay_failure,
    mark_terminal_independent_work_failure,
    mark_terminal_quiz_failure,
    mark_terminal_rewrite_failure,
    mark_terminal_translation_failure,
    mark_terminal_document_service_failure,
    mark_terminal_summary_failure,
    mark_terminal_write_text_failure,
)
from modules.service_orders.catalog import ServiceType
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
_TIMEOUT_FAILURES_OPTION = "generation_timeouts"
_TIMEOUT_RETRY_DELAY_MS = 60_000


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


def _cleanup_persistence_retry_when(retries: int, exception: Exception) -> bool:
    del retries
    return isinstance(exception, dramatiq.Retry)


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


def _current_timeout_failures() -> int:
    message = CurrentMessage.get_current_message()
    failures = 0 if message is None else int(message.options.get(_TIMEOUT_FAILURES_OPTION, 0))
    return max(0, failures)


def _record_timeout_failure() -> int:
    message = CurrentMessage.get_current_message()
    failures = _current_timeout_failures() + 1
    if message is not None:
        message.options[_TIMEOUT_FAILURES_OPTION] = failures
    return failures


async def _generate(presentation_id: int, *, terminal_failure: bool = False) -> None:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    destination = None
    progress_reporter = None
    provider = None
    try:
        from aiogram import Bot

        renderer_path = settings.presentation_renderer.expanduser().resolve()
        storage = build_storage(settings)
        provider = build_ai_provider(settings)
        pipeline = GenerationPipeline(
            renderer=NodePresentationRenderer(
                renderer_path.parent,
                node_binary=settings.node_binary,
            ),
            pdf_converter=LibreOfficeConverter(binary=settings.libreoffice_binary),
            storage=storage,
            work_root=settings.generation_work_root,
            content_provider=provider,
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
        if provider is not None:
            await close_ai_provider(provider)
        await engine.dispose()


async def _finalize_timed_out_presentation(presentation_id: int) -> bool:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    timeout = TimeoutError("Dramatiq actor time limit exhausted")
    try:
        if await mark_terminal_generation_failure(
            factory,
            presentation_id=presentation_id,
            error=timeout,
        ):
            return True
        return await mark_terminal_delivery_timeout(
            factory,
            presentation_id=presentation_id,
            error=timeout,
        )
    finally:
        await engine.dispose()


def _run_generation_actor(presentation_id: int) -> None:
    if _current_timeout_failures() > _settings.generation_max_retries:
        try:
            resolved = asyncio.run(_finalize_timed_out_presentation(presentation_id))
        except Exception:
            logger.exception(
                "timed-out presentation finalization failed presentation_id=%s",
                presentation_id,
            )
            resolved = False
        if resolved:
            return
        raise dramatiq.Retry(  # type: ignore[no-untyped-call]
            "waiting for timed-out presentation lease recovery",
            delay=_TIMEOUT_RETRY_DELAY_MS,
        )
    try:
        asyncio.run(
            _generate(
                presentation_id,
                terminal_failure=_current_failure_progress_step() == "failed",
            )
        )
    except TimeLimitExceeded as exc:
        _record_timeout_failure()
        raise dramatiq.Retry(  # type: ignore[no-untyped-call]
            "presentation actor timed out; waiting for fenced recovery",
            delay=_TIMEOUT_RETRY_DELAY_MS,
        ) from exc
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


async def _recover_paid_order_timeout(order_id: int, *, terminal_failure: bool) -> bool:
    from modules.presentations.service import PresentationService

    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            return await PresentationService().recover_paid_outline_timeout(
                session,
                order_id=order_id,
                terminal_failure=terminal_failure,
            )
    finally:
        await engine.dispose()


def _run_paid_order_actor(order_id: int) -> None:
    message = CurrentMessage.get_current_message()
    retries = 0 if message is None else int(message.options.get("retries", 0))
    terminal_failure = retries >= _settings.generation_max_retries
    try:
        asyncio.run(_process_paid_order(order_id))
    except TimeLimitExceeded as exc:
        recovered = asyncio.run(
            _recover_paid_order_timeout(
                order_id,
                terminal_failure=terminal_failure,
            )
        )
        if not recovered:
            raise dramatiq.Retry(  # type: ignore[no-untyped-call]
                "waiting for paid outline timeout recovery",
                delay=_TIMEOUT_RETRY_DELAY_MS,
            ) from exc
        raise RuntimeError("paid outline actor timed out") from exc


async def _process_service_document(service_order_id: int, *, service_type: ServiceType) -> None:
    from aiogram import Bot

    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    provider = None
    try:
        provider = build_ai_provider(settings)
        storage = build_storage(settings)

        async def convert_docx(source, output_dir, *, timeout_seconds):  # type: ignore[no-untyped-def]
            converter = LibreOfficeConverter(
                binary=settings.libreoffice_binary,
                timeout_seconds=timeout_seconds,
            )
            return await converter.convert(source, output_dir / f"{source.stem}.pdf")

        if service_type is ServiceType.ESSAY:
            pipeline = EssayPipeline(
                factory,
                provider,
                storage,
                temp_root=settings.generation_work_root / "service-essays",
                pdf_converter=convert_docx,
            )
            delivery = TelegramEssayDelivery(
                storage=storage,
                work_root=settings.generation_work_root / "service-essay-delivery",
                max_upload_bytes=settings.max_upload_bytes,
            )
        elif service_type is ServiceType.INDEPENDENT_WORK:
            pipeline = IndependentWorkPipeline(
                factory,
                provider,
                storage,
                temp_root=settings.generation_work_root / "service-independent",
                pdf_converter=convert_docx,
            )
            delivery = TelegramIndependentWorkDelivery(
                storage=storage,
                work_root=settings.generation_work_root / "service-independent-delivery",
                max_upload_bytes=settings.max_upload_bytes,
            )
        elif service_type is ServiceType.COURSE_WORK:
            pipeline = CourseWorkPipeline(
                factory,
                provider,
                storage,
                temp_root=settings.generation_work_root / "service-course",
                pdf_converter=convert_docx,
            )
            delivery = TelegramCourseWorkDelivery(
                storage=storage,
                work_root=settings.generation_work_root / "service-course-delivery",
                max_upload_bytes=settings.max_upload_bytes,
            )
        elif service_type is ServiceType.SUMMARY:
            pipeline = SummaryPipeline(
                factory,
                provider,
                storage,
                temp_root=settings.generation_work_root / "service-summary",
                pdf_converter=convert_docx,
            )
            delivery = TelegramSummaryDelivery(
                storage=storage,
                work_root=settings.generation_work_root / "service-summary-delivery",
                max_upload_bytes=settings.max_upload_bytes,
            )
        elif service_type is ServiceType.QUIZ:
            pipeline = QuizPipeline(
                factory,
                provider,
                storage,
                temp_root=settings.generation_work_root / "service-quiz",
                pdf_converter=convert_docx,
            )
            delivery = TelegramQuizDelivery(
                storage=storage,
                work_root=settings.generation_work_root / "service-quiz-delivery",
                max_upload_bytes=settings.max_upload_bytes,
            )
        elif service_type is ServiceType.WRITE_TEXT:
            pipeline = WriteTextPipeline(
                factory,
                provider,
                storage,
                temp_root=settings.generation_work_root / "service-write-text",
                pdf_converter=convert_docx,
            )
            delivery = TelegramWriteTextDelivery(
                storage=storage,
                work_root=settings.generation_work_root / "service-write-text-delivery",
                max_upload_bytes=settings.max_upload_bytes,
            )
        elif service_type is ServiceType.REWRITE:
            pipeline = RewritePipeline(
                factory,
                provider,
                storage,
                temp_root=settings.generation_work_root / "service-rewrite",
                pdf_converter=convert_docx,
            )
            delivery = TelegramRewriteDelivery(
                storage=storage,
                work_root=settings.generation_work_root / "service-rewrite-delivery",
                max_upload_bytes=settings.max_upload_bytes,
            )
        elif service_type is ServiceType.TRANSLATE:
            pipeline = TranslationPipeline(
                factory,
                provider,
                storage,
                temp_root=settings.generation_work_root / "service-translation",
                pdf_converter=convert_docx,
            )
            delivery = TelegramTranslationDelivery(
                storage=storage,
                work_root=settings.generation_work_root / "service-translation-delivery",
                max_upload_bytes=settings.max_upload_bytes,
            )
        elif service_type is ServiceType.DOCUMENT:
            pipeline = DocumentPipeline(
                factory,
                provider,
                storage,
                temp_root=settings.generation_work_root / "service-document",
                pdf_converter=convert_docx,
            )
            delivery = TelegramDocumentDelivery(
                storage=storage,
                work_root=settings.generation_work_root / "service-document-delivery",
                max_upload_bytes=settings.max_upload_bytes,
            )
        else:
            raise ValueError("unsupported production document service")
        await pipeline.process(service_order_id)
        token = settings.telegram_bot_token.get_secret_value()
        if not token:
            raise RuntimeError("Telegram bot token is required for document delivery")
        async with Bot(token=token) as bot:
            await delivery.deliver(factory, service_order_id=service_order_id, bot=bot)
    finally:
        if provider is not None:
            await close_ai_provider(provider)
        await engine.dispose()


async def _process_service_essay(service_order_id: int) -> None:
    await _process_service_document(service_order_id, service_type=ServiceType.ESSAY)


async def _process_service_independent(service_order_id: int) -> None:
    await _process_service_document(service_order_id, service_type=ServiceType.INDEPENDENT_WORK)


async def _process_service_course(service_order_id: int) -> None:
    await _process_service_document(service_order_id, service_type=ServiceType.COURSE_WORK)


async def _process_service_summary(service_order_id: int) -> None:
    await _process_service_document(service_order_id, service_type=ServiceType.SUMMARY)


async def _process_service_quiz(service_order_id: int) -> None:
    await _process_service_document(service_order_id, service_type=ServiceType.QUIZ)


async def _process_service_write_text(service_order_id: int) -> None:
    await _process_service_document(service_order_id, service_type=ServiceType.WRITE_TEXT)


async def _process_service_rewrite(service_order_id: int) -> None:
    await _process_service_document(service_order_id, service_type=ServiceType.REWRITE)


async def _process_service_translation(service_order_id: int) -> None:
    await _process_service_document(service_order_id, service_type=ServiceType.TRANSLATE)


async def _process_service_document_draft(service_order_id: int) -> None:
    await _process_service_document(service_order_id, service_type=ServiceType.DOCUMENT)


async def _finalize_service_essay(service_order_id: int) -> bool:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        if await mark_terminal_essay_delivery_failure(factory, service_order_id=service_order_id):
            return True
        return await mark_terminal_essay_failure(factory, order_id=service_order_id)
    finally:
        await engine.dispose()


async def _finalize_service_independent(service_order_id: int) -> bool:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        if await mark_terminal_independent_work_delivery_failure(
            factory, service_order_id=service_order_id
        ):
            return True
        return await mark_terminal_independent_work_failure(factory, order_id=service_order_id)
    finally:
        await engine.dispose()


async def _finalize_service_course(service_order_id: int) -> bool:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        if await mark_terminal_course_work_delivery_failure(
            factory, service_order_id=service_order_id
        ):
            return True
        return await mark_terminal_course_work_failure(factory, order_id=service_order_id)
    finally:
        await engine.dispose()


async def _finalize_service_summary(service_order_id: int) -> bool:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        if await mark_terminal_summary_delivery_failure(factory, service_order_id=service_order_id):
            return True
        return await mark_terminal_summary_failure(factory, order_id=service_order_id)
    finally:
        await engine.dispose()


async def _finalize_service_quiz(service_order_id: int) -> bool:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        if await mark_terminal_quiz_delivery_failure(factory, service_order_id=service_order_id):
            return True
        return await mark_terminal_quiz_failure(factory, order_id=service_order_id)
    finally:
        await engine.dispose()


async def _finalize_service_write_text(service_order_id: int) -> bool:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        if await mark_terminal_write_text_delivery_failure(
            factory, service_order_id=service_order_id
        ):
            return True
        return await mark_terminal_write_text_failure(factory, order_id=service_order_id)
    finally:
        await engine.dispose()


async def _finalize_service_rewrite(service_order_id: int) -> bool:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        if await mark_terminal_rewrite_delivery_failure(factory, service_order_id=service_order_id):
            return True
        return await mark_terminal_rewrite_failure(factory, order_id=service_order_id)
    finally:
        await engine.dispose()


async def _finalize_service_translation(service_order_id: int) -> bool:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        if await mark_terminal_translation_delivery_failure(
            factory, service_order_id=service_order_id
        ):
            return True
        return await mark_terminal_translation_failure(factory, order_id=service_order_id)
    finally:
        await engine.dispose()


async def _finalize_service_document_draft(service_order_id: int) -> bool:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        if await mark_terminal_document_service_delivery_failure(
            factory, service_order_id=service_order_id
        ):
            return True
        return await mark_terminal_document_service_failure(factory, order_id=service_order_id)
    finally:
        await engine.dispose()


async def _cleanup_service_essay_artifacts(service_order_id: int) -> None:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    storage = build_storage(settings)
    try:
        await cleanup_essay_orphan_artifacts(factory, storage, order_id=service_order_id)
    finally:
        await engine.dispose()


async def _cleanup_service_independent_artifacts(service_order_id: int) -> None:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    storage = build_storage(settings)
    try:
        await cleanup_independent_work_orphan_artifacts(factory, storage, order_id=service_order_id)
    finally:
        await engine.dispose()


async def _cleanup_service_course_artifacts(service_order_id: int) -> None:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    storage = build_storage(settings)
    try:
        await cleanup_course_work_orphan_artifacts(factory, storage, order_id=service_order_id)
    finally:
        await engine.dispose()


async def _cleanup_service_summary_artifacts(service_order_id: int) -> None:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    storage = build_storage(settings)
    try:
        await cleanup_summary_orphan_artifacts(factory, storage, order_id=service_order_id)
    finally:
        await engine.dispose()


async def _cleanup_service_quiz_artifacts(service_order_id: int) -> None:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    storage = build_storage(settings)
    try:
        await cleanup_quiz_orphan_artifacts(factory, storage, order_id=service_order_id)
    finally:
        await engine.dispose()


async def _cleanup_service_write_text_artifacts(service_order_id: int) -> None:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    storage = build_storage(settings)
    try:
        await cleanup_write_text_orphan_artifacts(factory, storage, order_id=service_order_id)
    finally:
        await engine.dispose()


async def _cleanup_service_rewrite_artifacts(service_order_id: int) -> None:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    storage = build_storage(settings)
    try:
        await cleanup_rewrite_orphan_artifacts(factory, storage, order_id=service_order_id)
    finally:
        await engine.dispose()


async def _cleanup_service_translation_artifacts(service_order_id: int) -> None:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    storage = build_storage(settings)
    try:
        await cleanup_translation_orphan_artifacts(factory, storage, order_id=service_order_id)
    finally:
        await engine.dispose()


async def _cleanup_service_document_artifacts(service_order_id: int) -> None:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    storage = build_storage(settings)
    try:
        await cleanup_document_service_orphan_artifacts(factory, storage, order_id=service_order_id)
    finally:
        await engine.dispose()


async def _requeue_service_document_cleanup(
    event_id: int, error: BaseException, *, event_type: str
) -> None:
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            event = await session.get(OutboxEvent, event_id, with_for_update=True)
            if event is None or event.event_type != event_type:
                raise LookupError("document cleanup outbox event not found")
            event.status = "PENDING"
            event.attempts = 0
            event.available_at = datetime.now(UTC) + timedelta(hours=1)
            event.published_at = None
            event.last_error = f"{type(error).__name__}: cleanup retry scheduled"[:2000]
    finally:
        await engine.dispose()


async def _requeue_service_essay_cleanup(event_id: int, error: BaseException) -> None:
    await _requeue_service_document_cleanup(
        event_id,
        error,
        event_type="service.essay.cleanup_requested",
    )


async def _requeue_service_independent_cleanup(event_id: int, error: BaseException) -> None:
    await _requeue_service_document_cleanup(
        event_id,
        error,
        event_type="service.independent.cleanup_requested",
    )


async def _requeue_service_course_cleanup(event_id: int, error: BaseException) -> None:
    await _requeue_service_document_cleanup(
        event_id,
        error,
        event_type="service.course.cleanup_requested",
    )


async def _requeue_service_summary_cleanup(event_id: int, error: BaseException) -> None:
    await _requeue_service_document_cleanup(
        event_id,
        error,
        event_type="service.summary.cleanup_requested",
    )


async def _requeue_service_quiz_cleanup(event_id: int, error: BaseException) -> None:
    await _requeue_service_document_cleanup(
        event_id,
        error,
        event_type="service.quiz.cleanup_requested",
    )


async def _requeue_service_write_text_cleanup(event_id: int, error: BaseException) -> None:
    await _requeue_service_document_cleanup(
        event_id,
        error,
        event_type="service.write.cleanup_requested",
    )


async def _requeue_service_rewrite_cleanup(event_id: int, error: BaseException) -> None:
    await _requeue_service_document_cleanup(
        event_id,
        error,
        event_type="service.rewrite.cleanup_requested",
    )


async def _requeue_service_translation_cleanup(event_id: int, error: BaseException) -> None:
    await _requeue_service_document_cleanup(
        event_id, error, event_type="service.translate.cleanup_requested"
    )


async def _requeue_service_document_draft_cleanup(event_id: int, error: BaseException) -> None:
    await _requeue_service_document_cleanup(
        event_id, error, event_type="service.document.cleanup_requested"
    )


def _run_service_document_actor(
    service_order_id: int,
    *,
    process: Callable[[int], Coroutine[Any, Any, None]],
    finalize: Callable[[int], Coroutine[Any, Any, bool]],
    label: str,
) -> None:
    if _current_timeout_failures() > _settings.generation_max_retries:
        try:
            resolved = asyncio.run(finalize(service_order_id))
        except Exception:
            logger.exception(
                "timed-out %s finalization failed service_order_id=%s",
                label,
                service_order_id,
            )
            resolved = False
        if resolved:
            return
        raise dramatiq.Retry(  # type: ignore[no-untyped-call]
            f"waiting for timed-out {label} lease recovery",
            delay=_TIMEOUT_RETRY_DELAY_MS,
        )
    try:
        asyncio.run(process(service_order_id))
    except EssayDeliveryAmbiguousPersistencePending as exc:
        raise dramatiq.Retry(  # type: ignore[no-untyped-call]
            f"waiting to persist ambiguous {label} delivery",
            delay=_TIMEOUT_RETRY_DELAY_MS,
        ) from exc
    except EssayDeliveryAmbiguous:
        logger.warning(
            "%s delivery acceptance ambiguous; automatic resend disabled service_order_id=%s",
            label,
            service_order_id,
        )
        return
    except (EssayLeaseBusy, EssayDeliveryInProgress, EssayDeliveryLeaseLost) as exc:
        retry_after_ms = max(1_000, int(getattr(exc, "retry_after_ms", 60_000)))
        raise dramatiq.Retry(  # type: ignore[no-untyped-call]
            f"{label} processing lease is active",
            delay=retry_after_ms,
        ) from exc
    except EssayTerminalError:
        return
    except TimeLimitExceeded as exc:
        _record_timeout_failure()
        raise dramatiq.Retry(  # type: ignore[no-untyped-call]
            f"{label} actor timed out; waiting for lease recovery",
            delay=_TIMEOUT_RETRY_DELAY_MS,
        ) from exc
    except Exception as exc:
        failures = _record_runtime_failure()
        if failures > _settings.generation_max_retries:
            try:
                resolved = asyncio.run(finalize(service_order_id))
            except Exception:
                logger.exception(
                    "%s finalization failed service_order_id=%s", label, service_order_id
                )
                resolved = False
            if resolved:
                return
            raise dramatiq.Retry(  # type: ignore[no-untyped-call]
                f"waiting for {label} terminal-state persistence",
                delay=_TIMEOUT_RETRY_DELAY_MS,
            ) from exc
        raise GenerationAttemptError(failures=failures) from exc


def _run_service_essay_actor(service_order_id: int) -> None:
    _run_service_document_actor(
        service_order_id,
        process=_process_service_essay,
        finalize=_finalize_service_essay,
        label="essay",
    )


def _run_service_independent_actor(service_order_id: int) -> None:
    _run_service_document_actor(
        service_order_id,
        process=_process_service_independent,
        finalize=_finalize_service_independent,
        label="independent work",
    )


def _run_service_course_actor(service_order_id: int) -> None:
    _run_service_document_actor(
        service_order_id,
        process=_process_service_course,
        finalize=_finalize_service_course,
        label="course work",
    )


def _run_service_summary_actor(service_order_id: int) -> None:
    _run_service_document_actor(
        service_order_id,
        process=_process_service_summary,
        finalize=_finalize_service_summary,
        label="summary",
    )


def _run_service_quiz_actor(service_order_id: int) -> None:
    _run_service_document_actor(
        service_order_id,
        process=_process_service_quiz,
        finalize=_finalize_service_quiz,
        label="quiz",
    )


def _run_service_write_text_actor(service_order_id: int) -> None:
    _run_service_document_actor(
        service_order_id,
        process=_process_service_write_text,
        finalize=_finalize_service_write_text,
        label="write text",
    )


def _run_service_rewrite_actor(service_order_id: int) -> None:
    _run_service_document_actor(
        service_order_id,
        process=_process_service_rewrite,
        finalize=_finalize_service_rewrite,
        label="rewrite",
    )


def _run_service_translation_actor(service_order_id: int) -> None:
    _run_service_document_actor(
        service_order_id,
        process=_process_service_translation,
        finalize=_finalize_service_translation,
        label="translation",
    )


def _run_service_document_draft_actor(service_order_id: int) -> None:
    _run_service_document_actor(
        service_order_id,
        process=_process_service_document_draft,
        finalize=_finalize_service_document_draft,
        label="document",
    )


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
    _run_paid_order_actor(order_id)


@dramatiq.actor(
    queue_name="generation",
    max_retries=_settings.generation_max_retries,
    retry_when=_generation_retry_when,
    min_backoff=15_000,
    max_backoff=300_000,
    time_limit=600_000,
)
def generate_service_essay(service_order_id: int) -> None:
    _run_service_essay_actor(service_order_id)


@dramatiq.actor(
    queue_name="generation",
    max_retries=_settings.generation_max_retries,
    retry_when=_generation_retry_when,
    min_backoff=15_000,
    max_backoff=300_000,
    time_limit=600_000,
)
def generate_service_independent(service_order_id: int) -> None:
    _run_service_independent_actor(service_order_id)


@dramatiq.actor(
    queue_name="generation",
    max_retries=_settings.generation_max_retries,
    retry_when=_generation_retry_when,
    min_backoff=15_000,
    max_backoff=300_000,
    time_limit=600_000,
)
def generate_service_course(service_order_id: int) -> None:
    _run_service_course_actor(service_order_id)


@dramatiq.actor(
    queue_name="generation",
    max_retries=_settings.generation_max_retries,
    retry_when=_generation_retry_when,
    min_backoff=15_000,
    max_backoff=300_000,
    time_limit=600_000,
)
def generate_service_summary(service_order_id: int) -> None:
    _run_service_summary_actor(service_order_id)


@dramatiq.actor(
    queue_name="generation",
    max_retries=_settings.generation_max_retries,
    retry_when=_generation_retry_when,
    min_backoff=15_000,
    max_backoff=300_000,
    time_limit=600_000,
)
def generate_service_quiz(service_order_id: int) -> None:
    _run_service_quiz_actor(service_order_id)


@dramatiq.actor(
    queue_name="generation",
    max_retries=_settings.generation_max_retries,
    retry_when=_generation_retry_when,
    min_backoff=15_000,
    max_backoff=300_000,
    time_limit=600_000,
)
def generate_service_write_text(service_order_id: int) -> None:
    _run_service_write_text_actor(service_order_id)


@dramatiq.actor(
    queue_name="generation",
    max_retries=_settings.generation_max_retries,
    retry_when=_generation_retry_when,
    min_backoff=15_000,
    max_backoff=300_000,
    time_limit=600_000,
)
def generate_service_rewrite(service_order_id: int) -> None:
    _run_service_rewrite_actor(service_order_id)


@dramatiq.actor(
    queue_name="generation",
    max_retries=_settings.generation_max_retries,
    retry_when=_generation_retry_when,
    min_backoff=15_000,
    max_backoff=300_000,
    time_limit=600_000,
)
def generate_service_translation(service_order_id: int) -> None:
    _run_service_translation_actor(service_order_id)


@dramatiq.actor(
    queue_name="generation",
    max_retries=_settings.generation_max_retries,
    retry_when=_generation_retry_when,
    min_backoff=15_000,
    max_backoff=300_000,
    time_limit=600_000,
)
def generate_service_document(service_order_id: int) -> None:
    _run_service_document_draft_actor(service_order_id)


def _run_service_cleanup_actor(
    service_order_id: int,
    outbox_event_id: int,
    *,
    cleanup: Callable[[int], Coroutine[Any, Any, None]],
    requeue: Callable[[int, BaseException], Coroutine[Any, Any, None]],
    label: str,
) -> None:
    try:
        asyncio.run(cleanup(service_order_id))
    except (Exception, TimeLimitExceeded) as exc:
        try:
            asyncio.run(requeue(outbox_event_id, exc))
        except Exception as persistence_exc:
            raise dramatiq.Retry(  # type: ignore[no-untyped-call]
                f"waiting to persist {label} cleanup requeue",
                delay=60_000,
            ) from persistence_exc


def _run_service_essay_cleanup_actor(service_order_id: int, outbox_event_id: int) -> None:
    _run_service_cleanup_actor(
        service_order_id,
        outbox_event_id,
        cleanup=_cleanup_service_essay_artifacts,
        requeue=_requeue_service_essay_cleanup,
        label="essay",
    )


def _run_service_independent_cleanup_actor(service_order_id: int, outbox_event_id: int) -> None:
    _run_service_cleanup_actor(
        service_order_id,
        outbox_event_id,
        cleanup=_cleanup_service_independent_artifacts,
        requeue=_requeue_service_independent_cleanup,
        label="independent work",
    )


def _run_service_course_cleanup_actor(service_order_id: int, outbox_event_id: int) -> None:
    _run_service_cleanup_actor(
        service_order_id,
        outbox_event_id,
        cleanup=_cleanup_service_course_artifacts,
        requeue=_requeue_service_course_cleanup,
        label="course work",
    )


def _run_service_summary_cleanup_actor(service_order_id: int, outbox_event_id: int) -> None:
    _run_service_cleanup_actor(
        service_order_id,
        outbox_event_id,
        cleanup=_cleanup_service_summary_artifacts,
        requeue=_requeue_service_summary_cleanup,
        label="summary",
    )


def _run_service_quiz_cleanup_actor(service_order_id: int, outbox_event_id: int) -> None:
    _run_service_cleanup_actor(
        service_order_id,
        outbox_event_id,
        cleanup=_cleanup_service_quiz_artifacts,
        requeue=_requeue_service_quiz_cleanup,
        label="quiz",
    )


def _run_service_write_text_cleanup_actor(service_order_id: int, outbox_event_id: int) -> None:
    _run_service_cleanup_actor(
        service_order_id,
        outbox_event_id,
        cleanup=_cleanup_service_write_text_artifacts,
        requeue=_requeue_service_write_text_cleanup,
        label="write text",
    )


def _run_service_rewrite_cleanup_actor(service_order_id: int, outbox_event_id: int) -> None:
    _run_service_cleanup_actor(
        service_order_id,
        outbox_event_id,
        cleanup=_cleanup_service_rewrite_artifacts,
        requeue=_requeue_service_rewrite_cleanup,
        label="rewrite",
    )


def _run_service_translation_cleanup_actor(service_order_id: int, outbox_event_id: int) -> None:
    _run_service_cleanup_actor(
        service_order_id,
        outbox_event_id,
        cleanup=_cleanup_service_translation_artifacts,
        requeue=_requeue_service_translation_cleanup,
        label="translation",
    )


def _run_service_document_cleanup_actor(service_order_id: int, outbox_event_id: int) -> None:
    _run_service_cleanup_actor(
        service_order_id,
        outbox_event_id,
        cleanup=_cleanup_service_document_artifacts,
        requeue=_requeue_service_document_draft_cleanup,
        label="document",
    )


@dramatiq.actor(
    queue_name="generation",
    max_retries=10,
    retry_when=_cleanup_persistence_retry_when,
    min_backoff=60_000,
    max_backoff=3_600_000,
    time_limit=300_000,
)
def cleanup_service_essay_artifacts(service_order_id: int, outbox_event_id: int) -> None:
    _run_service_essay_cleanup_actor(service_order_id, outbox_event_id)


@dramatiq.actor(
    queue_name="generation",
    max_retries=10,
    retry_when=_cleanup_persistence_retry_when,
    min_backoff=60_000,
    max_backoff=3_600_000,
    time_limit=300_000,
)
def cleanup_service_independent_artifacts(service_order_id: int, outbox_event_id: int) -> None:
    _run_service_independent_cleanup_actor(service_order_id, outbox_event_id)


@dramatiq.actor(
    queue_name="generation",
    max_retries=10,
    retry_when=_cleanup_persistence_retry_when,
    min_backoff=60_000,
    max_backoff=3_600_000,
    time_limit=300_000,
)
def cleanup_service_course_artifacts(service_order_id: int, outbox_event_id: int) -> None:
    _run_service_course_cleanup_actor(service_order_id, outbox_event_id)


@dramatiq.actor(
    queue_name="generation",
    max_retries=10,
    retry_when=_cleanup_persistence_retry_when,
    min_backoff=60_000,
    max_backoff=3_600_000,
    time_limit=300_000,
)
def cleanup_service_summary_artifacts(service_order_id: int, outbox_event_id: int) -> None:
    _run_service_summary_cleanup_actor(service_order_id, outbox_event_id)


@dramatiq.actor(
    queue_name="generation",
    max_retries=10,
    retry_when=_cleanup_persistence_retry_when,
    min_backoff=60_000,
    max_backoff=3_600_000,
    time_limit=300_000,
)
def cleanup_service_quiz_artifacts(service_order_id: int, outbox_event_id: int) -> None:
    _run_service_quiz_cleanup_actor(service_order_id, outbox_event_id)


@dramatiq.actor(
    queue_name="generation",
    max_retries=10,
    retry_when=_cleanup_persistence_retry_when,
    min_backoff=60_000,
    max_backoff=3_600_000,
    time_limit=300_000,
)
def cleanup_service_write_text_artifacts(service_order_id: int, outbox_event_id: int) -> None:
    _run_service_write_text_cleanup_actor(service_order_id, outbox_event_id)


@dramatiq.actor(
    queue_name="generation",
    max_retries=10,
    retry_when=_cleanup_persistence_retry_when,
    min_backoff=60_000,
    max_backoff=3_600_000,
    time_limit=300_000,
)
def cleanup_service_rewrite_artifacts(service_order_id: int, outbox_event_id: int) -> None:
    _run_service_rewrite_cleanup_actor(service_order_id, outbox_event_id)


@dramatiq.actor(
    queue_name="generation",
    max_retries=10,
    retry_when=_cleanup_persistence_retry_when,
    min_backoff=60_000,
    max_backoff=3_600_000,
    time_limit=300_000,
)
def cleanup_service_translation_artifacts(service_order_id: int, outbox_event_id: int) -> None:
    _run_service_translation_cleanup_actor(service_order_id, outbox_event_id)


@dramatiq.actor(
    queue_name="generation",
    max_retries=10,
    retry_when=_cleanup_persistence_retry_when,
    min_backoff=60_000,
    max_backoff=3_600_000,
    time_limit=300_000,
)
def cleanup_service_document_artifacts(service_order_id: int, outbox_event_id: int) -> None:
    _run_service_document_cleanup_actor(service_order_id, outbox_event_id)


@dramatiq.actor(queue_name="health", max_retries=0, time_limit=10_000)
def worker_health() -> str:
    return "ok"
