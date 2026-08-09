from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import core.database.models  # noqa: F401
from core.database.base import Base
from core.outbox.models import OutboxEvent
from modules.files.storage import LocalStorage
from modules.service_orders.essay_delivery import (
    EssayDeliveryAmbiguous,
    EssayDeliveryAmbiguousPersistencePending,
    EssayDeliveryInProgress,
    TelegramEssayDelivery,
    mark_terminal_essay_delivery_failure,
)
from modules.service_orders.models import ServiceOrder
from modules.service_orders.service import ServiceOrderStatus
from modules.users.models import User


@pytest.fixture
async def delivery_db(tmp_path: Path) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'delivery.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _ready_order(factory, storage: LocalStorage, tmp_path: Path) -> ServiceOrder:  # type: ignore[no-untyped-def]
    docx = tmp_path / "referat.docx"
    pdf = tmp_path / "referat.pdf"
    docx.write_bytes(b"PK\x03\x04-docx")
    pdf.write_bytes(b"%PDF-1.4\n%%EOF")
    async with factory() as session, session.begin():
        user = User(telegram_user_id=999, first_name="Owner", language_code="uz")
        session.add(user)
        await session.flush()
        order = ServiceOrder(
            user_id=user.id,
            service_type="essay",
            status=ServiceOrderStatus.ARTIFACTS_READY,
            title="Xavfsiz mavzu",
            request_payload={"topic": "Xavfsiz mavzu"},
            normalized_request={"topic": "Xavfsiz mavzu"},
            request_hash="a" * 64,
            generation_metadata={},
            result_metadata={},
            idempotency_key="delivery-order-1",
            docx_storage_key=await storage.put_service_file(
                docx,
                user_id=user.id,
                service_order_id=1,
                artifact_type="docx",
                attempt_token="a" * 32,
            ),
            pdf_storage_key=await storage.put_service_file(
                pdf,
                user_id=user.id,
                service_order_id=1,
                artifact_type="pdf",
                attempt_token="a" * 32,
            ),
        )
        session.add(order)
        await session.flush()
        assert order.id == 1
        return order


@pytest.mark.asyncio
async def test_delivery_is_idempotent_and_marks_delivered(delivery_db, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    storage = LocalStorage(tmp_path / "storage")
    order = await _ready_order(delivery_db, storage, tmp_path)
    bot = SimpleNamespace(
        send_document=AsyncMock(
            side_effect=[SimpleNamespace(message_id=101), SimpleNamespace(message_id=102)]
        )
    )
    delivery = TelegramEssayDelivery(
        storage=storage,
        work_root=tmp_path / "work",
        max_upload_bytes=1_000_000,
    )

    await delivery.deliver(delivery_db, service_order_id=order.id, bot=bot)
    await delivery.deliver(delivery_db, service_order_id=order.id, bot=bot)

    async with delivery_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.status == ServiceOrderStatus.DELIVERED
    assert saved.delivery_status == "DELIVERED"
    assert saved.docx_telegram_message_id == 101
    assert saved.pdf_telegram_message_id == 102
    assert saved.delivered_at is not None
    assert bot.send_document.await_count == 2
    first = bot.send_document.await_args_list[0].kwargs
    assert first["chat_id"] == 999
    assert first["document"].filename == "referat_1.docx"
    assert "DOCX" in first["caption"]


@pytest.mark.asyncio
async def test_ambiguous_pdf_acceptance_is_not_automatically_resent(
    delivery_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    storage = LocalStorage(tmp_path / "storage")
    order = await _ready_order(delivery_db, storage, tmp_path)
    bot = SimpleNamespace(
        send_document=AsyncMock(
            side_effect=[
                SimpleNamespace(message_id=201),
                TimeoutError("Telegram secret response"),
                SimpleNamespace(message_id=202),
            ]
        )
    )
    delivery = TelegramEssayDelivery(storage=storage, work_root=tmp_path / "work")

    with pytest.raises(EssayDeliveryAmbiguous):
        await delivery.deliver(delivery_db, service_order_id=order.id, bot=bot)
    await delivery.deliver(delivery_db, service_order_id=order.id, bot=bot)

    async with delivery_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.status == ServiceOrderStatus.ARTIFACTS_READY
    assert saved.delivery_status == "AMBIGUOUS"
    assert saved.docx_telegram_message_id == 201
    assert saved.pdf_telegram_message_id is None
    assert saved.delivery_attempts == 1
    assert "ambiguous" in (saved.delivery_error or "")
    assert bot.send_document.await_count == 2


@pytest.mark.asyncio
async def test_checkpoint_and_ambiguity_write_failure_never_resends(
    delivery_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    storage = LocalStorage(tmp_path / "storage")
    order = await _ready_order(delivery_db, storage, tmp_path)
    bot = SimpleNamespace(send_document=AsyncMock(return_value=SimpleNamespace(message_id=301)))
    delivery = TelegramEssayDelivery(storage=storage, work_root=tmp_path / "work")
    monkeypatch.setattr(
        delivery,
        "_record_message",
        AsyncMock(side_effect=ConnectionError("checkpoint database unavailable")),
    )
    monkeypatch.setattr(
        delivery,
        "_mark_ambiguous",
        AsyncMock(side_effect=ConnectionError("ambiguity database unavailable")),
    )

    with pytest.raises(EssayDeliveryAmbiguousPersistencePending):
        await delivery.deliver(delivery_db, service_order_id=order.id, bot=bot)
    with pytest.raises(EssayDeliveryInProgress):
        await delivery.deliver(delivery_db, service_order_id=order.id, bot=bot)

    assert bot.send_document.await_count == 1
    async with delivery_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.delivery_status == "SENDING"
    assert saved.delivery_lease_token is not None


@pytest.mark.asyncio
async def test_pending_delivery_is_terminally_reconciled_after_runtime_exhaustion(
    delivery_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    storage = LocalStorage(tmp_path / "storage")
    order = await _ready_order(delivery_db, storage, tmp_path)
    async with delivery_db() as session, session.begin():
        saved = await session.get(ServiceOrder, order.id)
        assert saved is not None
        saved.generation_metadata = {
            "orphan_candidate_keys": [
                f"users/{saved.user_id}/service-orders/{order.id}/attempts/"
                f"{'b' * 32}/referat_{order.id}.docx"
            ]
        }

    resolved = await mark_terminal_essay_delivery_failure(delivery_db, service_order_id=order.id)

    assert resolved is True
    async with delivery_db() as session:
        saved = await session.get(ServiceOrder, order.id)
        event = await session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "service.essay.cleanup_requested",
                OutboxEvent.aggregate_id == str(order.id),
            )
        )
    assert saved is not None
    assert saved.status == ServiceOrderStatus.ARTIFACTS_READY
    assert saved.delivery_status == "TERMINAL_FAILED"
    assert "history" in (saved.delivery_error or "")
    assert event is not None


@pytest.mark.asyncio
async def test_live_delivery_lease_is_never_acknowledged_by_terminal_finalizer(
    delivery_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    storage = LocalStorage(tmp_path / "storage")
    order = await _ready_order(delivery_db, storage, tmp_path)
    async with delivery_db() as session, session.begin():
        saved = await session.get(ServiceOrder, order.id)
        assert saved is not None
        saved.delivery_status = "SENDING"
        saved.delivery_lease_token = "live-token"
        saved.delivery_lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)

    with pytest.raises(EssayDeliveryInProgress):
        await mark_terminal_essay_delivery_failure(delivery_db, service_order_id=order.id)
