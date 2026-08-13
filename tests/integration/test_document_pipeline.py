import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

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
    DocumentPipeline,
    EssayLeaseBusy,
    EssayTerminalError,
)
from modules.service_orders.models import ServiceOrder
from modules.service_orders.service import ServiceOrderService, ServiceOrderStatus
from modules.users.service import UserService


@pytest.fixture
async def document_db(tmp_path: Path):  # type: ignore[no-untyped-def]
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'document.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await UserService().register(
            session,
            telegram_user_id=8811,
            username="document-owner",
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
        pdf.drawString(30, 790 - index * 20, paragraph.text)
    pdf.showPage()
    pdf.save()
    return target


async def _create(factory: async_sessionmaker[AsyncSession]) -> ServiceOrder:
    async with factory() as session:
        return await ServiceOrderService().create_document_order(
            session,
            telegram_user_id=8811,
            request_payload={
                "document_type": "application",
                "details": (
                    "Toshkent davlat universiteti rektoriga. Men, Ali Valiyev, "
                    "2026-yil 15-avgustdan uch kunlik ta’til so‘rayman."
                ),
                "language": "uz",
            },
            idempotency_key="document-pipeline-001",
        )


async def test_document_order_is_transactional_concurrent_safe_and_one_outbox(
    document_db,
) -> None:  # type: ignore[no-untyped-def]
    barrier = asyncio.Barrier(2)

    async def create_concurrently() -> tuple[int, bool]:
        async with document_db() as session:
            await barrier.wait()
            order = await ServiceOrderService().create_document_order(
                session,
                telegram_user_id=8811,
                request_payload={
                    "document_type": "application",
                    "details": (
                        "Toshkent davlat universiteti rektoriga. Men, Ali Valiyev, "
                        "2026-yil 15-avgustdan uch kunlik ta’til so‘rayman."
                    ),
                    "language": "uz",
                },
                idempotency_key="document-pipeline-001",
            )
            transactions_usable = await session.scalar(select(func.count(ServiceOrder.id))) == 1
            return order.id, transactions_usable

    first, duplicate = await asyncio.gather(create_concurrently(), create_concurrently())
    assert first[0] == duplicate[0]
    assert first[1] is True and duplicate[1] is True
    async with document_db() as session:
        orders = await session.scalar(select(func.count(ServiceOrder.id)))
        events = list(await session.scalars(select(OutboxEvent)))
    assert orders == 1
    assert [event.event_type for event in events] == ["service.document.requested"]


@pytest.fixture
async def document_postgres_db():  # type: ignore[no-untyped-def]
    database_url = os.getenv("DOCUMENT_POSTGRES_TEST_URL")
    if database_url is None:
        pytest.skip("dedicated disposable PostgreSQL URL is not configured")
    assert database_url is not None
    if not database_url.endswith("/phase8_v16_document_disposable"):
        pytest.fail("PostgreSQL concurrency test URL is not the dedicated disposable database")
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await UserService().register(
            session,
            telegram_user_id=168811,
            username="v16-disposable-owner",
            first_name="V16",
            last_name=None,
            language_code="uz",
        )
    yield factory
    await engine.dispose()


async def test_document_order_postgres_concurrency_is_one_order_one_outbox(
    document_postgres_db,
) -> None:  # type: ignore[no-untyped-def]
    barrier = asyncio.Barrier(2)

    async def create_concurrently() -> tuple[int, bool]:
        async with document_postgres_db() as session:
            await barrier.wait()
            order = await ServiceOrderService().create_document_order(
                session,
                telegram_user_id=168811,
                request_payload={
                    "document_type": "application",
                    "details": (
                        "Toshkent davlat universiteti rektoriga. Men, Ali Valiyev, "
                        "2026-yil 15-avgustdan uch kunlik ta’til so‘rayman."
                    ),
                    "language": "uz",
                },
                idempotency_key="document-v16-postgres-concurrency",
            )
            usable = await session.scalar(select(func.count(ServiceOrder.id))) == 1
            return order.id, usable

    first, duplicate = await asyncio.gather(create_concurrently(), create_concurrently())
    async with document_postgres_db() as session:
        orders = await session.scalar(select(func.count(ServiceOrder.id)))
        events = await session.scalar(
            select(func.count(OutboxEvent.id)).where(
                OutboxEvent.event_type == "service.document.requested"
            )
        )
    assert first[0] == duplicate[0]
    assert first[1] is True and duplicate[1] is True
    assert orders == 1
    assert events == 1
    print("orders=1 requested_events=1 returned_order_ids=equal transactions_usable=true")


