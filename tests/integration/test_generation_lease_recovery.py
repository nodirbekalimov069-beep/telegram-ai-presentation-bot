from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import core.database.models  # noqa: F401
from core.database.base import Base
from modules.files.storage import LocalStorage
from modules.orders.domain import OrderStatus
from modules.orders.models import Order
from modules.plans.models import Plan
from modules.presentations.engine import NodePresentationRenderer
from modules.presentations.generation import GenerationLeaseActive, GenerationPipeline
from modules.presentations.models import GenerationJob, Presentation
from modules.users.models import User


class MustNotRender:
    async def render(self, payload: dict[str, object], output_path: Path) -> Path:
        raise AssertionError("active lease must not start a second renderer")


class MustNotConvert:
    async def convert(self, pptx_path: Path, output_pdf: Path) -> Path:
        raise AssertionError("active lease must not start PDF conversion")


@pytest.mark.asyncio
async def test_active_generation_lease_requests_delayed_retry_instead_of_acknowledgement(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'lease.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    lease_expires_at = datetime.now(UTC) + timedelta(minutes=2)

    async with factory() as session, session.begin():
        user = User(telegram_user_id=77, selected_language="uz")
        plan = Plan(
            code="free_demo",
            name="Free Demo",
            price_uzs=0,
            minimum_slides=1,
            maximum_slides=8,
            priority=1,
            features={},
            is_active=True,
        )
        session.add_all([user, plan])
        await session.flush()
        order = Order(
            public_id="lease-order",
            user_id=user.id,
            plan_id=plan.id,
            amount=0,
            currency="UZS",
            status=OrderStatus.QUEUED,
            topic="AI",
            settings={"slide_count": 1},
        )
        session.add(order)
        await session.flush()
        presentation = Presentation(
            order_id=order.id,
            user_id=user.id,
            title="AI",
            topic="AI",
            language="uz",
            direction="Ta’lim",
            audience="Talabalar",
            slide_count=1,
            design_style="minimal",
            outline={},
            content={},
            status=OrderStatus.QUEUED,
        )
        session.add(presentation)
        await session.flush()
        session.add(
            GenerationJob(
                presentation_id=presentation.id,
                status="RUNNING",
                progress=30,
                current_step="rendering_pptx",
                retries=0,
                priority=1,
                lease_token="existing-worker",
                lease_expires_at=lease_expires_at,
            )
        )
        presentation_id = presentation.id

    pipeline = GenerationPipeline(
        renderer=cast(NodePresentationRenderer, MustNotRender()),
        pdf_converter=MustNotConvert(),
        storage=LocalStorage(tmp_path / "storage"),
        work_root=tmp_path / "work",
    )

    with pytest.raises(GenerationLeaseActive) as raised:
        await pipeline.run(factory, presentation_id=presentation_id)

    assert 100_000 <= raised.value.retry_after_ms <= 125_000
    await engine.dispose()
