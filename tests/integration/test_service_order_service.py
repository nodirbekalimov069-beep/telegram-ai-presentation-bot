from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apps.bot.handlers.menu import _send_orders
from apps.bot.localization import Localizer
from core.database.base import Base
import core.database.models  # noqa: F401
from modules.service_orders.catalog import ServiceType
from modules.service_orders.models import ServiceOrder
from modules.service_orders.service import ServiceOrderService, ServiceOrderStatus
from modules.users.models import User
from modules.users.service import UserService


@pytest.fixture
async def service_order_db(tmp_path: Path) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'service-orders.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await UserService().register(
            session,
            telegram_user_id=777,
            username="owner",
            first_name="Owner",
            last_name=None,
            language_code="uz",
        )
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_preview_order_creation_is_idempotent_and_server_validated(service_order_db) -> None:  # type: ignore[no-untyped-def]
    service = ServiceOrderService()
    request = {
        "topic": "AI xavfsizligi",
        "language": "uz",
        "length": "medium",
        "requirements": "Manbalar ko‘rsatilsin",
    }
    async with service_order_db() as session:
        first = await service.create_preview_order(
            session,
            telegram_user_id=777,
            service_type=ServiceType.ESSAY,
            request_payload=request,
            idempotency_key="svc-test-0001",
        )
    async with service_order_db() as session:
        duplicate = await service.create_preview_order(
            session,
            telegram_user_id=777,
            service_type=ServiceType.ESSAY,
            request_payload=request,
            idempotency_key="svc-test-0001",
        )
        count = await session.scalar(select(func.count(ServiceOrder.id)))

    assert duplicate.id == first.id
    assert count == 1
    assert first.status == ServiceOrderStatus.PREVIEW
    assert first.title == "AI xavfsizligi"
    assert first.result_metadata == {}
    assert first.error_message is None
    assert first.retry_count == 0


@pytest.mark.asyncio
async def test_idempotency_key_cannot_be_reused_for_different_payload(service_order_db) -> None:  # type: ignore[no-untyped-def]
    service = ServiceOrderService()
    base = {
        "text": "Sinov uchun yetarlicha uzun matn.",
        "target_language": "en",
    }
    async with service_order_db() as session:
        await service.create_preview_order(
            session,
            telegram_user_id=777,
            service_type=ServiceType.TRANSLATE,
            request_payload=base,
            idempotency_key="svc-test-0002",
        )
    async with service_order_db() as session:
        with pytest.raises(ValueError, match="idempotency"):
            await service.create_preview_order(
                session,
                telegram_user_id=777,
                service_type=ServiceType.TRANSLATE,
                request_payload={**base, "target_language": "ru"},
                idempotency_key="svc-test-0002",
            )


@pytest.mark.asyncio
async def test_blocked_or_unknown_user_cannot_create_service_order(service_order_db) -> None:  # type: ignore[no-untyped-def]
    service = ServiceOrderService()
    payload = {"text": "Sinov matni", "mode": "simple"}
    async with service_order_db() as session:
        with pytest.raises(PermissionError):
            await service.create_preview_order(
                session,
                telegram_user_id=999,
                service_type=ServiceType.REWRITE,
                request_payload=payload,
                idempotency_key="svc-test-0003",
            )
    async with service_order_db() as session, session.begin():
        user = await session.scalar(select(User).where(User.telegram_user_id == 777))
        assert user is not None
        user.is_blocked = True
    async with service_order_db() as session:
        with pytest.raises(PermissionError):
            await service.create_preview_order(
                session,
                telegram_user_id=777,
                service_type=ServiceType.REWRITE,
                request_payload=payload,
                idempotency_key="svc-test-0004",
            )


@pytest.mark.asyncio
async def test_failure_metadata_and_retry_counter_are_persisted(service_order_db) -> None:  # type: ignore[no-untyped-def]
    service = ServiceOrderService()
    async with service_order_db() as session:
        order = await service.create_preview_order(
            session,
            telegram_user_id=777,
            service_type=ServiceType.DOCUMENT,
            request_payload={"document_type": "application", "details": "Universitet uchun"},
            idempotency_key="svc-test-0005",
        )
    async with service_order_db() as session:
        await service.mark_failed(
            session,
            order_id=order.id,
            error_code="NOT_IMPLEMENTED",
            error_message="Generation engine is unavailable",
        )
    async with service_order_db() as session:
        saved = await session.get(ServiceOrder, order.id)

    assert saved is not None
    assert saved.status == ServiceOrderStatus.FAILED
    assert saved.error_code == "NOT_IMPLEMENTED"
    assert saved.error_message == "Generation engine is unavailable"
    assert saved.retry_count == 1


@pytest.mark.asyncio
async def test_universal_orders_use_database_pagination_without_a_global_cap(
    service_order_db,
) -> None:  # type: ignore[no-untyped-def]
    async with service_order_db() as session:
        now = datetime.now(UTC)
        async with session.begin():
            user = await session.scalar(select(User).where(User.telegram_user_id == 777))
            assert user is not None
            session.add_all(
                [
                    ServiceOrder(
                        user_id=user.id,
                        service_type=ServiceType.ESSAY.value,
                        status=ServiceOrderStatus.PREVIEW.value,
                        title=f"topic-{index}",
                        request_payload={"topic": f"topic-{index}"},
                        request_hash=f"{index:064d}",
                        result_metadata={},
                        retry_count=0,
                        idempotency_key=f"pagination-{index}",
                        created_at=now + timedelta(seconds=index),
                        updated_at=now + timedelta(seconds=index),
                    )
                    for index in range(13)
                ]
            )
        message = SimpleNamespace(answer=AsyncMock())
        await _send_orders(
            message,
            session,
            user,
            Localizer.load_default(),
            page=999,
        )

    text = message.answer.await_args.args[0]
    assert "Sahifa 3/3" in text
    assert text.count("• Referat") == 3
