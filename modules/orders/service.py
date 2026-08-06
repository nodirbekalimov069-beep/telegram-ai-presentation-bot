"""Order creation service with server-authoritative tariff pricing."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modules.orders.domain import OrderStatus
from modules.orders.models import Order
from modules.plans.domain import PlanPolicy
from modules.plans.models import Plan
from modules.users.models import User


class PlanNotAvailable(LookupError):
    pass


class UserNotAllowed(PermissionError):
    pass


class OrderService:
    async def create_paid_order(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        plan_code: str,
        topic: str,
        settings: dict[str, Any],
    ) -> Order:
        if len(topic.strip()) < 3 or len(topic) > 500:
            raise ValueError("topic length must be between 3 and 500 characters")
        async with session.begin():
            user = await session.scalar(select(User).where(User.id == user_id))
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
