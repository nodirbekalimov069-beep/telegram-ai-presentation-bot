from pathlib import Path
from unittest.mock import AsyncMock
from datetime import UTC, datetime, timedelta

import pytest
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
    EssayPipeline,
    EssayRetryableError,
    EssayTerminalError,
    cleanup_essay_orphan_artifacts,
    mark_terminal_essay_failure,
)
from modules.service_orders.models import ServiceOrder
from modules.service_orders.service import ServiceOrderService, ServiceOrderStatus
from modules.users.service import UserService


@pytest.fixture
async def essay_db(tmp_path: Path) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'essay.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await UserService().register(
            session,
            telegram_user_id=888,
            username="essay-owner",
            first_name="Owner",
            last_name=None,
            language_code="uz",
        )
    yield factory
    await engine.dispose()


async def _create_order(factory: async_sessionmaker[AsyncSession]) -> ServiceOrder:
    async with factory() as session:
        return await ServiceOrderService().create_essay_order(
            session,
            telegram_user_id=888,
            request_payload={
                "topic": "Raqamli iqtisodiyot",
                "language": "uz",
                "length": "short",
                "requirements": "O‘zbekiston misollari",
            },
            idempotency_key="pipeline-order-1",
        )


async def _fake_pdf(docx: Path, output_dir: Path, **_: object) -> Path:
    pdf = output_dir / f"{docx.stem}.pdf"
    output = canvas.Canvas(str(pdf))
    output.drawString(72, 760, "Raqamli iqtisodiyot")
    for line in range(8):
        output.drawString(
            72,
            730 - line * 24,
            "Mazmunli referat matni raqamli iqtisodiyotning amaliy tomonlarini tushuntiradi.",
        )
    output.save()
    return pdf


