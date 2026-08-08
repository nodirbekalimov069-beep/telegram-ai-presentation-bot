from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apps.api.routes.admin import retry_presentation
from core.database.base import Base
import core.database.models  # noqa: F401
from core.outbox.models import OutboxEvent
from modules.orders.domain import OrderStatus
from modules.orders.models import Order
from modules.plans.models import Plan
from modules.plans.seed import seed_default_plans
from modules.presentations.models import GenerationJob, Presentation
from modules.users.models import User


@pytest.mark.parametrize(
    ("delivery_status", "job_status", "lease_token", "lease_expires_at", "should_retry"),
    [
        ("FAILED", "DELIVERY_FAILED", None, None, True),
        (
            "SENDING",
            "ARTIFACTS_READY",
            "stale-delivery-lease",
            datetime.now(UTC) - timedelta(minutes=1),
            True,
        ),
        (
            "SENDING",
            "ARTIFACTS_READY",
            "active-delivery-lease",
            datetime.now(UTC) + timedelta(minutes=5),
            False,
        ),
    ],
    ids=["recorded-failure", "expired-sending-lease", "active-sending-lease"],
)
@pytest.mark.asyncio
async def test_admin_can_requeue_delivery_after_worker_retries_are_exhausted(
    tmp_path: Path,
    delivery_status: str,
    job_status: str,
    lease_token: str | None,
    lease_expires_at: datetime | None,
    should_retry: bool,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'admin-retry.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await seed_default_plans(session)
    async with factory() as session, session.begin():
        user = User(telegram_user_id=42, selected_language="uz")
        session.add(user)
        await session.flush()
        plan = await session.scalar(select(Plan).where(Plan.code == "premium"))
        assert plan is not None
        order = Order(
            user_id=user.id,
            plan_id=plan.id,
            amount=plan.price_uzs,
            currency="UZS",
            status=OrderStatus.COMPLETED,
            topic="AI va ta’lim",
            settings={"slide_count": 10},
        )
        session.add(order)
        await session.flush()
        presentation = Presentation(
            order_id=order.id,
            user_id=user.id,
            title="AI va ta’lim",
            topic="AI va ta’lim",
            language="uz",
            direction="Ta’lim",
            audience="Talabalar",
            slide_count=10,
            design_style="ai",
            outline={},
            content={},
            status=OrderStatus.COMPLETED,
            pptx_storage_key="users/1/presentation.pptx",
            pdf_storage_key="users/1/presentation.pdf",
            delivery_status=delivery_status,
            delivery_attempts=4,
            delivery_lease_token=lease_token,
            delivery_lease_expires_at=lease_expires_at,
            pptx_telegram_message_id=101,
            delivery_error=(
                "TimeoutError: delivery failed" if delivery_status == "FAILED" else None
            ),
        )
        session.add(presentation)
        await session.flush()
        session.add(
            GenerationJob(
                presentation_id=presentation.id,
                status=job_status,
                progress=90,
                current_step=(
                    "delivery_failed" if job_status == "DELIVERY_FAILED" else "delivery_pending"
                ),
                retries=0,
                priority=plan.priority,
                error_message="TimeoutError: delivery failed",
            )
        )
        presentation_id = presentation.id

    async with factory() as session:
        if not should_retry:
            with pytest.raises(HTTPException) as exc_info:
                await retry_presentation(
                    presentation_id,
                    Request({"type": "http", "headers": []}),
                    session,
                    admin_id=999,
                )
            assert exc_info.value.status_code == 409
            result = None
        else:
            result = await retry_presentation(
                presentation_id,
                Request({"type": "http", "headers": []}),
                session,
                admin_id=999,
            )

    if not should_retry:
        await engine.dispose()
        return

    assert result == {"presentation_id": presentation_id, "status": "DELIVERY_QUEUED"}
    async with factory() as session:
        saved = await session.get(Presentation, presentation_id)
        saved_order = await session.get(Order, saved.order_id) if saved else None
        job = await session.scalar(
            select(GenerationJob).where(GenerationJob.presentation_id == presentation_id)
        )
        event = await session.scalar(
            select(OutboxEvent).where(OutboxEvent.aggregate_id == str(presentation_id))
        )

    assert saved is not None
    assert saved.status == OrderStatus.COMPLETED
    assert saved.delivery_status == "PENDING"
    assert saved.delivery_error is None
    assert saved.delivery_lease_token is None
    assert saved.delivery_lease_expires_at is None
    assert saved.pptx_telegram_message_id == 101
    assert saved_order is not None and saved_order.status == OrderStatus.COMPLETED
    assert job is not None and job.status == "ARTIFACTS_READY"
    assert job.current_step == "delivery_pending"
    assert event is not None and event.event_type == "generation.requested"
    await engine.dispose()
