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
    TelegramCourseWorkDelivery,
)
from modules.service_orders.models import ServiceOrder
from modules.service_orders.service import ServiceOrderService, ServiceOrderStatus
from modules.users.service import UserService


@pytest.fixture
async def course_delivery_db(tmp_path: Path) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'course-delivery.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await UserService().register(
            session,
            telegram_user_id=1551,
            username="course-owner",
            first_name="Owner",
            last_name=None,
            language_code="uz",
        )
    yield factory
    await engine.dispose()


async def _ready_order(factory, storage: LocalStorage, tmp_path: Path) -> ServiceOrder:  # type: ignore[no-untyped-def]
    async with factory() as session:
        order = await ServiceOrderService().create_course_work_order(
            session,
            telegram_user_id=1551,
            request_payload={
                "subject": "Informatika",
                "topic": "Algoritmik fikrlash",
                "sections": "Nazariy asoslar; Amaliy tahlil; Tavsiyalar",
                "language": "uz",
                "length": "short",
                "requirements": "",
            },
            idempotency_key="course-delivery-1",
        )
        user_id = order.user_id
        order_id = order.id
    docx = tmp_path / "course.docx"
    pdf = tmp_path / "course.pdf"
    docx.write_bytes(b"PK\x03\x04-docx")
    pdf.write_bytes(b"%PDF-1.4\n%%EOF")
    token = "d" * 32
    docx_key = await storage.put_service_file(
        docx,
        user_id=user_id,
        service_order_id=order_id,
        artifact_type="docx",
        attempt_token=token,
        service_type="course",
    )
    pdf_key = await storage.put_service_file(
        pdf,
        user_id=user_id,
        service_order_id=order_id,
        artifact_type="pdf",
        attempt_token=token,
        service_type="course",
    )
    async with factory() as session, session.begin():
        saved = await session.get(ServiceOrder, order_id, with_for_update=True)
        assert saved is not None
        saved.status = ServiceOrderStatus.ARTIFACTS_READY
        saved.docx_storage_key = docx_key
        saved.pdf_storage_key = pdf_key
        return saved


async def test_course_delivery_is_owner_scoped_and_idempotent(
    course_delivery_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    storage = LocalStorage(tmp_path / "storage")
    order = await _ready_order(course_delivery_db, storage, tmp_path)
    bot = SimpleNamespace(
        send_document=AsyncMock(
            side_effect=[SimpleNamespace(message_id=601), SimpleNamespace(message_id=602)]
        )
    )
    delivery = TelegramCourseWorkDelivery(storage=storage, work_root=tmp_path / "work")

    await delivery.deliver(course_delivery_db, service_order_id=order.id, bot=bot)
    await delivery.deliver(course_delivery_db, service_order_id=order.id, bot=bot)

    assert bot.send_document.await_count == 2
    first = bot.send_document.await_args_list[0].kwargs
    assert first["chat_id"] == 1551
    assert first["document"].filename == f"kurs_ishi_{order.id}.docx"
    assert "Kurs ishi" in first["caption"]
    async with course_delivery_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.status == ServiceOrderStatus.DELIVERED
    assert saved.delivery_attempts == 1


async def test_course_ambiguous_send_is_never_automatically_retried(
    course_delivery_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    storage = LocalStorage(tmp_path / "storage")
    order = await _ready_order(course_delivery_db, storage, tmp_path)
    bot = SimpleNamespace(
        send_document=AsyncMock(
            side_effect=[SimpleNamespace(message_id=701), TimeoutError("unknown acceptance")]
        )
    )
    delivery = TelegramCourseWorkDelivery(storage=storage, work_root=tmp_path / "work")

    with pytest.raises(EssayDeliveryAmbiguous):
        await delivery.deliver(course_delivery_db, service_order_id=order.id, bot=bot)
    await delivery.deliver(course_delivery_db, service_order_id=order.id, bot=bot)

    assert bot.send_document.await_count == 2
    async with course_delivery_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.delivery_status == "AMBIGUOUS"
    assert saved.docx_telegram_message_id == 701
    assert saved.pdf_telegram_message_id is None


async def test_stale_course_delivery_worker_cannot_send_after_lease_takeover(
    course_delivery_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    storage = LocalStorage(tmp_path / "storage")
    order = await _ready_order(course_delivery_db, storage, tmp_path)
    real_download = storage.download_for_user

    async def download_then_take_over(*args, **kwargs):  # type: ignore[no-untyped-def]
        path = await real_download(*args, **kwargs)
        async with course_delivery_db() as session, session.begin():
            saved = await session.get(ServiceOrder, order.id, with_for_update=True)
            assert saved is not None
            saved.delivery_lease_token = "winner-token"
            saved.delivery_lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        return path

    storage.download_for_user = AsyncMock(side_effect=download_then_take_over)  # type: ignore[method-assign]
    bot = SimpleNamespace(send_document=AsyncMock())
    delivery = TelegramCourseWorkDelivery(storage=storage, work_root=tmp_path / "work")

    with pytest.raises(EssayDeliveryLeaseLost):
        await delivery.deliver(course_delivery_db, service_order_id=order.id, bot=bot)

    bot.send_document.assert_not_awaited()
