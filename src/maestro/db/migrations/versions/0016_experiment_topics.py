"""Experiment topics and their references.

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "topics",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("owner_id", sa.String(40), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("settings_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.Column("updated_at", sa.String(32), nullable=False),
    )
    op.create_index("ix_topics_created", "topics", ["created_at", "id"])
    op.create_table(
        "topic_references",
        sa.Column("topic_id", sa.String(40), sa.ForeignKey("topics.id"), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("object_id", sa.String(40), nullable=False),
        sa.Column("added_at", sa.String(32), nullable=False),
        sa.PrimaryKeyConstraint("topic_id", "kind", "object_id"),
        sa.CheckConstraint("kind IN ('run','collection','dataset','attachment_snapshot')", name="kind"),
    )
    op.create_index(
        "ix_topic_references_added", "topic_references", ["topic_id", "kind", "added_at", "object_id"]
    )


def downgrade() -> None:
    op.drop_table("topic_references")
    op.drop_table("topics")
