from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import core.database.models  # noqa: F401
from core.database.base import Base
from modules.files.storage import LocalStorage
from modules.service_orders.essay_delivery import (
    EssayDeliveryAmbiguous,
    EssayDeliveryLeaseLost,
    TelegramIndependentWorkDelivery,
)
from modules.service_orders.models import ServiceOrder
from modules.service_orders.service import ServiceOrderService, ServiceOrderStatus
from modules.users.service import UserService


@pytest.fixture
async def independent_delivery_db(tmp_path: Path) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'delivery.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await UserService().register(
            session,
            telegram_user_id=990,
            username="owner",
            first_name="Owner",
            last_name=None,
            language_code="uz",
        )
    yield factory
    await engine.dispose()


async def _ready_order(factory, storage: LocalStorage, tmp_path: Path) -> ServiceOrder:  # type: ignore[no-untyped-def]
    async with factory() as session:
        order = await ServiceOrderService().create_independent_work_order(
            session,
            telegram_user_id=990,
            request_payload={
                "subject": "Informatika",
                "topic": "Algoritmlar",
                "language": "uz",
                "length": "short",
                "requirements": "",
            },
            idempotency_key="independent-delivery-1",
        )
        user_id = order.user_id
        order_id = order.id
    docx = tmp_path / "work.docx"
    pdf = tmp_path / "work.pdf"
    docx.write_bytes(b"PK\x03\x04-docx")
    pdf.write_bytes(b"%PDF-1.4\n%%EOF")
    token = "d" * 32
    docx_key = await storage.put_service_file(
        docx,
        user_id=user_id,
        service_order_id=order_id,
        artifact_type="docx",
        attempt_token=token,
        service_type="independent",
    )
    pdf_key = await storage.put_service_file(
        pdf,
        user_id=user_id,
        service_order_id=order_id,
        artifact_type="pdf",
        attempt_token=token,
        service_type="independent",
    )
    async with factory() as session, session.begin():
        saved = await session.get(ServiceOrder, order_id, with_for_update=True)
        assert saved is not None
        saved.status = ServiceOrderStatus.ARTIFACTS_READY
        saved.docx_storage_key = docx_key
        saved.pdf_storage_key = pdf_key
        return saved


async def test_independent_delivery_is_owner_scoped_and_idempotent(
    independent_delivery_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    storage = LocalStorage(tmp_path / "storage")
    order = await _ready_order(independent_delivery_db, storage, tmp_path)
    bot = SimpleNamespace(
        send_document=AsyncMock(
            side_effect=[SimpleNamespace(message_id=401), SimpleNamespace(message_id=402)]
        )
    )
    delivery = TelegramIndependentWorkDelivery(storage=storage, work_root=tmp_path / "work")

    await delivery.deliver(independent_delivery_db, service_order_id=order.id, bot=bot)
    await delivery.deliver(independent_delivery_db, service_order_id=order.id, bot=bot)

    assert bot.send_document.await_count == 2
    first = bot.send_document.await_args_list[0].kwargs
    assert first["chat_id"] == 990
    assert first["document"].filename == f"mustaqil_ish_{order.id}.docx"
    assert "Mustaqil ish" in first["caption"]
    async with independent_delivery_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.status == ServiceOrderStatus.DELIVERED
    assert saved.delivery_attempts == 1


async def test_independent_ambiguous_send_is_not_automatically_retried(
    independent_delivery_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    storage = LocalStorage(tmp_path / "storage")
    order = await _ready_order(independent_delivery_db, storage, tmp_path)
    bot = SimpleNamespace(
        send_document=AsyncMock(
            side_effect=[SimpleNamespace(message_id=501), TimeoutError("unknown acceptance")]
        )
    )
    delivery = TelegramIndependentWorkDelivery(storage=storage, work_root=tmp_path / "work")

    with pytest.raises(EssayDeliveryAmbiguous):
        await delivery.deliver(independent_delivery_db, service_order_id=order.id, bot=bot)
    await delivery.deliver(independent_delivery_db, service_order_id=order.id, bot=bot)

    assert bot.send_document.await_count == 2
    async with independent_delivery_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.delivery_status == "AMBIGUOUS"
    assert saved.docx_telegram_message_id == 501
    assert saved.pdf_telegram_message_id is None


async def test_stale_delivery_worker_rechecks_lease_immediately_before_send(
    independent_delivery_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    storage = LocalStorage(tmp_path / "storage")
    order = await _ready_order(independent_delivery_db, storage, tmp_path)
    real_download = storage.download_for_user

    async def download_then_take_over(*args, **kwargs):  # type: ignore[no-untyped-def]
        path = await real_download(*args, **kwargs)
        async with independent_delivery_db() as session, session.begin():
            saved = await session.get(ServiceOrder, order.id, with_for_update=True)
            assert saved is not None
            saved.delivery_lease_token = "winner-token"
            saved.delivery_lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        return path

    storage.download_for_user = AsyncMock(side_effect=download_then_take_over)  # type: ignore[method-assign]
    bot = SimpleNamespace(send_document=AsyncMock())
    delivery = TelegramIndependentWorkDelivery(storage=storage, work_root=tmp_path / "work")

    with pytest.raises(EssayDeliveryLeaseLost):
        await delivery.deliver(independent_delivery_db, service_order_id=order.id, bot=bot)

    bot.send_document.assert_not_awaited()
    async with independent_delivery_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.delivery_lease_token == "winner-token"
