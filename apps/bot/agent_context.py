"""Build server-authoritative context for presentation-agent turns."""

from modules.ai.presentation_agent import AgentBackendContext, AgentPlan, PaymentStatus
from modules.orders.models import Order
from modules.plans.models import Plan
from modules.presentations.models import Presentation
from modules.users.models import User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_PAID_LIFECYCLE_STATES = {
    "PAID",
    "OUTLINE_GENERATING",
    "WAITING_OUTLINE_APPROVAL",
    "QUEUED",
    "GENERATING",
    "QUALITY_CHECK",
    "COMPLETED",
}
_DIRECT_PAYMENT_STATES: set[PaymentStatus] = {
    "WAITING_PAYMENT",
    "PAYMENT_PROCESSING",
    "FAILED",
    "CANCELLED",
    "REFUNDED",
}


def payment_status_for_order(*, status: str, amount: int) -> PaymentStatus | None:
    if amount == 0 and status not in {"FAILED", "CANCELLED", "REFUNDED"}:
        return "NOT_REQUIRED"
    if status in _PAID_LIFECYCLE_STATES:
        return "PAID"
    if status in _DIRECT_PAYMENT_STATES:
        return status
    return None


async def build_backend_context(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    conversation_data: dict[str, object],
) -> AgentBackendContext:
    user = await session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
    if user is None:
        raise LookupError("Telegram user is not registered")

    plan: Plan | None = None
    plan_code = conversation_data.get("plan_code")
    if isinstance(plan_code, str):
        plan = await session.scalar(
            select(Plan).where(Plan.code == plan_code, Plan.is_active.is_(True))
        )

    order: Order | None = None
    order_id = conversation_data.get("order_id")
    if isinstance(order_id, int):
        order = await session.scalar(
            select(Order).where(Order.id == order_id, Order.user_id == user.id)
        )
        if order is not None and plan is None:
            plan = await session.get(Plan, order.plan_id)

    presentation: Presentation | None = None
    if order is not None:
        presentation = await session.scalar(
            select(Presentation).where(Presentation.order_id == order.id)
        )

    selected_plan = None
    if plan is not None:
        selected_plan = AgentPlan(
            code=plan.code,
            price_uzs=plan.price_uzs,
            minimum_slides=plan.minimum_slides,
            maximum_slides=plan.maximum_slides,
            priority=plan.priority,
        )

    return AgentBackendContext(
        telegram_user_id=user.telegram_user_id,
        selected_plan=selected_plan,
        payment_status=(
            payment_status_for_order(status=str(order.status), amount=order.amount)
            if order is not None
            else None
        ),
        free_demo_available=not user.free_demo_used,
        order_id=order.public_id if order is not None else None,
        presentation_id=str(presentation.id) if presentation is not None else None,
        available_templates=[
            "auto",
            "minimal",
            "academic",
            "business",
            "modern",
            "creative",
            "technology",
            "dark",
            "light",
            "luxury",
            "corporate",
            "startup",
            "children",
            "medical",
            "historical",
        ],
        available_formats=["pptx", "pdf"],
        admin_status=user.is_admin,
    )
