"""Uploaded version labels, provenance and task file reads.

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("dataset_versions") as batch_op:
        batch_op.add_column(sa.Column("label", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("based_on_version_id", sa.String(40), nullable=True))
        batch_op.create_foreign_key(
            "fk_dataset_versions_based_on_version_id_dataset_versions",
            "dataset_versions",
            ["based_on_version_id"],
            ["id"],
        )
        batch_op.create_index("ix_dataset_versions_label", ["dataset_id", "label"])
    with op.batch_alter_table("task_reads") as batch_op:
        batch_op.drop_constraint(op.f("ck_task_reads_category"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_task_reads_category"),
            "category IN ('instruction','trial_artifacts','task_files')",
        )


def downgrade() -> None:
    op.execute("DELETE FROM task_reads WHERE category = 'task_files'")
    with op.batch_alter_table("task_reads") as batch_op:
        batch_op.drop_constraint(op.f("ck_task_reads_category"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_task_reads_category"), "category IN ('instruction','trial_artifacts')"
        )
    with op.batch_alter_table("dataset_versions") as batch_op:
        batch_op.drop_index("ix_dataset_versions_label")
        batch_op.drop_constraint(
            "fk_dataset_versions_based_on_version_id_dataset_versions", type_="foreignkey"
        )
        batch_op.drop_column("based_on_version_id")
        batch_op.drop_column("label")
