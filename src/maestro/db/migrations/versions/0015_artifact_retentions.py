"""Reserve team storage for manually retained run artifacts.

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artifact_retentions",
        sa.Column("storage_key", sa.String(256), primary_key=True),
        sa.Column("run_id", sa.String(40), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
    )
    op.create_index("ix_artifact_retentions_run", "artifact_retentions", ["run_id"])


def downgrade() -> None:
    op.drop_table("artifact_retentions")
