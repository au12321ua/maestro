"""Uploaded dataset file contents.

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dataset_files",
        sa.Column("operation_id", sa.String(40), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("dataset_digest", sa.String(80), nullable=False),
        sa.Column("dataset_version_id", sa.String(40), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("executable", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["operations.id"],
            name=op.f("fk_dataset_files_operation_id_operations"),
        ),
        sa.ForeignKeyConstraint(
            ["dataset_version_id"],
            ["dataset_versions.id"],
            name=op.f("fk_dataset_files_dataset_version_id_dataset_versions"),
        ),
        sa.PrimaryKeyConstraint("operation_id", "path", name=op.f("pk_dataset_files")),
    )
    op.create_index("ix_dataset_files_digest", "dataset_files", ["dataset_digest"])
    op.create_index("ix_dataset_files_sha256", "dataset_files", ["sha256"])


def downgrade() -> None:
    op.drop_table("dataset_files")
