"""API response contracts."""

from typing import Any

from pydantic import BaseModel, ConfigDict


class HealthResponse(BaseModel):
    status: str


class ReadinessResponse(BaseModel):
    status: str
    database: str


class PlanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str
    price_uzs: int
    minimum_slides: int
    maximum_slides: int
    priority: int
    features: dict[str, Any]


class AdminStatsResponse(BaseModel):
    users: int
    orders: int
    presentations: int
    paid_revenue_uzs: int
