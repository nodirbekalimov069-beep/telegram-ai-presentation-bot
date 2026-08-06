import pytest

from modules.orders.domain import InvalidStateTransition, OrderStateMachine, OrderStatus


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (OrderStatus.DRAFT, OrderStatus.CREATED),
        (OrderStatus.CREATED, OrderStatus.WAITING_PAYMENT),
        (OrderStatus.CREATED, OrderStatus.OUTLINE_GENERATING),
        (OrderStatus.WAITING_PAYMENT, OrderStatus.PAYMENT_PROCESSING),
        (OrderStatus.PAYMENT_PROCESSING, OrderStatus.PAID),
        (OrderStatus.PAID, OrderStatus.OUTLINE_GENERATING),
        (OrderStatus.OUTLINE_GENERATING, OrderStatus.WAITING_OUTLINE_APPROVAL),
        (OrderStatus.WAITING_OUTLINE_APPROVAL, OrderStatus.QUEUED),
        (OrderStatus.QUEUED, OrderStatus.GENERATING),
        (OrderStatus.GENERATING, OrderStatus.QUALITY_CHECK),
        (OrderStatus.QUALITY_CHECK, OrderStatus.COMPLETED),
        (OrderStatus.COMPLETED, OrderStatus.REFUND_PENDING),
        (OrderStatus.REFUND_PENDING, OrderStatus.REFUNDED),
    ],
)
def test_allowed_order_state_transition(current: OrderStatus, target: OrderStatus) -> None:
    machine = OrderStateMachine()

    assert machine.transition(current, target) is target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (OrderStatus.DRAFT, OrderStatus.COMPLETED),
        (OrderStatus.WAITING_PAYMENT, OrderStatus.GENERATING),
        (OrderStatus.PAID, OrderStatus.COMPLETED),
        (OrderStatus.COMPLETED, OrderStatus.GENERATING),
        (OrderStatus.REFUNDED, OrderStatus.PAID),
        (OrderStatus.CANCELLED, OrderStatus.CREATED),
    ],
)
def test_invalid_order_state_transition_is_blocked(
    current: OrderStatus,
    target: OrderStatus,
) -> None:
    machine = OrderStateMachine()

    with pytest.raises(InvalidStateTransition, match=f"{current.value} -> {target.value}"):
        machine.transition(current, target)


def test_retry_from_failed_returns_to_queue() -> None:
    machine = OrderStateMachine()

    assert machine.transition(OrderStatus.FAILED, OrderStatus.QUEUED) is OrderStatus.QUEUED
