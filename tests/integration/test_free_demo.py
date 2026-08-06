import asyncio
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.database.base import Base
from modules.orders.models import Order
from modules.plans.models import Plan
from modules.users.models import FreeDemoClaim, User
from modules.users.service import FreeDemoService, FreeDemoUnavailable


@pytest.fixture
async def session_factory(tmp_path: Path) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'free-demo.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        session.add(
            Plan(
                code="free_demo",
                name="Free Demo",
                price_uzs=0,
                minimum_slides=1,
                maximum_slides=8,
                priority=1,
                features={"watermark": True},
                is_active=True,
            )
        )
        session.add(User(telegram_user_id=123456789, selected_language="uz"))
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_free_demo_is_claimed_only_once_under_parallel_requests(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = FreeDemoService()

    async def claim(topic: str) -> Order | None:
        async with session_factory() as session:
            try:
                return await service.claim(
                    session,
                    telegram_user_id=123456789,
                    topic=topic,
                    settings={"slide_count": 8, "language": "uz"},
                )
            except FreeDemoUnavailable:
                return None

    first, second = await asyncio.gather(claim("AI"), claim("Biologiya"))

    assert sum(result is not None for result in (first, second)) == 1
    async with session_factory() as session:
        used = await session.scalar(
            select(User.free_demo_used).where(User.telegram_user_id == 123456789)
        )
        order_count = await session.scalar(select(func.count(Order.id)))
        claims = (await session.scalars(select(FreeDemoClaim))).all()
    assert used is False
    assert order_count == 1
    assert len(claims) == 1
    assert claims[0].status == "RESERVED"
    assert claims[0].order_id is not None


@pytest.mark.asyncio
async def test_missing_or_already_used_demo_is_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = FreeDemoService()
    async with session_factory() as session:
        await service.claim(
            session,
            telegram_user_id=123456789,
            topic="First",
            settings={"slide_count": 8},
        )

    async with session_factory() as session:
        with pytest.raises(FreeDemoUnavailable):
            await service.claim(
                session,
                telegram_user_id=123456789,
                topic="Second",
                settings={"slide_count": 8},
            )


@pytest.mark.asyncio
async def test_reserved_demo_is_consumed_when_generation_starts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = FreeDemoService()
    async with session_factory() as session:
        order = await service.claim(
            session,
            telegram_user_id=123456789,
            topic="AI",
            settings={"slide_count": 8},
        )
    async with session_factory() as session:
        await service.consume_reserved(session, order_id=order.id)
    async with session_factory() as session:
        claim = await session.scalar(select(FreeDemoClaim))
        user = await session.scalar(select(User).where(User.telegram_user_id == 123456789))
    assert claim is not None and claim.status == "CONSUMED"
    assert user is not None and user.free_demo_used is True
