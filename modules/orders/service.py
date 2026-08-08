"""Order creation service with server-authoritative tariff pricing."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.orders.domain import OrderStateMachine, OrderStatus
from modules.orders.models import Order
from modules.plans.domain import PlanPolicy
from modules.plans.models import Plan
from modules.users.models import User


class PlanNotAvailable(LookupError):
    pass


class UserNotAllowed(PermissionError):
    pass


class OrderService:
    def __init__(self) -> None:
        self._machine = OrderStateMachine()

    async def create_paid_order(
        self,
        session: AsyncSession,
        *,
        user_id: int | None = None,
        telegram_user_id: int | None = None,
        plan_code: str,
        topic: str,
        settings: dict[str, Any],
    ) -> Order:
        if len(topic.strip()) < 2 or len(topic) > 500:
            raise ValueError("topic length must be between 2 and 500 characters")
        if (user_id is None) == (telegram_user_id is None):
            raise ValueError("exactly one user identity is required")
        async with session.begin():
            user_filter = (
                User.id == user_id
                if user_id is not None
                else User.telegram_user_id == telegram_user_id
            )
            user = await session.scalar(select(User).where(user_filter))
            if user is None or user.is_blocked:
                raise UserNotAllowed("user is missing or blocked")
            plan = await session.scalar(
                select(Plan).where(Plan.code == plan_code, Plan.is_active.is_(True))
            )
            if plan is None or plan.price_uzs <= 0:
                raise PlanNotAvailable(plan_code)
            policy = PlanPolicy(
                code=plan.code,
                price_uzs=plan.price_uzs,
                minimum_slides=plan.minimum_slides,
                maximum_slides=plan.maximum_slides,
                priority=plan.priority,
            )
            policy.validate_slide_count(int(settings.get("slide_count", 0)))
            order = Order(
                user_id=user.id,
                plan_id=plan.id,
                amount=plan.price_uzs,
                currency="UZS",
                status=OrderStatus.WAITING_PAYMENT,
                topic=topic.strip(),
                settings=settings,
            )
            session.add(order)
            await session.flush()
        return order

    async def cancel_waiting_payment(
        self,
        session: AsyncSession,
        *,
        order_id: int,
        telegram_user_id: int,
    ) -> bool:
        async with session.begin():
            owner_id = (
                select(User.id).where(User.telegram_user_id == telegram_user_id).scalar_subquery()
            )
            order = await session.scalar(
                select(Order)
                .where(Order.id == order_id, Order.user_id == owner_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if order is None:
                raise LookupError("order not found")
            current = OrderStatus(order.status)
            if current == OrderStatus.CANCELLED:
                return False
            if current != OrderStatus.WAITING_PAYMENT:
                raise ValueError("payment is no longer cancellable")
            order.status = self._machine.transition(current, OrderStatus.CANCELLED)
            return True
