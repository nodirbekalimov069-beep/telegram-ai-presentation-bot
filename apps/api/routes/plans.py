"""Tariff API."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies import get_session
from apps.api.schemas import PlanResponse
from modules.plans.models import Plan

router = APIRouter(prefix="/api/v1/plans", tags=["plans"])


@router.get("", response_model=list[PlanResponse])
async def active_plans(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[PlanResponse]:
    plans = (
        await session.scalars(select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.price_uzs))
    ).all()
    return [PlanResponse.model_validate(plan) for plan in plans]
