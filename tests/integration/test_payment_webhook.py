import hashlib
import hmac
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.database.base import Base
from modules.orders.domain import OrderStatus
from modules.orders.models import Order
from modules.payments.models import Payment, PaymentEvent
from modules.payments.providers.mock import MockPaymentProvider
from modules.payments.service import PaymentAmountMismatch, PaymentService, WebhookSignatureError
from modules.plans.models import Plan
from modules.users.models import User

SECRET = "test-payment-secret"


@pytest.fixture
async def payment_context(tmp_path: Path) -> tuple[async_sessionmaker[AsyncSession], str]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'payment.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        user = User(telegram_user_id=777, selected_language="uz")
        plan = Plan(
            code="medium",
            name="Medium",
            price_uzs=3_000,
            minimum_slides=10,
            maximum_slides=15,
            priority=10,
            features={},
            is_active=True,
        )
        session.add_all([user, plan])
        await session.flush()
        order = Order(
            user_id=user.id,
            plan_id=plan.id,
            amount=3_000,
            currency="UZS",
            status=OrderStatus.WAITING_PAYMENT,
            topic="Test",
            settings={"slide_count": 10},
        )
        session.add(order)
        await session.flush()
        public_id = order.public_id
    yield factory, public_id
    await engine.dispose()


def signed_payload(public_id: str, *, amount: int = 3_000) -> tuple[bytes, dict[str, str]]:
    payload = json.dumps(
        {
            "event_id": "evt-001",
            "transaction_id": "txn-001",
            "order_public_id": public_id,
            "amount": amount,
            "currency": "UZS",
            "status": "paid",
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    signature = hmac.new(SECRET.encode(), payload, hashlib.sha256).hexdigest()
    return payload, {"X-Mock-Signature": signature}


@pytest.mark.asyncio
async def test_paid_webhook_is_idempotent(
    payment_context: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, public_id = payment_context
    provider = MockPaymentProvider(secret=SECRET)
    service = PaymentService()
    payload, headers = signed_payload(public_id)

    async with factory() as session:
        first = await service.handle_webhook(session, provider, payload, headers)
    async with factory() as session:
        second = await service.handle_webhook(session, provider, payload, headers)

    assert first.applied is True
    assert second.applied is False
    assert second.duplicate is True
    async with factory() as session:
        status = await session.scalar(select(Order.status).where(Order.public_id == public_id))
        payment_count = await session.scalar(select(func.count(Payment.id)))
        event_count = await session.scalar(select(func.count(PaymentEvent.id)))
    assert status == OrderStatus.PAID
    assert payment_count == 1
    assert event_count == 1


@pytest.mark.asyncio
async def test_webhook_with_invalid_signature_is_rejected(
    payment_context: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, public_id = payment_context
    payload, _ = signed_payload(public_id)

    async with factory() as session:
        with pytest.raises(WebhookSignatureError):
            await PaymentService().handle_webhook(
                session,
                MockPaymentProvider(secret=SECRET),
                payload,
                {"X-Mock-Signature": "tampered"},
            )


@pytest.mark.asyncio
async def test_server_side_amount_mismatch_is_rejected(
    payment_context: tuple[async_sessionmaker[AsyncSession], str],
) -> None:
    factory, public_id = payment_context
    payload, headers = signed_payload(public_id, amount=1)

    async with factory() as session:
        with pytest.raises(PaymentAmountMismatch):
            await PaymentService().handle_webhook(
                session,
                MockPaymentProvider(secret=SECRET),
                payload,
                headers,
            )
    async with factory() as session:
        status = await session.scalar(select(Order.status).where(Order.public_id == public_id))
        payment_count = await session.scalar(select(func.count(Payment.id)))
    assert status == OrderStatus.WAITING_PAYMENT
    assert payment_count == 0
