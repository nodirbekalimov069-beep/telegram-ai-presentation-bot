import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from docx import Document
from pypdf import PdfWriter
from reportlab.pdfgen import canvas
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import core.database.models  # noqa: F401
from core.database.base import Base
from core.outbox.models import OutboxEvent
from modules.ai.providers.mock import MockAIProvider
from modules.files.storage import LocalStorage
from modules.service_orders.essay_pipeline import (
    EssayLeaseBusy,
    EssayTerminalError,
    SummaryPipeline,
    cleanup_summary_orphan_artifacts,
)
from modules.service_orders.models import ServiceOrder
from modules.service_orders.service import ServiceOrderService, ServiceOrderStatus
from modules.users.service import UserService


@pytest.fixture
async def summary_db(tmp_path: Path) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'summary.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await UserService().register(
            session,
            telegram_user_id=1661,
            username="summary-owner",
            first_name="Owner",
            last_name=None,
            language_code="uz",
        )
    yield factory
    await engine.dispose()


async def _create_order(factory: async_sessionmaker[AsyncSession]) -> ServiceOrder:
    async with factory() as session:
        return await ServiceOrderService().create_summary_order(
            session,
            telegram_user_id=1661,
            request_payload={
                "source": (
                    "Algoritmik fikrlash vazifani qismlarga ajratadi. Inson nazorati "
                    "yechimni tekshiradi va amaliy xavfni kamaytiradi."
                ),
                "detail": "short",
                "language": "uz",
                "requirements": "Inson nazorati rolini alohida tushuntiring.",
            },
            idempotency_key="summary-pipeline-1",
        )


async def _content_pdf(docx: Path, output_dir: Path, **_: object) -> Path:
    pdf = output_dir / f"{docx.stem}.pdf"
    source_document = Document(str(docx))
    title = next(
        paragraph.text
        for paragraph in source_document.paragraphs
        if paragraph.style is not None and paragraph.style.name == "Title"
    )
    output = canvas.Canvas(str(pdf))
    for _page in range(2):
        output.drawString(72, 760, title)
        for line in range(10):
            output.drawString(
                72,
                730 - line * 24,
                "Algoritmik fikrlash konspekti amaliy tahlil va inson nazoratini boglaydi.",
            )
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


async def _short_pdf(docx: Path, output_dir: Path, **_: object) -> Path:
    pdf = output_dir / f"{docx.stem}.pdf"
    output = canvas.Canvas(str(pdf))
    output.drawString(72, 760, "Algoritmik fikrlash")
    output.drawString(72, 730, "Juda qisqa konspekt")
    output.save()
    return pdf


async def _missing_title_pdf(docx: Path, output_dir: Path, **_: object) -> Path:
    pdf = output_dir / f"{docx.stem}.pdf"
    output = canvas.Canvas(str(pdf))
    for _page in range(2):
        for line in range(10):
            output.drawString(72, 730 - line * 24, "Mazmun bor, lekin expected title yoq.")
        output.showPage()
    output.save()
    return pdf


async def test_summary_order_is_transactional_idempotent_and_emits_one_event(summary_db) -> None:  # type: ignore[no-untyped-def]
    first = await _create_order(summary_db)
    duplicate = await _create_order(summary_db)
    async with summary_db() as session:
        order_count = await session.scalar(select(func.count(ServiceOrder.id)))
        events = list(await session.scalars(select(OutboxEvent)))
    assert duplicate.id == first.id
    assert order_count == 1
    assert first.status == ServiceOrderStatus.QUEUED
    assert len(events) == 1
    assert events[0].event_type == "service.summary.requested"
    assert events[0].payload == {"service_order_id": first.id}


