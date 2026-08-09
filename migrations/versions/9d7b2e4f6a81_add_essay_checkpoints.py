"""Add durable essay generation and delivery checkpoints.

Revision ID: 9d7b2e4f6a81
Revises: 4c2f6a8d9e10
Create Date: 2026-08-09
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "9d7b2e4f6a81"
down_revision = "4c2f6a8d9e10"
branch_labels = None
depends_on = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column(
        "service_orders",
        sa.Column("normalized_request", json_type, server_default=sa.text("'{}'"), nullable=False),
    )
    op.add_column(
        "service_orders",
        sa.Column("generation_metadata", json_type, server_default=sa.text("'{}'"), nullable=False),
    )
    op.add_column(
        "service_orders",
        sa.Column("generation_attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("service_orders", sa.Column("generation_lease_token", sa.String(64)))
    op.add_column(
        "service_orders", sa.Column("generation_lease_expires_at", sa.DateTime(timezone=True))
    )
    op.add_column("service_orders", sa.Column("queued_at", sa.DateTime(timezone=True)))
    op.add_column("service_orders", sa.Column("started_at", sa.DateTime(timezone=True)))
    op.add_column("service_orders", sa.Column("completed_at", sa.DateTime(timezone=True)))
    op.add_column("service_orders", sa.Column("docx_storage_key", sa.String(500)))
    op.add_column("service_orders", sa.Column("pdf_storage_key", sa.String(500)))
    op.add_column("service_orders", sa.Column("pdf_error", sa.Text()))
    op.add_column(
        "service_orders",
        sa.Column("delivery_status", sa.String(32), server_default="PENDING", nullable=False),
    )
    op.add_column(
        "service_orders",
        sa.Column("delivery_attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("service_orders", sa.Column("delivery_lease_token", sa.String(64)))
    op.add_column(
        "service_orders", sa.Column("delivery_lease_expires_at", sa.DateTime(timezone=True))
    )
    op.add_column("service_orders", sa.Column("delivery_error", sa.Text()))
    op.add_column("service_orders", sa.Column("docx_telegram_message_id", sa.BigInteger()))
    op.add_column("service_orders", sa.Column("pdf_telegram_message_id", sa.BigInteger()))
    op.add_column("service_orders", sa.Column("delivered_at", sa.DateTime(timezone=True)))
    op.execute("UPDATE service_orders SET normalized_request = request_payload")


def downgrade() -> None:
    for column_name in (
        "delivered_at",
        "pdf_telegram_message_id",
        "docx_telegram_message_id",
        "delivery_error",
        "delivery_lease_expires_at",
        "delivery_lease_token",
        "delivery_attempts",
        "delivery_status",
        "pdf_error",
        "pdf_storage_key",
        "docx_storage_key",
        "completed_at",
        "started_at",
        "queued_at",
        "generation_lease_expires_at",
        "generation_lease_token",
        "generation_attempts",
        "generation_metadata",
        "normalized_request",
    ):
        op.drop_column("service_orders", column_name)
