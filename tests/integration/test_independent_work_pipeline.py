import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pypdf import PdfWriter
from reportlab.pdfgen import canvas
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import core.database.models  # noqa: F401
from core.database.base import Base
from core.outbox.models import OutboxEvent
from modules.ai.providers.mock import MockAIProvider
from modules.files.storage import LocalStorage
from modules.service_orders.essay_pipeline import (
    EssayLeaseBusy,
    IndependentWorkPipeline,
    _schedule_orphan_cleanup,
    cleanup_independent_work_orphan_artifacts,
    mark_terminal_independent_work_failure,
)
from modules.service_orders.models import ServiceOrder
from modules.service_orders.service import ServiceOrderService, ServiceOrderStatus
from modules.users.service import UserService


@pytest.fixture
async def independent_db(tmp_path: Path) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'independent.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await UserService().register(
            session,
            telegram_user_id=889,
            username="independent-owner",
            first_name="Owner",
            last_name=None,
            language_code="uz",
        )
    yield factory
    await engine.dispose()


async def _create_order(factory: async_sessionmaker[AsyncSession]) -> ServiceOrder:
    async with factory() as session:
        return await ServiceOrderService().create_independent_work_order(
            session,
            telegram_user_id=889,
            request_payload={
                "subject": "Informatika",
                "topic": "Algoritmlarning amaliy ahamiyati",
                "language": "uz",
                "length": "short",
                "requirements": "Xulosada algoritmik fikrlash iborasini aynan ishlat.",
            },
            idempotency_key="independent-pipeline-1",
        )


async def _fake_pdf(docx: Path, output_dir: Path, **_: object) -> Path:
    pdf = output_dir / f"{docx.stem}.pdf"
    output = canvas.Canvas(str(pdf))
    output.drawString(72, 760, "Algoritmlarning amaliy ahamiyati")
    output.drawString(72, 730, "Informatika")
    for line in range(8):
        output.drawString(
            72,
            700 - line * 24,
            "Mazmunli mustaqil ish matni algoritmik fikrlashni amalda tushuntiradi.",
        )
    output.save()
    return pdf


async def _blank_pdf(docx: Path, output_dir: Path, **_: object) -> Path:
    pdf = output_dir / f"{docx.stem}.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with pdf.open("wb") as stream:
        writer.write(stream)
    return pdf


