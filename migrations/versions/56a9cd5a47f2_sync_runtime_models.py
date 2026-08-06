"""Add idempotent Telegram artifact delivery state.

Revision ID: 56a9cd5a47f2
Revises: 1ff33e0c766a
Create Date: 2026-08-05 15:18:15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "56a9cd5a47f2"
down_revision: str | Sequence[str] | None = "1ff33e0c766a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("presentations") as batch_op:
        batch_op.add_column(
            sa.Column(
                "delivery_status",
                sa.String(length=24),
                nullable=False,
                server_default="PENDING",
            )
        )
        batch_op.add_column(
            sa.Column(
                "delivery_attempts",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(sa.Column("delivery_lease_token", sa.String(length=64)))
        batch_op.add_column(sa.Column("delivery_lease_expires_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("pptx_telegram_message_id", sa.Integer()))
        batch_op.add_column(sa.Column("pdf_telegram_message_id", sa.Integer()))
        batch_op.add_column(sa.Column("delivered_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("delivery_error", sa.Text()))
        batch_op.create_index(
            op.f("ix_presentations_delivery_status"),
            ["delivery_status"],
            unique=False,
        )
        batch_op.create_unique_constraint(
            op.f("uq_presentations_delivery_lease_token"),
            ["delivery_lease_token"],
        )


def downgrade() -> None:
    with op.batch_alter_table("presentations") as batch_op:
        batch_op.drop_constraint(
            op.f("uq_presentations_delivery_lease_token"),
            type_="unique",
        )
        batch_op.drop_index(op.f("ix_presentations_delivery_status"))
        batch_op.drop_column("delivery_error")
        batch_op.drop_column("delivered_at")
        batch_op.drop_column("pdf_telegram_message_id")
        batch_op.drop_column("pptx_telegram_message_id")
        batch_op.drop_column("delivery_lease_expires_at")
        batch_op.drop_column("delivery_lease_token")
        batch_op.drop_column("delivery_attempts")
        batch_op.drop_column("delivery_status")
