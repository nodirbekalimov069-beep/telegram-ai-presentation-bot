"""Authenticated admin statistics and management endpoints."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies import get_session, require_admin_header
from apps.api.schemas import AdminStatsResponse
from core.outbox.service import OutboxService
from modules.admin.models import AuditLog, BotSetting
from modules.orders.domain import OrderStateMachine, OrderStatus
from modules.orders.models import Order
from modules.plans.models import Plan
from modules.presentations.models import GenerationJob, Presentation
from modules.users.models import User

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


class PlanPatch(BaseModel):
    price_uzs: int | None = Field(default=None, ge=0)
    minimum_slides: int | None = Field(default=None, ge=1, le=30)
    maximum_slides: int | None = Field(default=None, ge=1, le=30)
    priority: int | None = Field(default=None, ge=0, le=1000)
    features: dict[str, Any] | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def limits_are_ordered(self) -> "PlanPatch":
        if (
            self.minimum_slides is not None
            and self.maximum_slides is not None
            and self.minimum_slides > self.maximum_slides
        ):
            raise ValueError("minimum_slides must not exceed maximum_slides")
        return self


class BlockPatch(BaseModel):
    blocked: bool


class SettingPatch(BaseModel):
    value: dict[str, Any]
    is_public: bool = False


@router.get("/stats", response_model=AdminStatsResponse)
async def stats(
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[int, Depends(require_admin_header)],
) -> AdminStatsResponse:
    users = int(await session.scalar(select(func.count(User.id))) or 0)
    orders = int(await session.scalar(select(func.count(Order.id))) or 0)
    presentations = int(await session.scalar(select(func.count(Presentation.id))) or 0)
    revenue = int(
        await session.scalar(
            select(func.coalesce(func.sum(Order.amount), 0)).where(
                Order.status.in_([OrderStatus.PAID, OrderStatus.COMPLETED])
            )
        )
        or 0
    )
    return AdminStatsResponse(
        users=users,
        orders=orders,
        presentations=presentations,
        paid_revenue_uzs=revenue,
    )


@router.patch("/plans/{code}")
async def update_plan(
    code: str,
    patch: PlanPatch,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    admin_id: Annotated[int, Depends(require_admin_header)],
) -> dict[str, Any]:
    async with session.begin():
        plan = await session.scalar(select(Plan).where(Plan.code == code).with_for_update())
        if plan is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="plan not found")
        changes = patch.model_dump(exclude_none=True)
        minimum = int(changes.get("minimum_slides", plan.minimum_slides))
        maximum = int(changes.get("maximum_slides", plan.maximum_slides))
        if minimum > maximum:
            raise HTTPException(status_code=422, detail="invalid slide limits")
        for key, value in changes.items():
            setattr(plan, key, value)
        session.add(
            AuditLog(
                actor_user_id=admin_id,
                action="plan.update",
                entity_type="plan",
                entity_id=code,
                request_id=request.headers.get("x-request-id"),
                details={"changes": changes},
            )
        )
    return {
        "code": plan.code,
        "price_uzs": plan.price_uzs,
        "minimum_slides": plan.minimum_slides,
        "maximum_slides": plan.maximum_slides,
        "priority": plan.priority,
        "features": plan.features,
        "is_active": plan.is_active,
    }


@router.patch("/users/{telegram_user_id}/block")
async def block_user(
    telegram_user_id: int,
    patch: BlockPatch,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    admin_id: Annotated[int, Depends(require_admin_header)],
) -> dict[str, Any]:
    async with session.begin():
        user = await session.scalar(
            select(User).where(User.telegram_user_id == telegram_user_id).with_for_update()
        )
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        user.is_blocked = patch.blocked
        session.add(
            AuditLog(
                actor_user_id=admin_id,
                action="user.block" if patch.blocked else "user.unblock",
                entity_type="user",
                entity_id=str(telegram_user_id),
                request_id=request.headers.get("x-request-id"),
                details={"blocked": patch.blocked},
            )
        )
    return {"telegram_user_id": telegram_user_id, "blocked": user.is_blocked}


@router.get("/orders")
async def list_orders(
    session: Annotated[AsyncSession, Depends(get_session)],
    _: Annotated[int, Depends(require_admin_header)],
    order_status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    query = select(Order).order_by(Order.id.desc()).limit(min(max(limit, 1), 200))
    if order_status is not None:
        try:
            normalized = OrderStatus(order_status.upper())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid status") from exc
        query = query.where(Order.status == normalized)
    orders = (await session.scalars(query)).all()
    return [
        {
            "id": order.id,
            "public_id": order.public_id,
            "user_id": order.user_id,
            "amount": order.amount,
            "currency": order.currency,
            "status": order.status,
            "topic": order.topic,
            "created_at": order.created_at,
        }
        for order in orders
    ]


@router.post("/presentations/{presentation_id}/retry")
async def retry_presentation(
    presentation_id: int,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    admin_id: Annotated[int, Depends(require_admin_header)],
) -> dict[str, Any]:
    machine = OrderStateMachine()
    async with session.begin():
        presentation = await session.get(Presentation, presentation_id, with_for_update=True)
        if presentation is None:
            raise HTTPException(status_code=404, detail="presentation not found")
        order = await session.get(Order, presentation.order_id, with_for_update=True)
        job = await session.scalar(
            select(GenerationJob)
            .where(GenerationJob.presentation_id == presentation_id)
            .with_for_update()
        )
        if order is None or job is None or OrderStatus(order.status) != OrderStatus.FAILED:
            raise HTTPException(status_code=409, detail="presentation is not retryable")
        order.status = machine.transition(OrderStatus.FAILED, OrderStatus.QUEUED)
        presentation.status = OrderStatus.QUEUED
        presentation.error_message = None
        job.status = "QUEUED"
        job.progress = 0
        job.current_step = "queued"
        job.error_message = None
        retry_number = job.retries + 1
        await OutboxService().enqueue(
            session,
            event_type="generation.requested",
            aggregate_type="presentation",
            aggregate_id=str(presentation_id),
            idempotency_key=f"generation:{presentation_id}:retry:{retry_number}",
            payload={"presentation_id": presentation_id, "priority": job.priority},
        )
        session.add(
            AuditLog(
                actor_user_id=admin_id,
                action="presentation.retry",
                entity_type="presentation",
                entity_id=str(presentation_id),
                request_id=request.headers.get("x-request-id"),
                details={"retry_number": retry_number},
            )
        )
    return {"presentation_id": presentation_id, "status": "QUEUED"}


@router.put("/settings/{key}")
async def upsert_setting(
    key: str,
    patch: SettingPatch,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    admin_id: Annotated[int, Depends(require_admin_header)],
) -> dict[str, Any]:
    if not key or len(key) > 120:
        raise HTTPException(status_code=422, detail="invalid key")
    async with session.begin():
        setting = await session.scalar(
            select(BotSetting).where(BotSetting.key == key).with_for_update()
        )
        if setting is None:
            setting = BotSetting(key=key, value=patch.value, is_public=patch.is_public)
            session.add(setting)
        else:
            setting.value = patch.value
            setting.is_public = patch.is_public
        session.add(
            AuditLog(
                actor_user_id=admin_id,
                action="setting.upsert",
                entity_type="bot_setting",
                entity_id=key,
                request_id=request.headers.get("x-request-id"),
                details={"is_public": patch.is_public},
            )
        )
    return {"key": key, "value": setting.value, "is_public": setting.is_public}
