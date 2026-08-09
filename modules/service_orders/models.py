"""Persistence model for extensible non-presentation service requests."""

from typing import Any
from uuid import uuid4

from sqlalchemy import ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin

JSON_TYPE = JSON().with_variant(JSONB, "postgresql")


class ServiceOrder(TimestampMixin, Base):
    __tablename__ = "service_orders"
    __table_args__ = (UniqueConstraint("idempotency_key", name="service_orders_idempotency_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(36), unique=True, index=True, default=lambda: str(uuid4())
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    service_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result_metadata: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
