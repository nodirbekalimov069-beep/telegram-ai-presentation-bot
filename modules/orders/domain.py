"""Explicit order lifecycle state machine."""

from enum import StrEnum


class OrderStatus(StrEnum):
    DRAFT = "DRAFT"
    CREATED = "CREATED"
    WAITING_PAYMENT = "WAITING_PAYMENT"
    PAYMENT_PROCESSING = "PAYMENT_PROCESSING"
    PAID = "PAID"
    OUTLINE_GENERATING = "OUTLINE_GENERATING"
    WAITING_OUTLINE_APPROVAL = "WAITING_OUTLINE_APPROVAL"
    QUEUED = "QUEUED"
    GENERATING = "GENERATING"
    QUALITY_CHECK = "QUALITY_CHECK"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    REFUND_PENDING = "REFUND_PENDING"
    REFUNDED = "REFUNDED"


class InvalidStateTransition(ValueError):
    """Raised when an order tries to skip or reverse its lifecycle."""


class OrderStateMachine:
    _allowed: dict[OrderStatus, frozenset[OrderStatus]] = {
        OrderStatus.DRAFT: frozenset({OrderStatus.CREATED, OrderStatus.CANCELLED}),
        OrderStatus.CREATED: frozenset(
            {
                OrderStatus.WAITING_PAYMENT,
                OrderStatus.OUTLINE_GENERATING,
                OrderStatus.CANCELLED,
            }
        ),
        OrderStatus.WAITING_PAYMENT: frozenset(
            {OrderStatus.PAYMENT_PROCESSING, OrderStatus.CANCELLED}
        ),
        OrderStatus.PAYMENT_PROCESSING: frozenset(
            {OrderStatus.PAID, OrderStatus.WAITING_PAYMENT, OrderStatus.FAILED}
        ),
        OrderStatus.PAID: frozenset({OrderStatus.OUTLINE_GENERATING, OrderStatus.REFUND_PENDING}),
        OrderStatus.OUTLINE_GENERATING: frozenset(
            {OrderStatus.WAITING_OUTLINE_APPROVAL, OrderStatus.FAILED}
        ),
        OrderStatus.WAITING_OUTLINE_APPROVAL: frozenset(
            {OrderStatus.OUTLINE_GENERATING, OrderStatus.QUEUED, OrderStatus.CANCELLED}
        ),
        OrderStatus.QUEUED: frozenset(
            {OrderStatus.GENERATING, OrderStatus.CANCELLED, OrderStatus.FAILED}
        ),
        OrderStatus.GENERATING: frozenset({OrderStatus.QUALITY_CHECK, OrderStatus.FAILED}),
        OrderStatus.QUALITY_CHECK: frozenset(
            {OrderStatus.COMPLETED, OrderStatus.GENERATING, OrderStatus.FAILED}
        ),
        OrderStatus.COMPLETED: frozenset({OrderStatus.REFUND_PENDING}),
        OrderStatus.FAILED: frozenset(
            {OrderStatus.QUEUED, OrderStatus.CANCELLED, OrderStatus.REFUND_PENDING}
        ),
        OrderStatus.CANCELLED: frozenset(),
        OrderStatus.REFUND_PENDING: frozenset({OrderStatus.REFUNDED, OrderStatus.FAILED}),
        OrderStatus.REFUNDED: frozenset(),
    }

    def can_transition(self, current: OrderStatus, target: OrderStatus) -> bool:
        return target in self._allowed[current]

    def transition(self, current: OrderStatus, target: OrderStatus) -> OrderStatus:
        if not self.can_transition(current, target):
            raise InvalidStateTransition(
                f"invalid order transition: {current.value} -> {target.value}"
            )
        return target
