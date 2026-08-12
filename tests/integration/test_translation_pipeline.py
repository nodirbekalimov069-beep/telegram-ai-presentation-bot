from pathlib import Path
from unittest.mock import AsyncMock
import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from docx import Document
from reportlab.pdfgen import canvas
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import core.database.models  # noqa: F401
from core.database.base import Base
from core.outbox.models import OutboxEvent
from modules.ai.contracts import AIContractError
from modules.ai.providers.mock import MockAIProvider
from modules.ai.providers.openai_compatible import OpenAICompatibleProvider
from modules.ai.translation_contracts import TranslationSegment, TranslationSelection
from modules.files.storage import LocalStorage
from modules.service_orders.essay_pipeline import (
    EssayLeaseBusy,
    EssayTerminalError,
    TranslationPipeline,
)
from modules.service_orders.models import ServiceOrder
from modules.service_orders.service import ServiceOrderService, ServiceOrderStatus
from modules.users.service import UserService


@pytest.fixture
async def translation_db(tmp_path: Path):  # type: ignore[no-untyped-def]
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'translation.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await UserService().register(
            session,
            telegram_user_id=7811,
            username="translation-owner",
            first_name="Owner",
            last_name=None,
            language_code="uz",
        )
    yield factory
    await engine.dispose()


async def _pdf(docx: Path, output_dir: Path, **_: object) -> Path:
    target = output_dir / f"{docx.stem}.pdf"
    pdf = canvas.Canvas(str(target))
    for index, paragraph in enumerate(Document(docx).paragraphs):
        pdf.drawString(30, 790 - index * 20, paragraph.text[:100])
    pdf.drawString(30, 700, "Translation document with meaningful validated content and structure.")
    pdf.showPage()
    pdf.save()
    return target


async def _create(factory: async_sessionmaker[AsyncSession]) -> ServiceOrder:
    async with factory() as session:
        return await ServiceOrderService().create_translation_order(
            session,
            telegram_user_id=7811,
            request_payload={
                "text": "Ali 2026-08-12 gacha 25 USD to‘lashi shart.",
                "source_language": "uz",
                "target_language": "en",
            },
            idempotency_key="translation-pipeline-001",
        )


async def test_translation_order_is_transactional_idempotent_and_document_stays_closed(
    translation_db,
) -> None:  # type: ignore[no-untyped-def]
    first = await _create(translation_db)
    duplicate = await _create(translation_db)
    assert first.id == duplicate.id
    async with translation_db() as session:
        assert await session.scalar(select(func.count(ServiceOrder.id))) == 1
        events = list(await session.scalars(select(OutboxEvent)))
        with pytest.raises(PermissionError, match="closed"):
            await ServiceOrderService().create_preview_order(
                session,
                telegram_user_id=7811,
                service_type="document",
                request_payload={"document_type": "other", "details": "Safe details"},
                idempotency_key="document-closed-001",
            )
    assert [event.event_type for event in events] == ["service.translate.requested"]


