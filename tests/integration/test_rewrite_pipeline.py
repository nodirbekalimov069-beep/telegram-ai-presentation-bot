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
from modules.files.storage import LocalStorage
from modules.service_orders.essay_pipeline import (
    EssayLeaseBusy,
    EssayTerminalError,
    RewritePipeline,
)
from modules.service_orders.models import ServiceOrder
from modules.service_orders.service import ServiceOrderService, ServiceOrderStatus
from modules.users.service import UserService


@pytest.fixture
async def rewrite_db(tmp_path: Path) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rewrite.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await UserService().register(
            session,
            telegram_user_id=2881,
            username="rewrite-owner",
            first_name="Owner",
            last_name=None,
            language_code="uz",
        )
    yield factory
    await engine.dispose()


async def _create_order(factory: async_sessionmaker[AsyncSession]) -> ServiceOrder:
    async with factory() as session:
        return await ServiceOrderService().create_rewrite_order(
            session,
            telegram_user_id=2881,
            request_payload={
                "text": (
                    "Jamoa vazifalarni umumiy reja asosida muvofiqlashtiradi va har bir natijani "
                    "yetkazishdan oldin sinchiklab tekshiradi."
                ),
                "mode": "formal",
            },
            idempotency_key="rewrite-pipeline-one",
        )


async def _content_pdf(docx: Path, output_dir: Path, **_: object) -> Path:
    pdf = output_dir / f"{docx.stem}.pdf"
    lines = [paragraph.text for paragraph in Document(docx).paragraphs if paragraph.text.strip()]
    output = canvas.Canvas(str(pdf))
    for index, line in enumerate(lines):
        output.drawString(42, 790 - (index % 22) * 32, line[:100])
    output.showPage()
    output.save()
    return pdf


async def test_rewrite_unsupported_source_has_zero_order_and_outbox_side_effects(
    rewrite_db,
) -> None:  # type: ignore[no-untyped-def]
    async with rewrite_db() as session:
        with pytest.raises(ValueError, match="language"):
            await ServiceOrderService().create_rewrite_order(
                session,
                telegram_user_id=2881,
                request_payload={
                    "text": (
                        "The and this result. El proyecto explica la información para los usuarios, "
                        "pero cambia el resultado con una descripción clara."
                    ),
                    "mode": "formal",
                },
                idempotency_key="rewrite-unsafe-spanish",
            )
    async with rewrite_db() as session:
        assert await session.scalar(select(func.count(ServiceOrder.id))) == 0
        assert await session.scalar(select(func.count(OutboxEvent.id))) == 0


async def test_rewrite_order_is_transactional_idempotent_and_emits_one_event(
    rewrite_db,
) -> None:  # type: ignore[no-untyped-def]
    first = await _create_order(rewrite_db)
    duplicate = await _create_order(rewrite_db)
    async with rewrite_db() as session:
        assert await session.scalar(select(func.count(ServiceOrder.id))) == 1
        events = list(await session.scalars(select(OutboxEvent)))
    assert duplicate.id == first.id
    assert first.status == ServiceOrderStatus.QUEUED
    assert [event.event_type for event in events] == ["service.rewrite.requested"]


async def test_rewrite_concurrent_confirm_creates_one_order_and_one_outbox(rewrite_db) -> None:  # type: ignore[no-untyped-def]
    payload = {
        "text": "Bu jamoa uchun umumiy reja va aniq natijani tushuntiradigan yetarli matn.",
        "mode": "formal",
    }

    async def confirm() -> int:
        async with rewrite_db() as session:
            order = await ServiceOrderService().create_rewrite_order(
                session,
                telegram_user_id=2881,
                request_payload=payload,
                idempotency_key="rewrite-concurrent-001",
            )
            return order.id

    first_id, second_id = await asyncio.gather(confirm(), confirm())
    assert first_id == second_id
    async with rewrite_db() as session:
        assert await session.scalar(select(func.count(ServiceOrder.id))) == 1
        assert await session.scalar(select(func.count(OutboxEvent.id))) == 1


