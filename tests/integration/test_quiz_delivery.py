from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import core.database.models  # noqa: F401
from core.database.base import Base
from modules.files.storage import LocalStorage, StorageAccessDenied
from modules.service_orders.essay_delivery import (
    EssayDeliveryAmbiguous,
    EssayDeliveryLeaseLost,
    TelegramQuizDelivery,
)
from modules.service_orders.models import ServiceOrder
from modules.service_orders.service import ServiceOrderService, ServiceOrderStatus
from modules.users.service import UserService


@pytest.fixture
async def quiz_delivery_db(tmp_path: Path) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'quiz-delivery.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await UserService().register(
            session,
            telegram_user_id=1771,
            username="quiz-owner",
            first_name="Owner",
            last_name=None,
            language_code="uz",
        )
    yield factory
    await engine.dispose()


async def _ready_order(factory, storage: LocalStorage, tmp_path: Path) -> ServiceOrder:  # type: ignore[no-untyped-def]
    async with factory() as session:
        order = await ServiceOrderService().create_quiz_order(
            session,
            telegram_user_id=1771,
            request_payload={
                "topic": "Algoritmik fikrlash",
                "question_count": 4,
                "difficulty": "medium",
                "option_count": 4,
                "answer_key": True,
                "language": "uz",
                "requirements": "",
            },
            idempotency_key="quiz-delivery-1",
        )
        user_id = order.user_id
        order_id = order.id
    docx = tmp_path / "quiz.docx"
    pdf = tmp_path / "quiz.pdf"
    docx.write_bytes(b"PK\x03\x04-docx")
    pdf.write_bytes(b"%PDF-1.4\n%%EOF")
    token = "f" * 32
    docx_key = await storage.put_service_file(
        docx,
        user_id=user_id,
        service_order_id=order_id,
        artifact_type="docx",
        attempt_token=token,
        service_type="quiz",
    )
    pdf_key = await storage.put_service_file(
        pdf,
        user_id=user_id,
        service_order_id=order_id,
        artifact_type="pdf",
        attempt_token=token,
        service_type="quiz",
    )
    async with factory() as session, session.begin():
        saved = await session.get(ServiceOrder, order_id, with_for_update=True)
        assert saved is not None
        saved.status = ServiceOrderStatus.ARTIFACTS_READY
        saved.docx_storage_key = docx_key
        saved.pdf_storage_key = pdf_key
        return saved


async def test_quiz_delivery_is_owner_scoped_checkpointed_and_idempotent(
    quiz_delivery_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    storage = LocalStorage(tmp_path / "storage")
    order = await _ready_order(quiz_delivery_db, storage, tmp_path)
    bot = SimpleNamespace(
        send_document=AsyncMock(
            side_effect=[SimpleNamespace(message_id=801), SimpleNamespace(message_id=802)]
        )
    )
    delivery = TelegramQuizDelivery(storage=storage, work_root=tmp_path / "work")

    await delivery.deliver(quiz_delivery_db, service_order_id=order.id, bot=bot)
    await delivery.deliver(quiz_delivery_db, service_order_id=order.id, bot=bot)

    assert bot.send_document.await_count == 2
    first = bot.send_document.await_args_list[0].kwargs
    assert first["chat_id"] == 1771
    assert first["document"].filename == f"quiz_{order.id}.docx"
    assert "Quiz" in first["caption"]
    with pytest.raises(StorageAccessDenied):
        await storage.download_for_user(
            order.docx_storage_key,
            tmp_path / "stolen.docx",
            user_id=order.user_id + 1,
        )
    async with quiz_delivery_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.status == ServiceOrderStatus.DELIVERED
    assert saved.delivery_attempts == 1


async def test_quiz_ambiguous_send_is_never_automatically_retried(
    quiz_delivery_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    storage = LocalStorage(tmp_path / "storage")
    order = await _ready_order(quiz_delivery_db, storage, tmp_path)
    bot = SimpleNamespace(
        send_document=AsyncMock(
            side_effect=[SimpleNamespace(message_id=901), TimeoutError("unknown acceptance")]
        )
    )
    delivery = TelegramQuizDelivery(storage=storage, work_root=tmp_path / "work")

    with pytest.raises(EssayDeliveryAmbiguous):
        await delivery.deliver(quiz_delivery_db, service_order_id=order.id, bot=bot)
    await delivery.deliver(quiz_delivery_db, service_order_id=order.id, bot=bot)

    assert bot.send_document.await_count == 2
    async with quiz_delivery_db() as session:
        saved = await session.get(ServiceOrder, order.id)
    assert saved is not None
    assert saved.delivery_status == "AMBIGUOUS"
    assert saved.docx_telegram_message_id == 901
    assert saved.pdf_telegram_message_id is None


async def test_stale_quiz_delivery_worker_cannot_send_after_lease_takeover(
    quiz_delivery_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    storage = LocalStorage(tmp_path / "storage")
    order = await _ready_order(quiz_delivery_db, storage, tmp_path)
    real_download = storage.download_for_user

    async def download_then_take_over(*args, **kwargs):  # type: ignore[no-untyped-def]
        path = await real_download(*args, **kwargs)
        async with quiz_delivery_db() as session, session.begin():
            saved = await session.get(ServiceOrder, order.id, with_for_update=True)
            assert saved is not None
            saved.delivery_lease_token = "winner-token"
            saved.delivery_lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        return path

    storage.download_for_user = AsyncMock(side_effect=download_then_take_over)  # type: ignore[method-assign]
    bot = SimpleNamespace(send_document=AsyncMock())
    delivery = TelegramQuizDelivery(storage=storage, work_root=tmp_path / "work")

    with pytest.raises(EssayDeliveryLeaseLost):
        await delivery.deliver(quiz_delivery_db, service_order_id=order.id, bot=bot)
    bot.send_document.assert_not_awaited()