async def test_document_pipeline_generates_once_with_required_canonical_artifacts(
    document_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    order = await _create(document_db)
    provider = MockAIProvider()
    provider.generate_document = AsyncMock(wraps=provider.generate_document)  # type: ignore[method-assign]
    pipeline = DocumentPipeline(
        document_db,
        provider,
        LocalStorage(tmp_path / "storage"),
        temp_root=tmp_path / "work",
        pdf_converter=_pdf,
    )

    await pipeline.process(order.id)
    await pipeline.process(order.id)

    async with document_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.status == ServiceOrderStatus.ARTIFACTS_READY
    assert saved.generation_attempts == 1
    assert saved.generation_metadata["source_bound"] is True
    assert saved.generation_metadata["reference_count"] == 0
    assert saved.generation_metadata["canonical_parity_score"] == 1.0
    assert saved.docx_storage_key and saved.docx_storage_key.endswith(f"/hujjat_{order.id}.docx")
    assert saved.pdf_storage_key and saved.pdf_storage_key.endswith(f"/hujjat_{order.id}.pdf")
    provider.generate_document.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("failure", "error_code"),
    [
        (AIContractError("strict contract rejected"), "AI_CONTRACT_REJECTED"),
        (ConnectionError("transport retries exhausted"), "AI_PROVIDER_FAILED"),
    ],
)
async def test_document_generation_failure_is_terminal_without_artifacts_or_delivery(
    document_db, tmp_path: Path, failure: Exception, error_code: str
) -> None:  # type: ignore[no-untyped-def]
    order = await _create(document_db)
    provider = MockAIProvider()
    provider.generate_document = AsyncMock(side_effect=failure)  # type: ignore[method-assign]
    converter = AsyncMock(side_effect=AssertionError("PDF conversion must not run"))
    pipeline = DocumentPipeline(
        document_db,
        provider,
        LocalStorage(tmp_path / "storage"),
        temp_root=tmp_path / "work",
        pdf_converter=converter,
    )

    with pytest.raises(EssayTerminalError):
        await pipeline.process(order.id)
    with pytest.raises(EssayTerminalError):
        await pipeline.process(order.id)

    converter.assert_not_awaited()
    async with document_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.status == ServiceOrderStatus.FAILED
    assert saved.error_code == error_code
    assert saved.docx_storage_key is None and saved.pdf_storage_key is None
    assert saved.docx_telegram_message_id is None and saved.pdf_telegram_message_id is None


async def test_document_pdf_parity_failure_stores_and_delivers_nothing(
    document_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    order = await _create(document_db)

    async def wrong_pdf(docx: Path, output_dir: Path, **_: object) -> Path:
        target = output_dir / f"{docx.stem}.pdf"
        pdf = canvas.Canvas(str(target))
        pdf.drawString(30, 790, "Forged unrelated PDF body")
        pdf.showPage()
        pdf.save()
        return target

    pipeline = DocumentPipeline(
        document_db,
        MockAIProvider(),
        LocalStorage(tmp_path / "storage"),
        temp_root=tmp_path / "work",
        pdf_converter=wrong_pdf,
    )
    with pytest.raises(EssayTerminalError):
        await pipeline.process(order.id)
    async with document_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.status == ServiceOrderStatus.FAILED
    assert saved.error_code == "PDF_CONVERSION_FAILED"
    assert saved.docx_storage_key is None and saved.pdf_storage_key is None


async def test_document_active_generation_lease_blocks_duplicate_worker(
    document_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    order = await _create(document_db)
    async with document_db() as session, session.begin():
        saved = await session.get(ServiceOrder, order.id, with_for_update=True)
        assert saved is not None
        saved.status = ServiceOrderStatus.PROCESSING
        saved.generation_lease_token = "active-worker"
        saved.generation_lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    provider = MockAIProvider()
    provider.generate_document = AsyncMock(wraps=provider.generate_document)  # type: ignore[method-assign]
    pipeline = DocumentPipeline(
        document_db,
        provider,
        LocalStorage(tmp_path / "storage"),
        temp_root=tmp_path / "work",
        pdf_converter=_pdf,
    )
    with pytest.raises(EssayLeaseBusy):
        await pipeline.process(order.id)
    provider.generate_document.assert_not_awaited()  # type: ignore[attr-defined]
