"""Operational health endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies import get_session
from apps.api.schemas import HealthResponse, ReadinessResponse

router = APIRouter(tags=["operations"])


@router.get("/health", response_model=HealthResponse, include_in_schema=False)
@router.get("/health/live", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/ready", response_model=ReadinessResponse, include_in_schema=False)
@router.get("/health/ready", response_model=ReadinessResponse)
async def ready(session: Annotated[AsyncSession, Depends(get_session)]) -> ReadinessResponse:
    await session.execute(text("SELECT 1"))
    return ReadinessResponse(status="ready", database="ok")
