"""Plan persistence model."""

from typing import Any

from sqlalchemy import Boolean, CheckConstraint, Integer, JSON, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin

JSON_TYPE = JSON().with_variant(JSONB, "postgresql")


class Plan(TimestampMixin, Base):
    __tablename__ = "plans"
    __table_args__ = (
        CheckConstraint("price_uzs >= 0", name="price_non_negative"),
        CheckConstraint("minimum_slides >= 1", name="minimum_slides_positive"),
        CheckConstraint("maximum_slides >= minimum_slides", name="slide_range_valid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    price_uzs: Mapped[int] = mapped_column(Integer)
    minimum_slides: Mapped[int] = mapped_column(Integer)
    maximum_slides: Mapped[int] = mapped_column(Integer)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    features: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