@pytest.mark.asyncio
async def test_pipeline_generates_artifacts_once_and_is_restart_idempotent(
    essay_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    order = await _create_order(essay_db)
    provider = MockAIProvider()
    provider.generate_essay = AsyncMock(wraps=provider.generate_essay)  # type: ignore[method-assign]
    storage = LocalStorage(tmp_path / "storage")
    pipeline = EssayPipeline(
        essay_db,
        provider,
        storage,
        temp_root=tmp_path / "work",
        pdf_converter=_fake_pdf,
    )

    await pipeline.process(order.id)
    await pipeline.process(order.id)

    async with essay_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.status == ServiceOrderStatus.ARTIFACTS_READY
    assert saved.generation_attempts == 1
    assert saved.docx_storage_key is not None
    assert saved.docx_storage_key.startswith(
        f"users/{saved.user_id}/service-orders/{order.id}/attempts/"
    )
    assert saved.docx_storage_key.endswith(f"/referat_{order.id}.docx")
    assert saved.pdf_storage_key is not None
    assert saved.pdf_storage_key.startswith(
        f"users/{saved.user_id}/service-orders/{order.id}/attempts/"
    )
    assert saved.pdf_storage_key.endswith(f"/referat_{order.id}.pdf")
    assert saved.completed_at is not None
    assert saved.generation_lease_token is None
    assert saved.generation_metadata["section_count"] >= 2
    assert await storage.exists(saved.docx_storage_key)
    assert await storage.exists(saved.pdf_storage_key)
    assert provider.generate_essay.await_count == 1  # type: ignore[attr-defined]
    assert not (tmp_path / "work" / str(order.id)).exists()


@pytest.mark.asyncio
async def test_pdf_failure_keeps_docx_and_records_controlled_error(
    essay_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    order = await _create_order(essay_db)

    async def fail_pdf(*_: object, **__: object) -> Path:
        raise TimeoutError("secret filesystem /tmp/provider")

    pipeline = EssayPipeline(
        essay_db,
        MockAIProvider(),
        LocalStorage(tmp_path / "storage"),
        temp_root=tmp_path / "work",
        pdf_converter=fail_pdf,
    )
    await pipeline.process(order.id)

    async with essay_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.status == ServiceOrderStatus.ARTIFACTS_READY
    assert saved.docx_storage_key is not None
    assert saved.pdf_storage_key is None
    assert saved.pdf_error == "PDF_CONVERSION_FAILED"
    assert "secret" not in str(saved.result_metadata)


@pytest.mark.asyncio
async def test_ai_failure_releases_lease_and_returns_order_to_queue(
    essay_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    order = await _create_order(essay_db)
    provider = MockAIProvider()
    provider.generate_essay = AsyncMock(side_effect=TimeoutError("provider secret"))  # type: ignore[method-assign]
    pipeline = EssayPipeline(
        essay_db,
        provider,
        LocalStorage(tmp_path / "storage"),
        temp_root=tmp_path / "work",
        pdf_converter=_fake_pdf,
    )

    with pytest.raises(EssayRetryableError):
        await pipeline.process(order.id)

    async with essay_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.status == ServiceOrderStatus.QUEUED
    assert saved.generation_attempts == 1
    assert saved.generation_lease_token is None
    assert saved.error_code == "AI_GENERATION_FAILED"
    assert saved.error_message == "Referat matnini yaratib bo‘lmadi"


@pytest.mark.asyncio
async def test_active_duplicate_worker_is_fenced_and_expired_lease_is_recoverable(
    essay_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    order = await _create_order(essay_db)
    async with essay_db() as session, session.begin():
        saved = await session.get(ServiceOrder, order.id)
        assert saved is not None
        saved.status = ServiceOrderStatus.GENERATING
        saved.generation_lease_token = "crashed-worker"
        saved.generation_lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)

    pipeline = EssayPipeline(
        essay_db,
        MockAIProvider(),
        LocalStorage(tmp_path / "storage"),
        temp_root=tmp_path / "work",
        pdf_converter=_fake_pdf,
    )
    with pytest.raises(EssayLeaseBusy):
        await pipeline.process(order.id)

    async with essay_db() as session, session.begin():
        saved = await session.get(ServiceOrder, order.id)
        assert saved is not None
        saved.generation_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await pipeline.process(order.id)

    async with essay_db() as session:
        recovered = await session.get(ServiceOrder, order.id)
    assert recovered is not None
    assert recovered.status == ServiceOrderStatus.ARTIFACTS_READY
    assert recovered.generation_lease_token is None


@pytest.mark.asyncio
async def test_stale_worker_cannot_overwrite_or_delete_successor_attempt(
    essay_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    order = await _create_order(essay_db)
    successor_token = "b" * 32

    class TakeoverStorage(LocalStorage):
        uploaded_key: str | None = None

        async def put_service_file(
            self,
            source: Path,
            *,
            user_id: int,
            service_order_id: int,
            artifact_type: str,
            attempt_token: str,
        ) -> str:
            key = await super().put_service_file(
                source,
                user_id=user_id,
                service_order_id=service_order_id,
                artifact_type=artifact_type,
                attempt_token=attempt_token,
            )
            self.uploaded_key = key
            async with essay_db() as session, session.begin():
                saved = await session.get(ServiceOrder, service_order_id, with_for_update=True)
                assert saved is not None
                saved.generation_lease_token = successor_token
                saved.generation_lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
            return key

    storage = TakeoverStorage(tmp_path / "storage")
    order_root = tmp_path / "work" / f"essay-{order.id}"
    successor_dir = order_root / successor_token
    successor_dir.mkdir(parents=True)
    sentinel = successor_dir / "successor.tmp"
    sentinel.write_text("owned by successor")
    pipeline = EssayPipeline(
        essay_db,
        MockAIProvider(),
        storage,
        temp_root=tmp_path / "work",
        pdf_converter=_fake_pdf,
        lease_seconds=30,
    )

    with pytest.raises(EssayLeaseBusy):
        await pipeline.process(order.id)

    assert sentinel.read_text() == "owned by successor"
    assert storage.uploaded_key is not None
    assert not await storage.exists(storage.uploaded_key)


@pytest.mark.asyncio
async def test_ambiguous_upload_orphan_is_durably_reconciled_on_takeover(
    essay_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    order = await _create_order(essay_db)

    class AmbiguousStorage(LocalStorage):
        fail_upload = True
        fail_delete = True
        ambiguous_key: str | None = None

        async def put_service_file(
            self,
            source: Path,
            *,
            user_id: int,
            service_order_id: int,
            artifact_type: str,
            attempt_token: str,
        ) -> str:
            key = await super().put_service_file(
                source,
                user_id=user_id,
                service_order_id=service_order_id,
                artifact_type=artifact_type,
                attempt_token=attempt_token,
            )
            if self.fail_upload:
                self.fail_upload = False
                self.ambiguous_key = key
                raise TimeoutError("upload response was lost")
            return key

        async def delete_for_user(self, key: str, *, user_id: int) -> None:
            if self.fail_delete:
                self.fail_delete = False
                raise ConnectionError("object store cleanup unavailable")
            await super().delete_for_user(key, user_id=user_id)

    storage = AmbiguousStorage(tmp_path / "storage")
    pipeline = EssayPipeline(
        essay_db,
        MockAIProvider(),
        storage,
        temp_root=tmp_path / "work",
        pdf_converter=_fake_pdf,
        lease_seconds=30,
    )

    with pytest.raises(ConnectionError):
        await pipeline.process(order.id)
    assert storage.ambiguous_key is not None
    assert await storage.exists(storage.ambiguous_key)
    async with essay_db() as session, session.begin():
        saved = await session.get(ServiceOrder, order.id)
        assert saved is not None
        assert saved.generation_metadata["pending_artifact_keys"]
        saved.generation_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    await pipeline.process(order.id)

    assert not await storage.exists(storage.ambiguous_key)
    async with essay_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.status == ServiceOrderStatus.ARTIFACTS_READY
    assert "pending_artifact_keys" not in saved.generation_metadata
    assert saved.generation_metadata["orphan_candidate_keys"]
    async with essay_db() as session:
        cleanup_event = await session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "service.essay.cleanup_requested",
                OutboxEvent.aggregate_id == str(order.id),
            )
        )
    assert cleanup_event is not None
    assert cleanup_event.status == "PENDING"

    await cleanup_essay_orphan_artifacts(essay_db, storage, order_id=order.id)

    async with essay_db() as session:
        cleaned = await session.get(ServiceOrder, order.id)
    assert cleaned is not None
    assert "orphan_candidate_keys" not in cleaned.generation_metadata


@pytest.mark.asyncio
async def test_docx_failure_is_terminal_and_does_not_leave_unknown_state(
    essay_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    order = await _create_order(essay_db)

    class BrokenRenderer:
        def render(self, *_: object, **__: object) -> Path:
            raise OSError("secret renderer path")

    pipeline = EssayPipeline(
        essay_db,
        MockAIProvider(),
        LocalStorage(tmp_path / "storage"),
        temp_root=tmp_path / "work",
        pdf_converter=_fake_pdf,
        renderer=BrokenRenderer(),  # type: ignore[arg-type]
    )
    with pytest.raises(EssayTerminalError):
        await pipeline.process(order.id)

    async with essay_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.status == ServiceOrderStatus.FAILED
    assert saved.error_code == "DOCX_RENDER_FAILED"
    assert saved.error_message == "Referat hujjatini yaratib bo‘lmadi"
    assert "secret" not in saved.error_message


@pytest.mark.asyncio
async def test_exhausted_retries_are_persisted_as_terminal_failure(
    essay_db,
) -> None:  # type: ignore[no-untyped-def]
    order = await _create_order(essay_db)

    assert await mark_terminal_essay_failure(essay_db, order_id=order.id) is True

    async with essay_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.status == ServiceOrderStatus.FAILED
    assert saved.error_code == "GENERATION_RETRIES_EXHAUSTED"


@pytest.mark.asyncio
async def test_terminal_generation_with_orphan_candidates_enqueues_cleanup(
    essay_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    order = await _create_order(essay_db)
    token = "c" * 32
    candidate = f"users/1/service-orders/{order.id}/attempts/{'a' * 32}/referat_{order.id}.docx"
    async with essay_db() as session, session.begin():
        saved = await session.get(ServiceOrder, order.id)
        assert saved is not None
        saved.status = ServiceOrderStatus.GENERATING
        saved.generation_lease_token = token
        saved.generation_lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        saved.generation_metadata = {"orphan_candidate_keys": [candidate]}
    pipeline = EssayPipeline(
        essay_db,
        MockAIProvider(),
        LocalStorage(tmp_path / "storage"),
        temp_root=tmp_path / "work",
        pdf_converter=_fake_pdf,
    )

    await pipeline._mark_terminal(
        order.id,
        token,
        error_code="DOCX_RENDER_FAILED",
        user_message="controlled",
    )

    async with essay_db() as session:
        event = await session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "service.essay.cleanup_requested",
                OutboxEvent.aggregate_id == str(order.id),
            )
        )
    assert event is not None
    assert event.status == "PENDING"
