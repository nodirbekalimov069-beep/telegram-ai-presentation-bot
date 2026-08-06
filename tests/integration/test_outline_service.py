from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.database.base import Base
import core.database.models  # noqa: F401
from core.outbox.models import OutboxEvent
from modules.ai.providers.mock import MockAIProvider
from modules.orders.domain import OrderStatus
from modules.orders.models import Order
from modules.plans.models import Plan
from modules.plans.seed import seed_default_plans
from modules.presentations.models import GenerationJob, Presentation
from modules.presentations.service import PresentationService
from modules.users.models import User


@pytest.mark.asyncio
async def test_outline_generation_and_approval_enqueue_one_job(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'outline.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await seed_default_plans(session)
    async with factory() as session, session.begin():
        user = User(telegram_user_id=555, selected_language="uz")
        session.add(user)
        await session.flush()
        plan = await session.scalar(select(Plan).where(Plan.code == "premium"))
        assert plan is not None
        order = Order(
            user_id=user.id,
            plan_id=plan.id,
            amount=plan.price_uzs,
            currency="UZS",
            status=OrderStatus.PAID,
            topic="AI va ta’lim",
            settings={
                "language": "uz",
                "direction": "Ta’lim",
                "purpose": "Talabalarga tushuntirish",
                "audience": "Universitet talabalari",
                "knowledge_level": "beginner",
                "slide_count": 15,
                "design_style": "ai",
            },
        )
        session.add(order)
        await session.flush()
        order_id = order.id

    service = PresentationService(ai_provider=MockAIProvider())
    async with factory() as session:
        presentation = await service.generate_outline(session, order_id=order_id)

    assert presentation.status == OrderStatus.WAITING_OUTLINE_APPROVAL
    assert len(presentation.outline["slides"]) == 15

    async with factory() as session:
        job = await service.approve_outline(session, presentation_id=presentation.id)
    async with factory() as session:
        duplicate_job = await service.approve_outline(session, presentation_id=presentation.id)
    async with factory() as session:
        saved_order = await session.get(Order, order_id)
        saved_presentation = await session.get(Presentation, presentation.id)
        jobs = (await session.scalars(select(GenerationJob))).all()
        events = (await session.scalars(select(OutboxEvent))).all()

    assert saved_order is not None and saved_order.status == OrderStatus.QUEUED
    assert saved_presentation is not None and saved_presentation.status == OrderStatus.QUEUED
    assert job.presentation_id == presentation.id
    assert len(jobs) == 1
    assert duplicate_job.id == job.id
    assert len(events) == 1
    await engine.dispose()
