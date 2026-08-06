"""Template persistence model."""

from typing import Any

from sqlalchemy import Boolean, Integer, JSON, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin

JSON_TYPE = JSON().with_variant(JSONB, "postgresql")


class Template(TimestampMixin, Base):
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    category: Mapped[str] = mapped_column(String(100), index=True)
    style: Mapped[str] = mapped_column(String(50), index=True)
    palette: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE)
    typography: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE)
    layouts: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE)
    preview_url: Mapped[str | None] = mapped_column(String(500))
    minimum_plan: Mapped[str] = mapped_column(String(64), default="free_demo")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
