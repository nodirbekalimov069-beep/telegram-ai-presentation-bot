"""Payment and webhook audit persistence models."""

from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin

JSON_TYPE = JSON().with_variant(JSONB, "postgresql")


class Payment(TimestampMixin, Base):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("provider", "provider_transaction_id", name="provider_transaction"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"),
        unique=True,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(32), index=True)
    provider_transaction_id: Mapped[str] = mapped_column(String(255))
    amount: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(32), index=True)
    raw_request: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict)
    raw_response: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    create_time_ms: Mapped[int | None] = mapped_column(BigInteger)
    perform_time_ms: Mapped[int | None] = mapped_column(BigInteger)
    cancel_time_ms: Mapped[int | None] = mapped_column(BigInteger)
    cancel_reason: Mapped[int | None] = mapped_column(Integer)


class PaymentEvent(TimestampMixin, Base):
    __tablename__ = "payment_events"
    __table_args__ = (
        UniqueConstraint("provider", "idempotency_key", name="provider_event_idempotency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), index=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    event_type: Mapped[str] = mapped_column(String(64))
    raw_request: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE)