async def test_independent_pipeline_rejects_structurally_valid_blank_pdf(
    independent_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    order = await _create_order(independent_db)
    pipeline = IndependentWorkPipeline(
        independent_db,
        MockAIProvider(),
        LocalStorage(tmp_path / "storage"),
        temp_root=tmp_path / "work",
        pdf_converter=_blank_pdf,
    )

    await pipeline.process(order.id)

    async with independent_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.status == ServiceOrderStatus.ARTIFACTS_READY
    assert saved.docx_storage_key is not None
    assert saved.pdf_storage_key is None
    assert saved.pdf_error == "PDF_CONVERSION_FAILED"


async def test_independent_pipeline_generates_once_and_uses_safe_artifact_names(
    independent_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    order = await _create_order(independent_db)
    provider = MockAIProvider()
    provider.generate_independent_work = AsyncMock(  # type: ignore[method-assign]
        wraps=provider.generate_independent_work
    )
    storage = LocalStorage(tmp_path / "storage")
    pipeline = IndependentWorkPipeline(
        independent_db,
        provider,
        storage,
        temp_root=tmp_path / "work",
        pdf_converter=_fake_pdf,
    )

    await pipeline.process(order.id)
    await pipeline.process(order.id)

    async with independent_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.status == ServiceOrderStatus.ARTIFACTS_READY
    assert saved.generation_attempts == 1
    assert saved.docx_storage_key is not None
    assert saved.docx_storage_key.endswith(f"/mustaqil_ish_{order.id}.docx")
    assert saved.pdf_storage_key is not None
    assert saved.pdf_storage_key.endswith(f"/mustaqil_ish_{order.id}.pdf")
    assert saved.generation_metadata["section_count"] >= 2
    generated_request = provider.generate_independent_work.await_args.args[0]  # type: ignore[attr-defined]
    assert generated_request.subject == "Informatika"
    assert "algoritmik fikrlash" in generated_request.additional_requirements
    assert provider.generate_independent_work.await_count == 1  # type: ignore[attr-defined]


async def test_independent_pipeline_rejects_parallel_live_lease(
    independent_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    order = await _create_order(independent_db)
    entered = asyncio.Event()
    release = asyncio.Event()
    provider = MockAIProvider()
    original = provider.generate_independent_work

    async def slow_generate(request):  # type: ignore[no-untyped-def]
        entered.set()
        await release.wait()
        return await original(request)

    provider.generate_independent_work = slow_generate  # type: ignore[method-assign]
    pipeline = IndependentWorkPipeline(
        independent_db,
        provider,
        LocalStorage(tmp_path / "storage"),
        temp_root=tmp_path / "work",
        pdf_converter=_fake_pdf,
    )
    first = asyncio.create_task(pipeline.process(order.id))
    await entered.wait()
    with pytest.raises(EssayLeaseBusy):
        await pipeline.process(order.id)
    release.set()
    await first


async def test_independent_cleanup_never_deletes_canonical_winner(
    independent_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    storage = LocalStorage(tmp_path / "storage")
    order = await _create_order(independent_db)
    pipeline = IndependentWorkPipeline(
        independent_db,
        MockAIProvider(),
        storage,
        temp_root=tmp_path / "work",
        pdf_converter=_fake_pdf,
    )
    await pipeline.process(order.id)

    orphan_source = tmp_path / "orphan.docx"
    orphan_source.write_bytes(b"PK\x03\x04-orphan")
    orphan_key = await storage.put_service_file(
        orphan_source,
        user_id=order.user_id,
        service_order_id=order.id,
        artifact_type="docx",
        attempt_token="f" * 32,
        service_type="independent",
    )
    async with independent_db() as session, session.begin():
        saved = await session.get(ServiceOrder, order.id, with_for_update=True)
        assert saved is not None and saved.docx_storage_key is not None
        winner_key = saved.docx_storage_key
        saved.generation_metadata = {
            **saved.generation_metadata,
            "orphan_candidate_keys": [winner_key, orphan_key],
        }

    await cleanup_independent_work_orphan_artifacts(independent_db, storage, order_id=order.id)

    assert await storage.exists(winner_key)
    assert not await storage.exists(orphan_key)
    async with independent_db() as session:
        cleaned = await session.get(ServiceOrder, order.id)
    assert cleaned is not None
    assert "orphan_candidate_keys" not in cleaned.generation_metadata


async def test_terminal_exhaustion_promotes_pending_keys_to_durable_cleanup(
    independent_db,
) -> None:  # type: ignore[no-untyped-def]
    order = await _create_order(independent_db)
    pending_key = (
        f"users/{order.user_id}/service-orders/{order.id}/attempts/"
        f"{'a' * 32}/mustaqil_ish_{order.id}.docx"
    )
    async with independent_db() as session, session.begin():
        saved = await session.get(ServiceOrder, order.id, with_for_update=True)
        assert saved is not None
        saved.generation_metadata = {"pending_artifact_keys": [pending_key]}

    assert await mark_terminal_independent_work_failure(independent_db, order_id=order.id)

    async with independent_db() as session:
        saved = await session.get(ServiceOrder, order.id)
        event = await session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "service.independent.cleanup_requested"
            )
        )
    assert saved is not None
    assert saved.status == ServiceOrderStatus.FAILED
    assert "pending_artifact_keys" not in saved.generation_metadata
    assert saved.generation_metadata["orphan_candidate_keys"] == [pending_key]
    assert event is not None and event.status == "PENDING"


async def test_cleanup_event_is_reopened_after_previous_publication(
    independent_db,
) -> None:  # type: ignore[no-untyped-def]
    order = await _create_order(independent_db)
    first_available_at = datetime.now(UTC) + timedelta(hours=1)
    async with independent_db() as session, session.begin():
        saved = await session.get(ServiceOrder, order.id, with_for_update=True)
        assert saved is not None
        saved.generation_metadata = {"orphan_candidate_keys": ["first-key"]}
        await _schedule_orphan_cleanup(session, saved, available_at=first_available_at)
    async with independent_db() as session, session.begin():
        event = await session.scalar(
            select(OutboxEvent)
            .where(OutboxEvent.event_type == "service.independent.cleanup_requested")
            .with_for_update()
        )
        assert event is not None
        event_id = event.id
        event.status = "PUBLISHED"
        event.attempts = 9
        event.published_at = datetime.now(UTC)

    second_available_at = datetime.now(UTC) + timedelta(hours=2)
    async with independent_db() as session, session.begin():
        saved = await session.get(ServiceOrder, order.id, with_for_update=True)
        assert saved is not None
        saved.generation_metadata = {"orphan_candidate_keys": ["second-key"]}
        await _schedule_orphan_cleanup(session, saved, available_at=second_available_at)

    async with independent_db() as session:
        events = list(
            await session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == "service.independent.cleanup_requested"
                )
            )
        )
    assert len(events) == 1
    assert events[0].id == event_id
    assert events[0].status == "PENDING"
    assert events[0].attempts == 0
    assert events[0].published_at is None
    stored_available_at = events[0].available_at
    if stored_available_at.tzinfo is None:
        stored_available_at = stored_available_at.replace(tzinfo=UTC)
    assert stored_available_at == second_available_at
