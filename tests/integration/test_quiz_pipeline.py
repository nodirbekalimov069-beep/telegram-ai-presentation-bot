import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import APITimeoutError
from docx import Document
from pypdf import PdfWriter
from reportlab.pdfgen import canvas
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import core.database.models  # noqa: F401
from core.database.base import Base
from core.outbox.models import OutboxEvent
from modules.ai.providers.mock import MockAIProvider
from modules.ai.providers.errors import ProviderCallBudgetExhausted
from modules.files.storage import LocalStorage
from modules.service_orders.essay_pipeline import (
    EssayLeaseBusy,
    EssayRetryableError,
    EssayTerminalError,
    QuizPipeline,
    mark_terminal_quiz_failure,
)
from modules.service_orders.models import ServiceOrder
from modules.service_orders.service import ServiceOrderService, ServiceOrderStatus
from modules.users.service import UserService


@pytest.fixture
async def quiz_db(tmp_path: Path) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'quiz.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await UserService().register(
            session,
            telegram_user_id=1771,
            username="quiz-owner",
            first_name="Owner",
            last_name=None,
            language_code="uz",
        )
    yield factory
    await engine.dispose()


async def _create_order(factory: async_sessionmaker[AsyncSession]) -> ServiceOrder:
    async with factory() as session:
        return await ServiceOrderService().create_quiz_order(
            session,
            telegram_user_id=1771,
            request_payload={
                "topic": "Algoritmik fikrlash",
                "question_count": 4,
                "difficulty": "medium",
                "option_count": 4,
                "answer_key": True,
                "language": "uz",
                "requirements": "Amaliy vaziyatlarga e’tibor bering",
            },
            idempotency_key="quiz-pipeline-1",
        )


async def _content_pdf(docx: Path, output_dir: Path, **_: object) -> Path:
    pdf = output_dir / f"{docx.stem}.pdf"
    source = Document(str(docx))
    text = [paragraph.text for paragraph in source.paragraphs if paragraph.text.strip()]
    output = canvas.Canvas(str(pdf))
    for page in range(2):
        for index, line in enumerate(text[page * 18 : (page + 1) * 18]):
            output.drawString(48, 790 - index * 38, line[:100])
        output.showPage()
    output.save()
    return pdf


async def _blank_pdf(docx: Path, output_dir: Path, **_: object) -> Path:
    pdf = output_dir / f"{docx.stem}.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with pdf.open("wb") as stream:
        writer.write(stream)
    return pdf


async def test_quiz_order_is_transactional_idempotent_and_emits_one_event(quiz_db) -> None:  # type: ignore[no-untyped-def]
    first = await _create_order(quiz_db)
    duplicate = await _create_order(quiz_db)
    async with quiz_db() as session:
        order_count = await session.scalar(select(func.count(ServiceOrder.id)))
        events = list(await session.scalars(select(OutboxEvent)))
    assert duplicate.id == first.id
    assert order_count == 1
    assert first.status == ServiceOrderStatus.QUEUED
    assert len(events) == 1
    assert events[0].event_type == "service.quiz.requested"


@pytest.mark.parametrize(
    ("progress", "idempotency_key"),
    [
        ({"progress_chat_id": 1771}, "quiz-partial-chat"),
        ({"progress_message_id": 9001}, "quiz-partial-message"),
        ({"progress_language": "uz"}, "quiz-partial-language"),
        (
            {
                "progress_chat_id": 1771,
                "progress_message_id": 9001,
                "progress_language": "de",
            },
            "quiz-invalid-language",
        ),
        (
            {
                "progress_chat_id": 0,
                "progress_message_id": 9001,
                "progress_language": "uz",
            },
            "quiz-invalid-chat",
        ),
        (
            {
                "progress_chat_id": 1771,
                "progress_message_id": 0,
                "progress_language": "uz",
            },
            "quiz-invalid-message",
        ),
    ],
)
async def test_partial_or_invalid_quiz_progress_is_rejected_before_durable_state(
    quiz_db: async_sessionmaker[AsyncSession],
    progress: dict[str, object],
    idempotency_key: str,
) -> None:
    async with quiz_db() as session:
        with pytest.raises(ValueError, match="invalid QUIZ progress destination"):
            await ServiceOrderService().create_quiz_order(
                session,
                telegram_user_id=1771,
                request_payload={
                    "topic": "Algoritmik fikrlash",
                    "question_count": 4,
                    "difficulty": "medium",
                    "option_count": 4,
                    "answer_key": True,
                    "language": "uz",
                    "requirements": "Amaliy vaziyatlarga e’tibor bering",
                },
                idempotency_key=idempotency_key,
                **progress,  # type: ignore[arg-type]
            )

    async with quiz_db() as session:
        order_count = await session.scalar(select(func.count(ServiceOrder.id)))
        outbox_count = await session.scalar(select(func.count(OutboxEvent.id)))
    assert order_count == 0
    assert outbox_count == 0


