"""Collection held-out specifications and permanent task read summaries.

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "collections",
        sa.Column("held_out_task_ids_json", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.create_table(
        "task_reads",
        sa.Column("task_id", sa.String(40), nullable=False),
        sa.Column("reader_id", sa.String(40), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("channel_id", sa.String(40), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("first_read_at", sa.String(32), nullable=False),
        sa.Column("last_read_at", sa.String(32), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_task_reads_task_id_tasks")
        ),
        sa.ForeignKeyConstraint(
            ["reader_id"], ["users.id"], name=op.f("fk_task_reads_reader_id_users")
        ),
        sa.PrimaryKeyConstraint(
            "task_id", "reader_id", "channel", "channel_id", "category", name=op.f("pk_task_reads")
        ),
        sa.CheckConstraint("channel IN ('pat','web_session')", name=op.f("ck_task_reads_channel")),
        sa.CheckConstraint(
            "category IN ('instruction','trial_artifacts')", name=op.f("ck_task_reads_category")
        ),
    )


def downgrade() -> None:
    op.drop_table("task_reads")
    op.drop_column("collections", "held_out_task_ids_json")
