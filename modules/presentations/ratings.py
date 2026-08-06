"""User ratings for generated presentations."""

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from core.database.base import Base, TimestampMixin


class Rating(TimestampMixin, Base):
    __tablename__ = "ratings"
    __table_args__ = (
        UniqueConstraint("user_id", "presentation_id", name="user_presentation_rating"),
        CheckConstraint("score BETWEEN 1 AND 5", name="score_range"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    presentation_id: Mapped[int] = mapped_column(
        ForeignKey("presentations.id", ondelete="CASCADE"), index=True
    )
    score: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(String(1000))