async def test_translation_pipeline_generates_once_and_replay_is_unchanged(
    translation_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    order = await _create(translation_db)
    provider = MockAIProvider()
    provider.generate_translation = AsyncMock(wraps=provider.generate_translation)  # type: ignore[method-assign]
    pipeline = TranslationPipeline(
        translation_db,
        provider,
        LocalStorage(tmp_path / "storage"),
        temp_root=tmp_path / "work",
        pdf_converter=_pdf,
    )
    await pipeline.process(order.id)
    await pipeline.process(order.id)
    async with translation_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.status == ServiceOrderStatus.ARTIFACTS_READY
    assert saved.generation_attempts == 1
    assert saved.generation_metadata["segment_count"] == 1
    assert saved.generation_metadata["reference_count"] == 0
    assert saved.generation_metadata["protected_association_score"] == 1.0
    assert saved.generation_metadata["canonical_parity_score"] == 1.0
    assert saved.docx_storage_key and saved.docx_storage_key.endswith(f"/tarjima_{order.id}.docx")
    assert saved.pdf_storage_key and saved.pdf_storage_key.endswith(f"/tarjima_{order.id}.pdf")
    provider.generate_translation.assert_awaited_once()  # type: ignore[attr-defined]


async def test_translation_concurrent_confirm_creates_one_order_and_one_outbox(
    translation_db,
) -> None:  # type: ignore[no-untyped-def]
    first, second = await asyncio.gather(_create(translation_db), _create(translation_db))
    assert first.id == second.id
    async with translation_db() as session:
        assert await session.scalar(select(func.count(ServiceOrder.id))) == 1
        assert await session.scalar(select(func.count(OutboxEvent.id))) == 1


async def test_translation_active_lease_blocks_duplicate_generation(
    translation_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    order = await _create(translation_db)
    async with translation_db() as session, session.begin():
        saved = await session.get(ServiceOrder, order.id, with_for_update=True)
        assert saved is not None
        saved.status = ServiceOrderStatus.PROCESSING
        saved.generation_attempts = 1
        saved.generation_lease_token = "active-worker"
        saved.generation_lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    provider = MockAIProvider()
    provider.generate_translation = AsyncMock(wraps=provider.generate_translation)  # type: ignore[method-assign]
    pipeline = TranslationPipeline(
        translation_db,
        provider,
        LocalStorage(tmp_path / "storage"),
        temp_root=tmp_path / "work",
        pdf_converter=_pdf,
    )
    with pytest.raises(EssayLeaseBusy):
        await pipeline.process(order.id)
    provider.generate_translation.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("failure", "error_code"),
    [
        (AIContractError("strict contract rejected"), "AI_CONTRACT_REJECTED"),
        (ConnectionError("transport retries exhausted"), "AI_PROVIDER_FAILED"),
    ],
    ids=("invalid-ai-contract", "bounded-provider-transport"),
)
async def test_translation_generation_failures_are_terminal_without_artifacts_or_delivery(
    translation_db, tmp_path: Path, failure: Exception, error_code: str
) -> None:  # type: ignore[no-untyped-def]
    order = await _create(translation_db)
    provider = MockAIProvider()
    provider.generate_translation = AsyncMock(side_effect=failure)  # type: ignore[method-assign]
    storage = LocalStorage(tmp_path / "storage")
    converter = AsyncMock(side_effect=AssertionError("PDF conversion must not run"))
    pipeline = TranslationPipeline(
        translation_db,
        provider,
        storage,
        temp_root=tmp_path / "work",
        pdf_converter=converter,
    )
    with pytest.raises(EssayTerminalError):
        await pipeline.process(order.id)
    with pytest.raises(EssayTerminalError):
        await pipeline.process(order.id)
    provider.generate_translation.assert_awaited_once()  # type: ignore[attr-defined]
    converter.assert_not_awaited()
    async with translation_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.status == ServiceOrderStatus.FAILED
    assert saved.error_code == error_code
    assert saved.generation_attempts == 1
    assert saved.docx_storage_key is None and saved.pdf_storage_key is None


async def test_buy_sell_contradiction_never_reaches_artifact_or_delivery_path(
    translation_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    async with translation_db() as session:
        order = await ServiceOrderService().create_translation_order(
            session,
            telegram_user_id=7811,
            request_payload={
                "source_language": "uz",
                "text": "Ali Vali uchun avtomobil sotib oldi.",
                "target_language": "en",
            },
            idempotency_key="translation-buy-sell-adversarial",
        )
    provider = OpenAICompatibleProvider(
        base_url="https://example.test/v1", api_key="test-key", model="test", max_retries=0
    )
    forged_candidate = TranslationSelection(
        source_language="uz",
        target_language="en",
        segments=[
            TranslationSegment(
                source_index=0,
                kind="paragraph",
                text="Ali sold a vehicle for Vali.",
            )
        ],
    )
    provider._request_structured = AsyncMock(return_value=forged_candidate)  # type: ignore[method-assign]
    converter = AsyncMock(side_effect=AssertionError("artifact conversion must not run"))
    pipeline = TranslationPipeline(
        translation_db,
        provider,
        LocalStorage(tmp_path / "storage"),
        temp_root=tmp_path / "work",
        pdf_converter=converter,
    )

    with pytest.raises(EssayTerminalError):
        await pipeline.process(order.id)

    converter.assert_not_awaited()
    async with translation_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.status == ServiceOrderStatus.FAILED
    assert saved.error_code == "AI_CONTRACT_REJECTED"
    assert saved.docx_storage_key is None and saved.pdf_storage_key is None
    assert saved.docx_telegram_message_id is None and saved.pdf_telegram_message_id is None
    await provider.aclose()
