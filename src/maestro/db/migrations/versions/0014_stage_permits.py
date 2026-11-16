"""Persistent attempt slots and environment stage permits.

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trials", sa.Column("queue_reason", sa.String(64), nullable=True))
    op.add_column("execution_attempts", sa.Column("backfilled", sa.Boolean(), nullable=False, server_default="0"))
    op.add_column("execution_attempts", sa.Column("slot_held", sa.Boolean(), nullable=False, server_default="0"))
    op.add_column("execution_attempts", sa.Column("cleanup_state", sa.String(16), nullable=False, server_default="not_required"))
    op.add_column("work_items", sa.Column("ready_at", sa.String(32), nullable=True))
    op.add_column("work_items", sa.Column("bypass_count", sa.Integer(), nullable=False, server_default="0"))
    op.create_table(
        "stage_permits",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("attempt_id", sa.String(40), sa.ForeignKey("execution_attempts.id"), nullable=False),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("cpus", sa.Float(), nullable=False),
        sa.Column("memory_mb", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(64), nullable=True),
        sa.Column("requested_at", sa.String(32), nullable=False),
        sa.Column("granted_at", sa.String(32), nullable=True),
        sa.Column("released_at", sa.String(32), nullable=True),
        sa.UniqueConstraint("attempt_id", "phase"),
        sa.CheckConstraint("state IN ('waiting','granted','released')", name="state"),
    )
    op.create_index("ix_stage_permits_state", "stage_permits", ["state"])


def downgrade() -> None:
    op.drop_table("stage_permits")
    op.drop_column("work_items", "bypass_count")
    op.drop_column("work_items", "ready_at")
    op.drop_column("execution_attempts", "cleanup_state")
    op.drop_column("execution_attempts", "slot_held")
    op.drop_column("execution_attempts", "backfilled")
    op.drop_column("trials", "queue_reason")
