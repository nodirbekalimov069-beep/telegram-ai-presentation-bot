from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import core.database.models  # noqa: F401
from core.database.base import Base
from modules.files.storage import LocalStorage
from modules.orders.domain import OrderStatus
from modules.orders.models import Order
from modules.plans.models import Plan
from modules.presentations.delivery import (
    DeliveryInProgress,
    TelegramArtifactDelivery,
    mark_terminal_delivery_timeout,
)
from modules.presentations.models import GenerationJob, Presentation
from modules.users.models import User


class RecordingBot:
    def __init__(self, *, fail_pdf: bool = False, first_message_id: int = 100) -> None:
        self.fail_pdf = fail_pdf
        self.next_message_id = first_message_id
        self.filenames: list[str] = []

    async def send_document(
        self,
        *,
        chat_id: int,
        document: Any,
        caption: str,
    ) -> object:
        del chat_id, caption
        filename = str(document.filename)
        self.filenames.append(filename)
        if self.fail_pdf and filename.endswith(".pdf"):
            raise ConnectionError("Telegram temporarily unavailable")
        self.next_message_id += 1
        return SimpleNamespace(message_id=self.next_message_id)


@pytest.mark.asyncio
async def test_delivery_retries_only_the_missing_telegram_file(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'delivery.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    storage = LocalStorage(tmp_path / "storage")
    pptx_source = tmp_path / "source.pptx"
    pdf_source = tmp_path / "source.pdf"
    pptx_source.write_bytes(b"pptx")
    pdf_source.write_bytes(b"pdf")

    async with factory() as session, session.begin():
        user = User(telegram_user_id=777, username="nodir", selected_language="uz")
        plan = Plan(
            code="free_demo",
            name="Free Demo",
            price_uzs=0,
            minimum_slides=1,
            maximum_slides=8,
            priority=0,
            features={},
            is_active=True,
        )
        session.add_all([user, plan])
        await session.flush()
        order = Order(
            public_id="delivery-order",
            user_id=user.id,
            plan_id=plan.id,
            amount=0,
            currency="UZS",
            status=OrderStatus.COMPLETED,
            topic="Sun’iy intellekt",
            settings={"slide_count": 2},
        )
        session.add(order)
        await session.flush()
        presentation = Presentation(
            order_id=order.id,
            user_id=user.id,
            title="Sun’iy intellekt",
            topic="Sun’iy intellekt",
            language="uz",
            direction="Texnologiya",
            audience="Talabalar",
            slide_count=2,
            design_style="minimal",
            outline={},
            content={},
            status=OrderStatus.COMPLETED,
        )
        session.add(presentation)
        await session.flush()
        presentation.pptx_storage_key = await storage.put_file(
            pptx_source,
            user_id=user.id,
            presentation_id=presentation.id,
            artifact_type="pptx",
        )
        presentation.pdf_storage_key = await storage.put_file(
            pdf_source,
            user_id=user.id,
            presentation_id=presentation.id,
            artifact_type="pdf",
        )
        session.add(
            GenerationJob(
                presentation_id=presentation.id,
                status="ARTIFACTS_READY",
                progress=90,
                current_step="delivery_pending",
                retries=0,
                priority=0,
            )
        )
        presentation_id = presentation.id

    delivery = TelegramArtifactDelivery(storage=storage, work_root=tmp_path / "delivery-work")
    first_bot = RecordingBot(fail_pdf=True)
    with pytest.raises(ConnectionError):
        await delivery.deliver(factory, presentation_id=presentation_id, bot=first_bot)

    async with factory() as session:
        after_failure = await session.get(Presentation, presentation_id)
    assert after_failure is not None
    assert after_failure.pptx_telegram_message_id == 101
    assert after_failure.pdf_telegram_message_id is None
    assert after_failure.delivery_status == "FAILED"
    assert first_bot.filenames == ["Suniy_intellekt.pptx", "Suniy_intellekt.pdf"]

    retry_bot = RecordingBot(first_message_id=200)
    result = await delivery.deliver(factory, presentation_id=presentation_id, bot=retry_bot)
    assert result.delivered
    assert retry_bot.filenames == ["Suniy_intellekt.pdf"]

    no_op_bot = RecordingBot(first_message_id=300)
    duplicate = await delivery.deliver(factory, presentation_id=presentation_id, bot=no_op_bot)
    assert duplicate.delivered
    assert no_op_bot.filenames == []

    async with factory() as session:
        saved = await session.get(Presentation, presentation_id)
        job = await session.get(GenerationJob, 1)
    assert saved is not None
    assert saved.delivery_status == "DELIVERED"
    assert saved.pptx_telegram_message_id == 101
    assert saved.pdf_telegram_message_id == 201
    assert saved.delivered_at is not None
    assert job is not None and job.progress == 100 and job.current_step == "completed"

    async with factory() as session, session.begin():
        saved = await session.get(Presentation, presentation_id, with_for_update=True)
        assert saved is not None
        saved.delivery_status = "SENDING"
        saved.delivery_lease_token = "existing-delivery"
        saved.delivery_lease_expires_at = datetime.now(UTC) + timedelta(minutes=2)

    active_lease_bot = RecordingBot(first_message_id=400)
    with pytest.raises(DeliveryInProgress) as raised:
        await delivery.deliver(factory, presentation_id=presentation_id, bot=active_lease_bot)
    assert 100_000 <= raised.value.retry_after_ms <= 125_000
    assert active_lease_bot.filenames == []

    active_timeout = await mark_terminal_delivery_timeout(
        factory,
        presentation_id=presentation_id,
        error=TimeoutError("delivery timed out"),
    )
    assert active_timeout is False
    async with factory() as session, session.begin():
        saved = await session.get(Presentation, presentation_id, with_for_update=True)
        assert saved is not None and saved.delivery_status == "SENDING"
        saved.delivery_lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    expired_timeout = await mark_terminal_delivery_timeout(
        factory,
        presentation_id=presentation_id,
        error=TimeoutError("delivery timed out"),
    )
    assert expired_timeout is True
    async with factory() as session:
        saved = await session.get(Presentation, presentation_id)
        job = await session.get(GenerationJob, 1)
    assert saved is not None and saved.delivery_status == "FAILED"
    assert saved.delivery_lease_token is None and saved.delivery_lease_expires_at is None
    assert job is not None and job.status == "DELIVERY_FAILED"
    assert job.current_step == "delivery_failed"
    await engine.dispose()
