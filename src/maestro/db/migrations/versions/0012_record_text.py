"""Workflow record text written for people, next to the JSON body.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("workflow_records", "workflow_record_revisions"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(sa.Column("text", sa.Text(), nullable=False, server_default=""))


def downgrade() -> None:
    for table in ("workflow_record_revisions", "workflow_records"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_column("text")
