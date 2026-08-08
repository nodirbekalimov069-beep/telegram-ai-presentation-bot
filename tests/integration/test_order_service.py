from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.database.base import Base
import core.database.models  # noqa: F401
from modules.orders.domain import OrderStatus
from modules.orders.models import Order
from modules.orders.service import OrderService
from modules.plans.domain import PlanLimitError
from modules.plans.seed import seed_default_plans
from modules.users.models import User
from modules.users.service import UserService


@pytest.fixture
async def order_db(tmp_path: Path):  # type: ignore[no-untyped-def]
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'orders.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await seed_default_plans(session)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_user_registration_is_idempotent_and_updates_profile(order_db) -> None:  # type: ignore[no-untyped-def]
    service = UserService()
    async with order_db() as session:
        first = await service.register(
            session,
            telegram_user_id=42,
            username="old",
            first_name="Nodir",
            last_name=None,
            language_code="uz",
        )
    async with order_db() as session:
        second = await service.register(
            session,
            telegram_user_id=42,
            username="new",
            first_name="Nodirbek",
            last_name="Alimov",
            language_code="uz",
        )
    async with order_db() as session:
        count = await session.scalar(select(func.count(User.id)))

    assert first.id == second.id
    assert second.username == "new"
    assert second.first_name == "Nodirbek"
    assert count == 1


@pytest.mark.asyncio
async def test_paid_order_amount_and_status_come_from_database_plan(order_db) -> None:  # type: ignore[no-untyped-def]
    async with order_db() as session:
        await UserService().register(
            session,
            telegram_user_id=99,
            username=None,
            first_name="User",
            last_name=None,
            language_code="uz",
        )
    async with order_db() as session:
        order = await OrderService().create_paid_order(
            session,
            telegram_user_id=99,
            plan_code="premium",
            topic="AI",
            settings={"slide_count": 18, "language": "uz"},
        )

    assert order.amount == 5_000
    assert order.currency == "UZS"
    assert order.status == OrderStatus.WAITING_PAYMENT
    assert order.topic == "AI"


@pytest.mark.asyncio
async def test_paid_order_rejects_slide_count_outside_plan(order_db) -> None:  # type: ignore[no-untyped-def]
    async with order_db() as session:
        user = await UserService().register(
            session,
            telegram_user_id=100,
            username=None,
            first_name="User",
            last_name=None,
            language_code="uz",
        )
    async with order_db() as session:
        with pytest.raises(PlanLimitError):
            await OrderService().create_paid_order(
                session,
                user_id=user.id,
                plan_code="medium",
                topic="Invalid",
                settings={"slide_count": 20},
            )
    async with order_db() as session:
        count = await session.scalar(select(func.count(Order.id)))
    assert count == 0


@pytest.mark.asyncio
async def test_waiting_payment_cancellation_is_owner_scoped_and_idempotent(order_db) -> None:  # type: ignore[no-untyped-def]
    async with order_db() as session:
        owner = await UserService().register(
            session,
            telegram_user_id=101,
            username=None,
            first_name="Owner",
            last_name=None,
            language_code="uz",
        )
        await UserService().register(
            session,
            telegram_user_id=102,
            username=None,
            first_name="Other",
            last_name=None,
            language_code="uz",
        )
    async with order_db() as session:
        order = await OrderService().create_paid_order(
            session,
            user_id=owner.id,
            plan_code="medium",
            topic="Bekor qilinadigan buyurtma",
            settings={"slide_count": 10, "language": "uz"},
        )

    async with order_db() as session:
        with pytest.raises(LookupError, match="order not found"):
            await OrderService().cancel_waiting_payment(
                session,
                order_id=order.id,
                telegram_user_id=102,
            )
    async with order_db() as session:
        cancelled = await OrderService().cancel_waiting_payment(
            session,
            order_id=order.id,
            telegram_user_id=101,
        )
    async with order_db() as session:
        duplicate = await OrderService().cancel_waiting_payment(
            session,
            order_id=order.id,
            telegram_user_id=101,
        )
        saved = await session.get(Order, order.id)

    assert cancelled is True
    assert duplicate is False
    assert saved is not None and saved.status == OrderStatus.CANCELLED
