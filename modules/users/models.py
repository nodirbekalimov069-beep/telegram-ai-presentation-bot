"""User persistence model."""

from datetime import UTC, datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, true
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str | None] = mapped_column(String(255))
    last_name: Mapped[str | None] = mapped_column(String(255))
    language_code: Mapped[str | None] = mapped_column(String(16))
    selected_language: Mapped[str] = mapped_column(String(8), default="uz")
    notifications_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    free_demo_used: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    presentations_count: Mapped[int] = mapped_column(Integer, default=0)
    referral_code: Mapped[str | None] = mapped_column(String(32), unique=True)
    referred_by: Mapped[int | None] = mapped_column(Integer, index=True)
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )


class FreeDemoClaim(TimestampMixin, Base):
    """Durable entitlement ledger keyed by immutable Telegram user ID."""

    __tablename__ = "free_demo_claims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, nullable=False, index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    status: Mapped[str] = mapped_column(String(20), default="RESERVED", nullable=False, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    reserved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    release_reason: Mapped[str | None] = mapped_column(String(500))
