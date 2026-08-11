from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import core.database.models  # noqa: F401
from core.database.base import Base
from modules.files.storage import LocalStorage, StorageAccessDenied
from modules.service_orders.essay_delivery import TelegramWriteTextDelivery
from modules.service_orders.models import ServiceOrder
from modules.service_orders.service import ServiceOrderService, ServiceOrderStatus
from modules.users.service import UserService


@pytest.fixture
async def write_delivery_db(tmp_path: Path) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'write-delivery.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await UserService().register(
            session,
            telegram_user_id=1881,
            username="write-owner",
            first_name="Owner",
            last_name=None,
            language_code="uz",
        )
    yield factory
    await engine.dispose()


async def test_write_text_delivery_is_owner_scoped_checkpointed_and_idempotent(
    write_delivery_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    storage = LocalStorage(tmp_path / "storage")
    async with write_delivery_db() as session:
        order = await ServiceOrderService().create_write_text_order(
            session,
            telegram_user_id=1881,
            request_payload={
                "topic": "Mahalla hamjihatligi",
                "text_type": "article",
                "style": "formal",
                "length": "short",
                "language": "uz",
                "requirements": "",
            },
            idempotency_key="write-delivery-one",
        )
        user_id, order_id = order.user_id, order.id
    docx, pdf = tmp_path / "matn.docx", tmp_path / "matn.pdf"
    docx.write_bytes(b"PK\x03\x04-docx")
    pdf.write_bytes(b"%PDF-1.4\n%%EOF")
    token = "f" * 32
    docx_key = await storage.put_service_file(
        docx,
        user_id=user_id,
        service_order_id=order_id,
        artifact_type="docx",
        attempt_token=token,
        service_type="write",
    )
    pdf_key = await storage.put_service_file(
        pdf,
        user_id=user_id,
        service_order_id=order_id,
        artifact_type="pdf",
        attempt_token=token,
        service_type="write",
    )
    async with write_delivery_db() as session, session.begin():
        saved = await session.get(ServiceOrder, order_id, with_for_update=True)
        assert saved is not None
        saved.status = ServiceOrderStatus.ARTIFACTS_READY
        saved.docx_storage_key = docx_key
        saved.pdf_storage_key = pdf_key

    bot = SimpleNamespace(
        send_document=AsyncMock(
            side_effect=[SimpleNamespace(message_id=811), SimpleNamespace(message_id=812)]
        )
    )
    delivery = TelegramWriteTextDelivery(storage=storage, work_root=tmp_path / "delivery")
    await delivery.deliver(write_delivery_db, service_order_id=order_id, bot=bot)
    await delivery.deliver(write_delivery_db, service_order_id=order_id, bot=bot)

    assert bot.send_document.await_count == 2
    first = bot.send_document.await_args_list[0].kwargs
    assert first["chat_id"] == 1881
    assert first["document"].filename == f"matn_{order_id}.docx"
    assert "Matn" in first["caption"]
    with pytest.raises(StorageAccessDenied):
        await storage.download_for_user(
            docx_key,
            tmp_path / "stolen.docx",
            user_id=user_id + 1,
        )
    async with write_delivery_db() as session:
        saved = await session.get(ServiceOrder, order_id)
    assert saved is not None
    assert saved.status == ServiceOrderStatus.DELIVERED
    assert saved.delivery_attempts == 1
