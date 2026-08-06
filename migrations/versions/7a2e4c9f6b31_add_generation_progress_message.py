"""Add durable Telegram generation progress message coordinates.

Revision ID: 7a2e4c9f6b31
Revises: b0efafa8db81
Create Date: 2026-08-07
"""

import sqlalchemy as sa
from alembic import op

revision = "7a2e4c9f6b31"
down_revision = "b0efafa8db81"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "generation_jobs",
        sa.Column("progress_chat_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "generation_jobs",
        sa.Column("progress_message_id", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("generation_jobs", "progress_message_id")
    op.drop_column("generation_jobs", "progress_chat_id")