async def test_quiz_pipeline_generates_once_and_replay_preserves_artifacts(
    quiz_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    order = await _create_order(quiz_db)
    provider = MockAIProvider()
    provider.generate_quiz = AsyncMock(wraps=provider.generate_quiz)  # type: ignore[method-assign]
    storage = LocalStorage(tmp_path / "storage")
    pipeline = QuizPipeline(
        quiz_db,
        provider,
        storage,
        temp_root=tmp_path / "work",
        pdf_converter=_content_pdf,
    )

    await pipeline.process(order.id)
    async with quiz_db() as session:
        first = await session.get(ServiceOrder, order.id)
        assert first is not None
        checkpoint = (
            first.status,
            first.generation_attempts,
            first.docx_storage_key,
            first.pdf_storage_key,
        )
    await pipeline.process(order.id)
    async with quiz_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert checkpoint == (
        saved.status,
        saved.generation_attempts,
        saved.docx_storage_key,
        saved.pdf_storage_key,
    )
    assert saved.status == ServiceOrderStatus.ARTIFACTS_READY
    assert saved.generation_attempts == 1
    assert saved.generation_metadata["question_count"] == 4
    assert saved.docx_storage_key is not None and saved.docx_storage_key.endswith(
        f"/quiz_{order.id}.docx"
    )
    assert saved.pdf_storage_key is not None and saved.pdf_storage_key.endswith(
        f"/quiz_{order.id}.pdf"
    )
    assert provider.generate_quiz.await_count == 1  # type: ignore[attr-defined]


async def test_quiz_pipeline_rejects_blank_pdf_fail_closed(quiz_db, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    order = await _create_order(quiz_db)
    pipeline = QuizPipeline(
        quiz_db,
        MockAIProvider(),
        LocalStorage(tmp_path / "storage"),
        temp_root=tmp_path / "work",
        pdf_converter=_blank_pdf,
    )
    with pytest.raises(EssayTerminalError, match="PDF conversion or validation"):
        await pipeline.process(order.id)
    async with quiz_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.status == ServiceOrderStatus.FAILED
    assert saved.docx_storage_key is None
    assert saved.pdf_storage_key is None


async def test_quiz_timeout_failure_has_no_partial_artifact_or_delivery(
    quiz_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    order = await _create_order(quiz_db)
    provider = MockAIProvider()
    provider.generate_quiz = AsyncMock(
        side_effect=APITimeoutError(
            request=httpx.Request("POST", "https://example.test/v1/chat/completions")
        )
    )  # type: ignore[method-assign]
    pipeline = QuizPipeline(
        quiz_db,
        provider,
        LocalStorage(tmp_path / "storage"),
        temp_root=tmp_path / "work",
        pdf_converter=_content_pdf,
    )

    with pytest.raises(EssayRetryableError, match="AI generation failed"):
        await pipeline.process(order.id)
    assert await mark_terminal_quiz_failure(quiz_db, order_id=order.id) is True
    async with quiz_db() as session:
        saved = await session.get(ServiceOrder, order.id)

    assert saved is not None
    assert saved.status == ServiceOrderStatus.FAILED
    assert saved.error_code == "GENERATION_RETRIES_EXHAUSTED"
    assert saved.docx_storage_key is None
    assert saved.pdf_storage_key is None
    assert saved.delivery_status == "PENDING"
    assert saved.delivery_attempts == 0


async def test_non_openai_provider_budget_exhaustion_is_terminal_without_output(
    quiz_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    order = await _create_order(quiz_db)
    provider = MockAIProvider()
    provider.generate_quiz = AsyncMock(
        side_effect=ProviderCallBudgetExhausted("fake provider exhausted")
    )  # type: ignore[method-assign]
    pipeline = QuizPipeline(
        quiz_db,
        provider,
        LocalStorage(tmp_path / "storage"),
        temp_root=tmp_path / "work",
        pdf_converter=_content_pdf,
    )

    with pytest.raises(EssayTerminalError, match="provider budget exhausted"):
        await pipeline.process(order.id)
    async with quiz_db() as session:
        saved = await session.get(ServiceOrder, order.id)

    assert saved is not None
    assert saved.status == ServiceOrderStatus.FAILED
    assert saved.error_code == "AI_PROVIDER_BUDGET_EXHAUSTED"
    assert saved.retry_count == 1
    assert saved.docx_storage_key is None
    assert saved.pdf_storage_key is None
    assert saved.result_metadata == {}
    assert saved.delivery_status == "PENDING"
    assert saved.delivery_attempts == 0
    assert saved.docx_telegram_message_id is None
    assert saved.pdf_telegram_message_id is None


async def test_ordinary_provider_failure_remains_retryable_not_budget_exhaustion(
    quiz_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    order = await _create_order(quiz_db)
    provider = MockAIProvider()
    provider.generate_quiz = AsyncMock(side_effect=ConnectionError("temporary outage"))  # type: ignore[method-assign]
    pipeline = QuizPipeline(
        quiz_db,
        provider,
        LocalStorage(tmp_path / "storage"),
        temp_root=tmp_path / "work",
        pdf_converter=_content_pdf,
    )

    with pytest.raises(EssayRetryableError, match="AI generation failed"):
        await pipeline.process(order.id)
    async with quiz_db() as session:
        saved = await session.get(ServiceOrder, order.id)

    assert saved is not None
    assert saved.status == ServiceOrderStatus.QUEUED
    assert saved.error_code == "AI_GENERATION_FAILED"
    assert saved.retry_count == 1
    assert saved.docx_storage_key is None
    assert saved.pdf_storage_key is None
    assert saved.delivery_attempts == 0


async def test_quiz_pipeline_rejects_parallel_generation_lease(quiz_db, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    order = await _create_order(quiz_db)
    entered = asyncio.Event()
    release = asyncio.Event()
    provider = MockAIProvider()
    original = provider.generate_quiz

    async def slow_generate(request):  # type: ignore[no-untyped-def]
        entered.set()
        await release.wait()
        return await original(request)

    provider.generate_quiz = slow_generate  # type: ignore[method-assign]
    pipeline = QuizPipeline(
        quiz_db,
        provider,
        LocalStorage(tmp_path / "storage"),
        temp_root=tmp_path / "work",
        pdf_converter=_content_pdf,
    )
    first = asyncio.create_task(pipeline.process(order.id))
    await entered.wait()
    with pytest.raises(EssayLeaseBusy):
        await pipeline.process(order.id)
    release.set()
    await first
