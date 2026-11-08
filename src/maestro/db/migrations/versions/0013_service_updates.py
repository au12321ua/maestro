"""Updates started in the web pages.

Revision ID: 0013
Revises: 0012
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "service_updates",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("from_version", sa.String(length=64), nullable=False),
        sa.Column("release_json", sa.JSON(), nullable=False),
        sa.Column("interrupt_running", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("requested_by", sa.String(length=40), nullable=False),
        sa.Column("finished_by", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.Column("requested_at", sa.String(length=32), nullable=True),
        sa.Column("finished_at", sa.String(length=32), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.CheckConstraint(
            "state IN ('draining','requested','succeeded','failed','cancelled')",
            name=op.f("ck_service_updates_state"),
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"], ["users.id"], name=op.f("fk_service_updates_requested_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["finished_by"], ["users.id"], name=op.f("fk_service_updates_finished_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_service_updates")),
    )
    op.create_index("ix_service_updates_state", "service_updates", ["state", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_service_updates_state", table_name="service_updates")
    op.drop_table("service_updates")
