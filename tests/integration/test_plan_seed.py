from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.database.base import Base
from core.database.models import Plan
from modules.plans.seed import seed_default_plans


@pytest.mark.asyncio
async def test_default_plan_seed_is_idempotent_and_has_required_prices(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'plans.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        await seed_default_plans(session)
    async with factory() as session:
        await seed_default_plans(session)
    async with factory() as session:
        plans = (await session.scalars(select(Plan).order_by(Plan.price_uzs))).all()

    assert [
        (plan.code, plan.price_uzs, plan.minimum_slides, plan.maximum_slides) for plan in plans
    ] == [
        ("free_demo", 0, 1, 8),
        ("medium", 3_000, 10, 15),
        ("premium", 5_000, 15, 20),
        ("premium_plus", 7_990, 20, 30),
    ]
    await engine.dispose()
