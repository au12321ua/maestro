"""workflow records and collection current best

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflow_records",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("target_snapshot_id", sa.String(length=40), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("body_json", sa.JSON(), nullable=False),
        sa.Column("references_json", sa.JSON(), nullable=False),
        sa.Column("excerpts_json", sa.JSON(), nullable=False),
        sa.Column("author_id", sa.String(length=40), nullable=False),
        sa.Column("pat_id", sa.String(length=40), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.Column("updated_at", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["author_id"], ["users.id"], name=op.f("fk_workflow_records_author_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["pat_id"], ["pats.id"], name=op.f("fk_workflow_records_pat_id_pats")
        ),
        sa.ForeignKeyConstraint(
            ["target_snapshot_id"],
            ["attachment_versions.id"],
            name=op.f("fk_workflow_records_target_snapshot_id_attachment_versions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_records")),
    )
    with op.batch_alter_table("workflow_records", schema=None) as batch_op:
        batch_op.create_index(
            "ix_workflow_records_target",
            ["target_type", "target_id", "created_at", "id"],
            unique=False,
        )

    op.create_table(
        "workflow_record_revisions",
        sa.Column("record_id", sa.String(length=40), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("body_json", sa.JSON(), nullable=False),
        sa.Column("references_json", sa.JSON(), nullable=False),
        sa.Column("excerpts_json", sa.JSON(), nullable=False),
        sa.Column("actor_id", sa.String(length=40), nullable=False),
        sa.Column("pat_id", sa.String(length=40), nullable=True),
        sa.Column("at", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["users.id"], name=op.f("fk_workflow_record_revisions_actor_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["pat_id"], ["pats.id"], name=op.f("fk_workflow_record_revisions_pat_id_pats")
        ),
        sa.ForeignKeyConstraint(
            ["record_id"],
            ["workflow_records.id"],
            name=op.f("fk_workflow_record_revisions_record_id_workflow_records"),
        ),
        sa.PrimaryKeyConstraint("record_id", "revision", name=op.f("pk_workflow_record_revisions")),
    )
    with op.batch_alter_table("collections", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("current_best_snapshot_id", sa.String(length=40), nullable=True)
        )
        batch_op.add_column(sa.Column("current_best_json", sa.JSON(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f("fk_collections_current_best_snapshot_id_attachment_versions"),
            "attachment_versions",
            ["current_best_snapshot_id"],
            ["id"],
        )



def downgrade() -> None:
    with op.batch_alter_table("collections", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_collections_current_best_snapshot_id_attachment_versions"),
            type_="foreignkey",
        )
        batch_op.drop_column("current_best_json")
        batch_op.drop_column("current_best_snapshot_id")

    op.drop_table("workflow_record_revisions")
    with op.batch_alter_table("workflow_records", schema=None) as batch_op:
        batch_op.drop_index("ix_workflow_records_target")

    op.drop_table("workflow_records")
