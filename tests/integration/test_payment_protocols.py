import hashlib
import base64
import json
from pathlib import Path
from urllib.parse import urlencode

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import core.database.models  # noqa: F401
from core.database.base import Base
from core.outbox.models import OutboxEvent
from modules.orders.domain import OrderStatus
from modules.orders.models import Order
from modules.payments.models import Payment
from modules.payments.protocol_service import PaymentProtocolService
from modules.payments.providers.click import ClickProvider
from modules.payments.providers.payme import PaymeProvider
from modules.plans.models import Plan
from modules.users.models import User


def click_body(
    *,
    secret: str,
    action: str,
    merchant_prepare_id: int | None = None,
) -> bytes:
    fields = {
        "click_trans_id": "click-tx-1",
        "service_id": "77",
        "click_paydoc_id": "paydoc-1",
        "merchant_trans_id": "public-order",
        "amount": "3000",
        "action": action,
        "error": "0",
        "error_note": "Success",
        "sign_time": "2026-08-04 15:00:00",
    }
    pieces = ["click-tx-1", "77", secret, "public-order"]
    if action == "1":
        assert merchant_prepare_id is not None
        fields["merchant_prepare_id"] = str(merchant_prepare_id)
        pieces.append(str(merchant_prepare_id))
    pieces.extend(["3000", action, fields["sign_time"]])
    fields["sign_string"] = hashlib.md5("".join(pieces).encode()).hexdigest()  # noqa: S324
    return urlencode(fields).encode()


@pytest.mark.asyncio
async def test_click_prepare_complete_and_replay_are_idempotent(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'click.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        user = User(telegram_user_id=11, selected_language="uz")
        plan = Plan(
            code="medium",
            name="Medium",
            price_uzs=3000,
            minimum_slides=10,
            maximum_slides=15,
            priority=10,
            features={},
            is_active=True,
        )
        session.add_all([user, plan])
        await session.flush()
        order = Order(
            public_id="public-order",
            user_id=user.id,
            plan_id=plan.id,
            amount=3000,
            currency="UZS",
            status=OrderStatus.WAITING_PAYMENT,
            topic="AI",
            settings={"slide_count": 10},
        )
        session.add(order)

    provider = ClickProvider(merchant_id="merchant", service_id="77", secret_key="secret")
    service = PaymentProtocolService()
    prepare = provider.authenticate_and_decode(click_body(secret="secret", action="0"))
    async with factory() as session:
        prepared = await service.handle_click(session, prepare)
    assert prepared["error"] == 0
    prepare_id = int(prepared["merchant_prepare_id"])

    complete = provider.authenticate_and_decode(
        click_body(secret="secret", action="1", merchant_prepare_id=prepare_id)
    )
    async with factory() as session:
        completed = await service.handle_click(session, complete)
    async with factory() as session:
        replay = await service.handle_click(session, complete)
    assert completed["error"] == 0
    assert replay["error"] == -4

    async with factory() as session:
        payment = await session.scalar(select(Payment))
        saved_order = await session.scalar(select(Order))
        outbox_count = await session.scalar(select(func.count(OutboxEvent.id)))
    assert payment is not None and payment.status == "PAID"
    assert saved_order is not None and saved_order.status == OrderStatus.PAID
    assert outbox_count == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_payme_check_create_perform_and_replay_are_idempotent(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'payme.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        user = User(telegram_user_id=12, selected_language="uz")
        plan = Plan(
            code="medium",
            name="Medium",
            price_uzs=3000,
            minimum_slides=10,
            maximum_slides=15,
            priority=10,
            features={},
            is_active=True,
        )
        session.add_all([user, plan])
        await session.flush()
        session.add(
            Order(
                public_id="payme-order",
                user_id=user.id,
                plan_id=plan.id,
                amount=3000,
                currency="UZS",
                status=OrderStatus.WAITING_PAYMENT,
                topic="AI",
                settings={"slide_count": 10},
            )
        )

    provider = PaymeProvider(merchant_id="merchant", login="Paycom", secret_key="secret")
    auth = "Basic " + base64.b64encode(b"Paycom:secret").decode()

    def command(method: str, params: dict) -> object:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        return provider.authenticate_and_decode(body, {"authorization": auth})

    service = PaymentProtocolService()
    account = {"order_id": "payme-order"}
    async with factory() as session:
        check = await service.handle_payme(
            session,
            command("CheckPerformTransaction", {"amount": 300000, "account": account}),  # type: ignore[arg-type]
        )
    assert check == {"allow": True}
    create_command = command(
        "CreateTransaction",
        {"id": "payme-tx-1", "time": 1000, "amount": 300000, "account": account},
    )
    async with factory() as session:
        created = await service.handle_payme(session, create_command)  # type: ignore[arg-type]
    assert created["state"] == 1  # type: ignore[index]
    perform_command = command("PerformTransaction", {"id": "payme-tx-1"})
    async with factory() as session:
        performed = await service.handle_payme(session, perform_command)  # type: ignore[arg-type]
    async with factory() as session:
        replay = await service.handle_payme(session, perform_command)  # type: ignore[arg-type]
    assert performed["state"] == 2  # type: ignore[index]
    assert replay["perform_time"] == performed["perform_time"]  # type: ignore[index]
    async with factory() as session:
        outbox_count = await session.scalar(select(func.count(OutboxEvent.id)))
        payment = await session.scalar(select(Payment))
    assert outbox_count == 1
    assert payment is not None and payment.status == "PAID"
    await engine.dispose()
