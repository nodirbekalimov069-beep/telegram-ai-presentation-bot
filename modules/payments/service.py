"""Payment webhook verification and idempotent application service."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.outbox.service import OutboxService
from modules.orders.domain import InvalidStateTransition, OrderStateMachine, OrderStatus
from modules.orders.models import Order
from modules.payments.models import Payment, PaymentEvent
from modules.payments.providers.base import PaymentProvider
from modules.payments.providers.mock import InvalidMockWebhookSignature


class WebhookSignatureError(ValueError):
    pass


class PaymentAmountMismatch(ValueError):
    pass


class PaymentOrderNotFound(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class WebhookApplicationResult:
    applied: bool
    duplicate: bool
    order_id: int | None


class PaymentService:
    def __init__(self) -> None:
        self._state_machine = OrderStateMachine()
        self._outbox = OutboxService()

    async def handle_webhook(
        self,
        session: AsyncSession,
        provider: PaymentProvider,
        body: bytes,
        headers: Mapping[str, str],
    ) -> WebhookApplicationResult:
        try:
            webhook = provider.parse_webhook(body, headers)
        except (InvalidMockWebhookSignature, KeyError, TypeError, ValueError) as exc:
            raise WebhookSignatureError("webhook signature or payload is invalid") from exc

        try:
            async with session.begin():
                existing_event_id = await session.scalar(
                    select(PaymentEvent.id).where(
                        PaymentEvent.provider == provider.name,
                        PaymentEvent.idempotency_key == webhook.idempotency_key,
                    )
                )
                if existing_event_id is not None:
                    return WebhookApplicationResult(False, True, None)

                order = await session.scalar(
                    select(Order)
                    .where(Order.public_id == webhook.order_public_id)
                    .with_for_update()
                )
                if order is None:
                    raise PaymentOrderNotFound(webhook.order_public_id)
                if order.amount != webhook.amount or order.currency != webhook.currency:
                    raise PaymentAmountMismatch(
                        "provider amount/currency does not match server order"
                    )
                if webhook.status.lower() != "paid":
                    raise ValueError("only a verified paid event can complete payment")

                current = OrderStatus(order.status)
                if current is OrderStatus.WAITING_PAYMENT:
                    order.status = self._state_machine.transition(
                        current,
                        OrderStatus.PAYMENT_PROCESSING,
                    )
                    current = OrderStatus.PAYMENT_PROCESSING
                try:
                    order.status = self._state_machine.transition(current, OrderStatus.PAID)
                except InvalidStateTransition as exc:
                    raise ValueError("order is not payable in its current state") from exc
                order.paid_at = datetime.now(UTC)

                payment = Payment(
                    order_id=order.id,
                    provider=provider.name,
                    provider_transaction_id=webhook.provider_transaction_id,
                    amount=webhook.amount,
                    currency=webhook.currency,
                    status="PAID",
                    raw_request=webhook.raw_payload,
                    raw_response={"accepted": True},
                    idempotency_key=webhook.idempotency_key,
                )
                session.add(payment)
                await session.flush()
                session.add(
                    PaymentEvent(
                        order_id=order.id,
                        payment_id=payment.id,
                        provider=provider.name,
                        idempotency_key=webhook.idempotency_key,
                        event_type="paid",
                        raw_request=webhook.raw_payload,
                    )
                )
                order.payment_id = payment.id
                await self._outbox.enqueue(
                    session,
                    event_type="payment.paid",
                    aggregate_type="order",
                    aggregate_id=str(order.id),
                    idempotency_key=f"payment-paid:{provider.name}:{order.id}",
                    payload={"order_id": order.id, "user_id": order.user_id},
                )
                order_id = order.id
            return WebhookApplicationResult(True, False, order_id)
        except IntegrityError:
            await session.rollback()
            return WebhookApplicationResult(False, True, None)