async def test_summary_pipeline_generates_once_with_owner_scoped_safe_artifacts(
    summary_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    order = await _create_order(summary_db)
    provider = MockAIProvider()
    provider.generate_summary = AsyncMock(wraps=provider.generate_summary)  # type: ignore[method-assign]
    storage = LocalStorage(tmp_path / "storage")
    pipeline = SummaryPipeline(
        summary_db,
        provider,
        storage,
        temp_root=tmp_path / "work",
        pdf_converter=_content_pdf,
    )

    await pipeline.process(order.id)
    await pipeline.process(order.id)

    async with summary_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.status == ServiceOrderStatus.ARTIFACTS_READY
    assert saved.generation_attempts == 1
    assert saved.generation_metadata["section_count"] >= 2
    assert saved.docx_storage_key is not None
    assert saved.docx_storage_key.startswith(f"users/{order.user_id}/service-orders/{order.id}/")
    assert saved.docx_storage_key.endswith(f"/konspekt_{order.id}.docx")
    assert saved.pdf_storage_key is not None
    assert saved.pdf_storage_key.endswith(f"/konspekt_{order.id}.pdf")
    request = provider.generate_summary.await_args.args[0]  # type: ignore[attr-defined]
    assert "Algoritmik fikrlash" in request.source
    assert "Inson nazorati" in request.additional_requirements
    assert provider.generate_summary.await_count == 1  # type: ignore[attr-defined]


@pytest.mark.parametrize("invalid_converter", [_blank_pdf, _short_pdf, _missing_title_pdf])
async def test_summary_pipeline_rejects_blank_short_or_titleless_pdf_without_storing_it(
    summary_db, tmp_path: Path, invalid_converter
) -> None:  # type: ignore[no-untyped-def]
    order = await _create_order(summary_db)
    pipeline = SummaryPipeline(
        summary_db,
        MockAIProvider(),
        LocalStorage(tmp_path / "storage"),
        temp_root=tmp_path / "work",
        pdf_converter=invalid_converter,
    )
    with pytest.raises(EssayTerminalError, match="PDF conversion or validation"):
        await pipeline.process(order.id)
    async with summary_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.status == ServiceOrderStatus.FAILED
    assert saved.error_code == "PDF_CONVERSION_FAILED"
    assert saved.docx_storage_key is None
    assert saved.pdf_storage_key is None


async def test_summary_pipeline_rejects_parallel_generation_lease(
    summary_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    order = await _create_order(summary_db)
    entered = asyncio.Event()
    release = asyncio.Event()
    provider = MockAIProvider()
    original = provider.generate_summary

    async def slow_generate(request):  # type: ignore[no-untyped-def]
        entered.set()
        await release.wait()
        return await original(request)

    provider.generate_summary = slow_generate  # type: ignore[method-assign]
    pipeline = SummaryPipeline(
        summary_db,
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


async def test_summary_cleanup_never_deletes_canonical_winner(summary_db, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    storage = LocalStorage(tmp_path / "storage")
    order = await _create_order(summary_db)
    pipeline = SummaryPipeline(
        summary_db,
        MockAIProvider(),
        storage,
        temp_root=tmp_path / "work",
        pdf_converter=_content_pdf,
    )
    await pipeline.process(order.id)
    orphan_source = tmp_path / "orphan.docx"
    orphan_source.write_bytes(b"PK\x03\x04-orphan")
    orphan_key = await storage.put_service_file(
        orphan_source,
        user_id=order.user_id,
        service_order_id=order.id,
        artifact_type="docx",
        attempt_token="e" * 32,
        service_type="summary",
    )
    async with summary_db() as session, session.begin():
        saved = await session.get(ServiceOrder, order.id, with_for_update=True)
        assert saved is not None and saved.docx_storage_key is not None
        winner = saved.docx_storage_key
        saved.generation_metadata = {
            **saved.generation_metadata,
            "orphan_candidate_keys": [winner, orphan_key],
        }

    await cleanup_summary_orphan_artifacts(summary_db, storage, order_id=order.id)
    assert await storage.exists(winner)
    assert not await storage.exists(orphan_key)
