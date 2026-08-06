"""Presentation persistence and generation jobs."""

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin

JSON_TYPE = JSON().with_variant(JSONB, "postgresql")


class Presentation(TimestampMixin, Base):
    __tablename__ = "presentations"
    __table_args__ = (CheckConstraint("slide_count BETWEEN 1 AND 30", name="slide_count_range"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), unique=True, index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    topic: Mapped[str] = mapped_column(String(500))
    language: Mapped[str] = mapped_column(String(8))
    direction: Mapped[str] = mapped_column(String(100), index=True)
    audience: Mapped[str] = mapped_column(String(200))
    slide_count: Mapped[int] = mapped_column(Integer)
    design_style: Mapped[str] = mapped_column(String(50))
    outline: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict)
    content: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict)
    status: Mapped[str] = mapped_column(String(40), index=True)
    pptx_storage_key: Mapped[str | None] = mapped_column(String(500))
    pdf_storage_key: Mapped[str | None] = mapped_column(String(500))
    preview_storage_key: Mapped[str | None] = mapped_column(String(500))
    delivery_status: Mapped[str] = mapped_column(String(24), default="PENDING", index=True)
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    delivery_lease_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    delivery_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pptx_telegram_message_id: Mapped[int | None] = mapped_column(Integer)
    pdf_telegram_message_id: Mapped[int | None] = mapped_column(Integer)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_error: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    generation_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    generation_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GenerationJob(TimestampMixin, Base):
    __tablename__ = "generation_jobs"
    __table_args__ = (CheckConstraint("progress BETWEEN 0 AND 100", name="progress_range"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    presentation_id: Mapped[int] = mapped_column(
        ForeignKey("presentations.id", ondelete="CASCADE"), unique=True, index=True
    )
    task_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    current_step: Mapped[str] = mapped_column(String(120))
    retries: Mapped[int] = mapped_column(Integer, default=0)
    priority: Mapped[int] = mapped_column(Integer, default=0, index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    lease_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GenerationAttempt(TimestampMixin, Base):
    __tablename__ = "generation_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation_job_id: Mapped[int] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="CASCADE"), index=True
    )
    fencing_token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
