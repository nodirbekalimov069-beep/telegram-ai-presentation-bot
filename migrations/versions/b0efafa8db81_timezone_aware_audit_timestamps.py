"""Make audit timestamps timezone-aware.

Revision ID: b0efafa8db81
Revises: 56a9cd5a47f2
Create Date: 2026-08-06
"""

from collections.abc import Iterable

import sqlalchemy as sa
from alembic import op

revision = "b0efafa8db81"
down_revision = "56a9cd5a47f2"
branch_labels = None
depends_on = None

_AUDIT_TIMESTAMP_TABLES: tuple[str, ...] = (
    "bot_settings",
    "free_demo_claims",
    "generation_attempts",
    "generation_jobs",
    "orders",
    "outbox_events",
    "payment_events",
    "payments",
    "plans",
    "presentations",
    "promo_codes",
    "prompt_versions",
    "ratings",
    "telegram_update_inbox",
    "templates",
    "users",
)


def _alter_timestamp_columns(
    *,
    tables: Iterable[str],
    timezone: bool,
) -> None:
    if op.get_bind().dialect.name != "postgresql":
        # SQLite stores both variants as DATETIME, so no physical alteration is needed.
        return

    existing_type = sa.DateTime(timezone=not timezone)
    target_type = sa.DateTime(timezone=timezone)
    for table_name in tables:
        for column_name in ("created_at", "updated_at"):
            op.alter_column(
                table_name,
                column_name,
                existing_type=existing_type,
                type_=target_type,
                existing_nullable=False,
                postgresql_using=f"\"{column_name}\" AT TIME ZONE 'UTC'",
            )


def upgrade() -> None:
    _alter_timestamp_columns(tables=_AUDIT_TIMESTAMP_TABLES, timezone=True)


def downgrade() -> None:
    _alter_timestamp_columns(tables=reversed(_AUDIT_TIMESTAMP_TABLES), timezone=False)
