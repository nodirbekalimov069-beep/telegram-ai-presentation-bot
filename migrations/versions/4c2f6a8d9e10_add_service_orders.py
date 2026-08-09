"""Add universal service orders and notification preference.

Revision ID: 4c2f6a8d9e10
Revises: 7a2e4c9f6b31
Create Date: 2026-08-09
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "4c2f6a8d9e10"
down_revision = "7a2e4c9f6b31"
branch_labels = None
depends_on = None

json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "notifications_enabled",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )
    op.create_table(
        "service_orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("service_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("request_payload", json_type, nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("result_metadata", json_type, nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_service_orders_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_service_orders")),
        sa.UniqueConstraint("idempotency_key", name="service_orders_idempotency_key"),
    )
    op.create_index(
        op.f("ix_service_orders_public_id"), "service_orders", ["public_id"], unique=True
    )
    op.create_index(op.f("ix_service_orders_user_id"), "service_orders", ["user_id"])
    op.create_index(op.f("ix_service_orders_service_type"), "service_orders", ["service_type"])
    op.create_index(op.f("ix_service_orders_status"), "service_orders", ["status"])


def downgrade() -> None:
    op.drop_index(op.f("ix_service_orders_status"), table_name="service_orders")
    op.drop_index(op.f("ix_service_orders_service_type"), table_name="service_orders")
    op.drop_index(op.f("ix_service_orders_user_id"), table_name="service_orders")
    op.drop_index(op.f("ix_service_orders_public_id"), table_name="service_orders")
    op.drop_table("service_orders")
    op.drop_column("users", "notifications_enabled")