async def test_rewrite_pipeline_generates_once_and_replay_preserves_artifacts(
    rewrite_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    order = await _create_order(rewrite_db)
    provider = MockAIProvider()
    provider.generate_rewrite = AsyncMock(wraps=provider.generate_rewrite)  # type: ignore[method-assign]
    pipeline = RewritePipeline(
        rewrite_db,
        provider,
        LocalStorage(tmp_path / "storage"),
        temp_root=tmp_path / "work",
        pdf_converter=_content_pdf,
    )
    await pipeline.process(order.id)
    async with rewrite_db() as session:
        first = await session.get(ServiceOrder, order.id)
        assert first is not None
        checkpoint = (
            first.status,
            first.generation_attempts,
            first.docx_storage_key,
            first.pdf_storage_key,
        )
    await pipeline.process(order.id)
    async with rewrite_db() as session:
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
    assert saved.generation_metadata["segment_count"] == 1
    assert saved.generation_metadata["reference_count"] == 0
    assert saved.docx_storage_key and saved.docx_storage_key.endswith(
        f"/qayta_yozish_{order.id}.docx"
    )
    assert saved.pdf_storage_key and saved.pdf_storage_key.endswith(f"/qayta_yozish_{order.id}.pdf")
    assert provider.generate_rewrite.await_count == 1  # type: ignore[attr-defined]


async def test_rewrite_active_lease_blocks_duplicate_generation(rewrite_db, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    order = await _create_order(rewrite_db)
    async with rewrite_db() as session, session.begin():
        saved = await session.get(ServiceOrder, order.id, with_for_update=True)
        assert saved is not None
        saved.status = ServiceOrderStatus.PROCESSING
        saved.generation_attempts = 1
        saved.generation_lease_token = "active-worker"
        saved.generation_lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)

    provider = MockAIProvider()
    provider.generate_rewrite = AsyncMock(wraps=provider.generate_rewrite)  # type: ignore[method-assign]
    pipeline = RewritePipeline(
        rewrite_db,
        provider,
        LocalStorage(tmp_path / "storage-lease"),
        temp_root=tmp_path / "work-lease",
        pdf_converter=_content_pdf,
    )
    with pytest.raises(EssayLeaseBusy):
        await pipeline.process(order.id)
    provider.generate_rewrite.assert_not_awaited()  # type: ignore[attr-defined]
    async with rewrite_db() as session:
        blocked = await session.get(ServiceOrder, order.id)
    assert blocked is not None
    assert blocked.status == ServiceOrderStatus.PROCESSING
    assert blocked.generation_attempts == 1


async def test_rewrite_contract_rejection_is_terminal_without_generation_retry(
    rewrite_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    order = await _create_order(rewrite_db)
    provider = MockAIProvider()
    provider.generate_rewrite = AsyncMock(  # type: ignore[method-assign]
        side_effect=AIContractError("strict request contract rejected output")
    )
    pipeline = RewritePipeline(
        rewrite_db,
        provider,
        LocalStorage(tmp_path / "storage-contract"),
        temp_root=tmp_path / "work-contract",
        pdf_converter=_content_pdf,
    )
    with pytest.raises(EssayTerminalError):
        await pipeline.process(order.id)
    with pytest.raises(EssayTerminalError):
        await pipeline.process(order.id)
    provider.generate_rewrite.assert_awaited_once()  # type: ignore[attr-defined]
    async with rewrite_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.status == ServiceOrderStatus.FAILED
    assert saved.generation_attempts == 1
    assert saved.error_code == "AI_CONTRACT_REJECTED"


async def test_rewrite_provider_exception_after_internal_retries_is_terminal(
    rewrite_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    order = await _create_order(rewrite_db)
    provider = MockAIProvider()
    provider.generate_rewrite = AsyncMock(  # type: ignore[method-assign]
        side_effect=ConnectionError("provider transport retries exhausted")
    )
    pipeline = RewritePipeline(
        rewrite_db,
        provider,
        LocalStorage(tmp_path / "storage-provider"),
        temp_root=tmp_path / "work-provider",
        pdf_converter=_content_pdf,
    )
    with pytest.raises(EssayTerminalError):
        await pipeline.process(order.id)
    provider.generate_rewrite.assert_awaited_once()  # type: ignore[attr-defined]
    async with rewrite_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.status == ServiceOrderStatus.FAILED
    assert saved.generation_attempts == 1
    assert saved.error_code == "AI_PROVIDER_FAILED"
