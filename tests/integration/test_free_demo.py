import asyncio
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.database.base import Base
from modules.ai.contracts import Outline, PresentationRequest
from modules.ai.providers.base import AIProvider
from modules.orders.domain import OrderStatus
from modules.orders.models import Order
from modules.plans.models import Plan
from modules.presentations.service import PresentationService
from modules.users.models import FreeDemoClaim, User
from modules.users.service import FreeDemoService, FreeDemoUnavailable


class FailingOutlineProvider(AIProvider):
    async def generate_outline(self, request: PresentationRequest) -> Outline:
        del request
        raise TimeoutError("provider timeout")


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


@pytest.mark.asyncio
async def test_private_owner_test_is_repeatable_and_does_not_touch_demo_ledger(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        user = await session.scalar(
            select(User).where(User.telegram_user_id == 123456789).with_for_update()
        )
        assert user is not None
        user.free_demo_used = True

    service = FreeDemoService()
    for topic in ("AI ta’limda", "AI ta’limda qayta test"):
        async with session_factory() as session:
            order = await service.claim(
                session,
                telegram_user_id=123456789,
                topic=topic,
                settings={"slide_count": 10, "language": "uz"},
                private_owner_test=True,
            )
            assert order.amount == 0

    async with session_factory() as session:
        order_count = await session.scalar(select(func.count(Order.id)))
        claim_count = await session.scalar(select(func.count(FreeDemoClaim.id)))
        used = await session.scalar(
            select(User.free_demo_used).where(User.telegram_user_id == 123456789)
        )
    assert order_count == 2
    assert claim_count == 0
    assert used is True


@pytest.mark.asyncio
async def test_outline_failure_releases_reserved_demo_for_retry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        order = await FreeDemoService().claim(
            session,
            telegram_user_id=123456789,
            topic="AI ta’limda",
            settings={"slide_count": 8, "language": "uz"},
        )

    async with session_factory() as session:
        with pytest.raises(TimeoutError, match="provider timeout"):
            await PresentationService(ai_provider=FailingOutlineProvider()).generate_outline(
                session,
                order_id=order.id,
            )

    async with session_factory() as session:
        saved_order = await session.get(Order, order.id)
        claim = await session.scalar(
            select(FreeDemoClaim).where(FreeDemoClaim.order_id == order.id)
        )
        user = await session.scalar(select(User).where(User.telegram_user_id == 123456789))
    assert saved_order is not None and saved_order.status == OrderStatus.FAILED
    assert claim is not None and claim.status == "RELEASED"
    assert user is not None and user.free_demo_used is False
