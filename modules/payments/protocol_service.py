"""Stateful Click/Payme merchant protocol application service."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.outbox.service import OutboxService
from modules.orders.domain import OrderStateMachine, OrderStatus
from modules.orders.models import Order
from modules.payments.models import Payment, PaymentEvent
from modules.payments.providers.click import ClickCommand
from modules.payments.providers.payme import PaymeCommand

PaymeResult = dict[str, Any] | list[dict[str, Any]]
PaymeHandler = Callable[[AsyncSession, PaymeCommand], Awaitable[PaymeResult]]


class PaymeProtocolError(ValueError):
    def __init__(self, code: int, message: str, data: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class PaymentProtocolService:
    def __init__(self) -> None:
        self._machine = OrderStateMachine()
        self._outbox = OutboxService()

    async def handle_click(
        self,
        session: AsyncSession,
        command: ClickCommand,
    ) -> dict[str, Any]:
        if command.operation == "PREPARE":
            return await self._click_prepare(session, command)
        return await self._click_complete(session, command)

    async def _click_prepare(
        self,
        session: AsyncSession,
        command: ClickCommand,
    ) -> dict[str, Any]:
        async with session.begin():
            order = await session.scalar(
                select(Order).where(Order.public_id == command.order_public_id).with_for_update()
            )
            if order is None:
                return self._click_response(command, error=-5, note="Order not found")
            if order.amount != command.amount_uzs or order.currency != "UZS":
                return self._click_response(command, error=-2, note="Incorrect amount")
            existing = await session.scalar(
                select(Payment).where(
                    Payment.provider == "click",
                    Payment.provider_transaction_id == command.click_trans_id,
                )
            )
            if existing is not None:
                if existing.order_id != order.id or existing.amount != command.amount_uzs:
                    return self._click_response(command, error=-6, note="Transaction conflict")
                return self._click_response(
                    command,
                    error=0,
                    note="Success",
                    merchant_prepare_id=existing.id,
                )
            current = OrderStatus(order.status)
            if current == OrderStatus.WAITING_PAYMENT:
                order.status = self._machine.transition(current, OrderStatus.PAYMENT_PROCESSING)
            elif current != OrderStatus.PAYMENT_PROCESSING:
                return self._click_response(command, error=-4, note="Order is not payable")
            payment = Payment(
                order_id=order.id,
                provider="click",
                provider_transaction_id=command.click_trans_id,
                amount=command.amount_uzs,
                currency="UZS",
                status="PREPARED",
                raw_request=self._safe_click_payload(command),
                raw_response={},
                idempotency_key=f"click:prepare:{command.click_trans_id}",
            )
            session.add(payment)
            await session.flush()
            response = self._click_response(
                command,
                error=0,
                note="Success",
                merchant_prepare_id=payment.id,
            )
            payment.raw_response = response
            session.add(
                PaymentEvent(
                    order_id=order.id,
                    payment_id=payment.id,
                    provider="click",
                    idempotency_key=f"click:prepare:{command.click_trans_id}",
                    event_type="prepare",
                    raw_request=self._safe_click_payload(command),
                )
            )
            return response

    async def _click_complete(
        self,
        session: AsyncSession,
        command: ClickCommand,
    ) -> dict[str, Any]:
        async with session.begin():
            payment = await session.scalar(
                select(Payment)
                .where(
                    Payment.provider == "click",
                    Payment.provider_transaction_id == command.click_trans_id,
                )
                .with_for_update()
            )
            if payment is None or payment.id != command.merchant_prepare_id:
                return self._click_response(command, error=-6, note="Transaction not found")
            order = await session.get(Order, payment.order_id, with_for_update=True)
            if order is None or order.public_id != command.order_public_id:
                return self._click_response(command, error=-6, note="Transaction mismatch")
            prepared_paydoc = str(payment.raw_request.get("click_paydoc_id", ""))
            if (
                payment.amount != command.amount_uzs
                or order.amount != command.amount_uzs
                or prepared_paydoc != command.click_paydoc_id
            ):
                return self._click_response(command, error=-6, note="Transaction mismatch")
            if payment.status == "PAID":
                return self._click_response(command, error=-4, note="Already paid")
            if command.provider_error < 0:
                if payment.status == "PREPARED":
                    payment.status = "CANCELLED"
                    current = OrderStatus(order.status)
                    if self._machine.can_transition(current, OrderStatus.WAITING_PAYMENT):
                        order.status = OrderStatus.WAITING_PAYMENT
                    session.add(
                        PaymentEvent(
                            order_id=order.id,
                            payment_id=payment.id,
                            provider="click",
                            idempotency_key=f"click:cancel:{command.click_trans_id}",
                            event_type="cancel",
                            raw_request=self._safe_click_payload(command),
                        )
                    )
                return self._click_response(command, error=-9, note="Transaction cancelled")
            if payment.status != "PREPARED":
                return self._click_response(command, error=-9, note="Transaction cancelled")
            current = OrderStatus(order.status)
            if current != OrderStatus.PAYMENT_PROCESSING:
                return self._click_response(command, error=-4, note="Order is not payable")
            order.status = self._machine.transition(current, OrderStatus.PAID)
            order.paid_at = datetime.now(UTC)
            payment.status = "PAID"
            order.payment_id = payment.id
            response = self._click_response(
                command,
                error=0,
                note="Success",
                merchant_confirm_id=payment.id,
            )
            payment.raw_response = response
            session.add(
                PaymentEvent(
                    order_id=order.id,
                    payment_id=payment.id,
                    provider="click",
                    idempotency_key=f"click:complete:{command.click_trans_id}",
                    event_type="paid",
                    raw_request=self._safe_click_payload(command),
                )
            )
            await self._outbox.enqueue(
                session,
                event_type="payment.paid",
                aggregate_type="order",
                aggregate_id=str(order.id),
                idempotency_key=f"payment-paid:click:{order.id}",
                payload={"order_id": order.id, "user_id": order.user_id},
            )
            return response

    async def handle_payme(
        self,
        session: AsyncSession,
        command: PaymeCommand,
    ) -> PaymeResult:
        handlers: dict[str, PaymeHandler] = {
            "CheckPerformTransaction": self._payme_check,
            "CreateTransaction": self._payme_create,
            "PerformTransaction": self._payme_perform,
            "CancelTransaction": self._payme_cancel,
            "CheckTransaction": self._payme_transaction,
            "GetStatement": self._payme_statement,
        }
        return await handlers[command.method](session, command)

    async def _payme_check(self, session: AsyncSession, command: PaymeCommand) -> dict[str, Any]:
        order, _ = await self._payme_order(session, command, lock=False)
        if OrderStatus(order.status) not in {
            OrderStatus.WAITING_PAYMENT,
            OrderStatus.PAYMENT_PROCESSING,
        }:
            raise PaymeProtocolError(-31008, "Order is not payable")
        return {"allow": True}

    async def _payme_create(self, session: AsyncSession, command: PaymeCommand) -> dict[str, Any]:
        if command.transaction_id is None or command.provider_time_ms is None:
            raise PaymeProtocolError(-32600, "Transaction id and time are required")
        async with session.begin():
            order, amount_uzs = await self._payme_order(session, command, lock=True)
            existing = await session.scalar(
                select(Payment).where(
                    Payment.provider == "payme",
                    Payment.provider_transaction_id == command.transaction_id,
                )
            )
            if existing is not None:
                if (
                    existing.order_id != order.id
                    or existing.amount != amount_uzs
                    or existing.create_time_ms != command.provider_time_ms
                ):
                    raise PaymeProtocolError(-31008, "Transaction parameters conflict")
                return self._payme_payment_result(existing)
            current = OrderStatus(order.status)
            if current == OrderStatus.WAITING_PAYMENT:
                order.status = self._machine.transition(current, OrderStatus.PAYMENT_PROCESSING)
            elif current != OrderStatus.PAYMENT_PROCESSING:
                raise PaymeProtocolError(-31008, "Order is not payable")
            payment = Payment(
                order_id=order.id,
                provider="payme",
                provider_transaction_id=command.transaction_id,
                amount=amount_uzs,
                currency="UZS",
                status="PREPARED",
                raw_request=self._safe_payme_payload(command),
                raw_response={},
                idempotency_key=f"payme:create:{command.transaction_id}",
                create_time_ms=command.provider_time_ms,
            )
            session.add(payment)
            await session.flush()
            result = self._payme_payment_result(payment)
            payment.raw_response = result
            session.add(
                PaymentEvent(
                    order_id=order.id,
                    payment_id=payment.id,
                    provider="payme",
                    idempotency_key=f"payme:create:{command.transaction_id}",
                    event_type="prepare",
                    raw_request=self._safe_payme_payload(command),
                )
            )
            return result

    async def _payme_perform(self, session: AsyncSession, command: PaymeCommand) -> dict[str, Any]:
        if command.transaction_id is None:
            raise PaymeProtocolError(-32600, "Transaction id is required")
        async with session.begin():
            payment = await self._payme_payment(session, command.transaction_id, lock=True)
            if payment.status == "PAID":
                return self._payme_payment_result(payment)
            if payment.status != "PREPARED":
                raise PaymeProtocolError(-31008, "Transaction cannot be performed")
            order = await session.get(Order, payment.order_id, with_for_update=True)
            if order is None or OrderStatus(order.status) != OrderStatus.PAYMENT_PROCESSING:
                raise PaymeProtocolError(-31008, "Order is not payable")
            now_ms = self._now_ms()
            order.status = self._machine.transition(
                OrderStatus.PAYMENT_PROCESSING,
                OrderStatus.PAID,
            )
            order.paid_at = datetime.now(UTC)
            order.payment_id = payment.id
            payment.status = "PAID"
            payment.perform_time_ms = now_ms
            session.add(
                PaymentEvent(
                    order_id=order.id,
                    payment_id=payment.id,
                    provider="payme",
                    idempotency_key=f"payme:perform:{command.transaction_id}",
                    event_type="paid",
                    raw_request=self._safe_payme_payload(command),
                )
            )
            await self._outbox.enqueue(
                session,
                event_type="payment.paid",
                aggregate_type="order",
                aggregate_id=str(order.id),
                idempotency_key=f"payment-paid:payme:{order.id}",
                payload={"order_id": order.id, "user_id": order.user_id},
            )
            return self._payme_payment_result(payment)

    async def _payme_cancel(self, session: AsyncSession, command: PaymeCommand) -> dict[str, Any]:
        if command.transaction_id is None:
            raise PaymeProtocolError(-32600, "Transaction id is required")
        async with session.begin():
            payment = await self._payme_payment(session, command.transaction_id, lock=True)
            if payment.status in {"CANCELLED", "REFUNDED"}:
                return self._payme_payment_result(payment)
            order = await session.get(Order, payment.order_id, with_for_update=True)
            if order is None:
                raise PaymeProtocolError(-31003, "Transaction not found")
            now_ms = self._now_ms()
            if payment.status == "PREPARED":
                payment.status = "CANCELLED"
                current = OrderStatus(order.status)
                if self._machine.can_transition(current, OrderStatus.WAITING_PAYMENT):
                    order.status = OrderStatus.WAITING_PAYMENT
            elif payment.status == "PAID" and OrderStatus(order.status) == OrderStatus.PAID:
                order.status = self._machine.transition(
                    OrderStatus.PAID, OrderStatus.REFUND_PENDING
                )
                order.status = self._machine.transition(
                    OrderStatus.REFUND_PENDING, OrderStatus.REFUNDED
                )
                payment.status = "REFUNDED"
            else:
                raise PaymeProtocolError(-31007, "Order is already being fulfilled")
            payment.cancel_time_ms = now_ms
            payment.cancel_reason = command.reason or 0
            session.add(
                PaymentEvent(
                    order_id=order.id,
                    payment_id=payment.id,
                    provider="payme",
                    idempotency_key=f"payme:cancel:{command.transaction_id}",
                    event_type="cancel",
                    raw_request=self._safe_payme_payload(command),
                )
            )
            return self._payme_payment_result(payment)

    async def _payme_transaction(
        self, session: AsyncSession, command: PaymeCommand
    ) -> dict[str, Any]:
        if command.transaction_id is None:
            raise PaymeProtocolError(-32600, "Transaction id is required")
        payment = await self._payme_payment(session, command.transaction_id, lock=False)
        return self._payme_payment_result(payment)

    async def _payme_statement(
        self, session: AsyncSession, command: PaymeCommand
    ) -> list[dict[str, Any]]:
        start = command.params.get("from")
        end = command.params.get("to")
        if type(start) is not int or type(end) is not int or start < 0 or end < start:
            raise PaymeProtocolError(-32600, "Invalid statement range")
        payments = (
            await session.scalars(
                select(Payment).where(
                    Payment.provider == "payme",
                    Payment.create_time_ms >= start,
                    Payment.create_time_ms <= end,
                )
            )
        ).all()
        return [self._payme_payment_result(payment) for payment in payments]

    async def _payme_order(
        self,
        session: AsyncSession,
        command: PaymeCommand,
        *,
        lock: bool,
    ) -> tuple[Order, int]:
        if command.order_public_id is None or command.amount_tiyin is None:
            raise PaymeProtocolError(-32600, "Order and amount are required")
        if command.amount_tiyin % 100 != 0:
            raise PaymeProtocolError(-31001, "Incorrect amount", "amount")
        query = select(Order).where(Order.public_id == command.order_public_id)
        if lock:
            query = query.with_for_update()
        order = await session.scalar(query)
        if order is None:
            raise PaymeProtocolError(-31050, "Order not found", "order_id")
        amount_uzs = command.amount_tiyin // 100
        if order.amount != amount_uzs or order.currency != "UZS":
            raise PaymeProtocolError(-31001, "Incorrect amount", "amount")
        return order, amount_uzs

    @staticmethod
    async def _payme_payment(
        session: AsyncSession,
        transaction_id: str,
        *,
        lock: bool,
    ) -> Payment:
        query = select(Payment).where(
            Payment.provider == "payme",
            Payment.provider_transaction_id == transaction_id,
        )
        if lock:
            query = query.with_for_update()
        payment = await session.scalar(query)
        if payment is None:
            raise PaymeProtocolError(-31003, "Transaction not found")
        return payment

    @staticmethod
    def _payme_payment_result(payment: Payment) -> dict[str, Any]:
        state = {
            "PREPARED": 1,
            "PAID": 2,
            "CANCELLED": -1,
            "REFUNDED": -2,
        }.get(payment.status, 0)
        return {
            "create_time": payment.create_time_ms or 0,
            "perform_time": payment.perform_time_ms or 0,
            "cancel_time": payment.cancel_time_ms or 0,
            "transaction": str(payment.id),
            "state": state,
            "reason": payment.cancel_reason,
        }

    @staticmethod
    def _safe_payme_payload(command: PaymeCommand) -> dict[str, Any]:
        return {"method": command.method, "params": command.params}

    @staticmethod
    def _now_ms() -> int:
        return int(datetime.now(UTC).timestamp() * 1000)

    @staticmethod
    def _safe_click_payload(command: ClickCommand) -> dict[str, str]:
        return {key: value for key, value in command.fields.items() if key not in {"sign_string"}}

    @staticmethod
    def _click_response(
        command: ClickCommand,
        *,
        error: int,
        note: str,
        merchant_prepare_id: int | None = None,
        merchant_confirm_id: int | None = None,
    ) -> dict[str, Any]:
        response: dict[str, Any] = {
            "click_trans_id": command.click_trans_id,
            "merchant_trans_id": command.order_public_id,
            "error": error,
            "error_note": note,
        }
        if command.operation == "PREPARE":
            response["merchant_prepare_id"] = merchant_prepare_id or 0
        else:
            response["merchant_confirm_id"] = merchant_confirm_id or 0
        return response
