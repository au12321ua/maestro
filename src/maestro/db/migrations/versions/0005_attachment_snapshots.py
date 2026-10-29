"""attachment snapshots by content, uploaded file by file (N-06)

Existing attachments are removed, not converted (U-50): their rows, the runs' links to
them and their recycle bin entries. Runs keep what their snapshot recorded; a rerun of
one that loaded an attachment is refused. The old package directories under
``attachments/`` are removed at the next start.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM deletions WHERE object_type = 'attachment_version'")
    op.drop_table('run_attachments')
    op.drop_table('attachment_versions')
    op.create_table('attachment_versions',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('owner_id', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('label', sa.String(length=64), nullable=True),
    sa.Column('parent_id', sa.String(length=40), nullable=True),
    sa.Column('description', sa.String(length=512), nullable=False),
    sa.Column('content_digest', sa.String(length=80), nullable=False),
    sa.Column('manifest_json', sa.JSON(), nullable=False),
    sa.Column('files_json', sa.JSON(), nullable=False),
    sa.Column('file_count', sa.Integer(), nullable=False),
    sa.Column('size_bytes', sa.Integer(), nullable=False),
    sa.Column('has_bootstrap', sa.Boolean(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], name=op.f('fk_attachment_versions_owner_id_users')),
    sa.ForeignKeyConstraint(['parent_id'], ['attachment_versions.id'], name=op.f('fk_attachment_versions_parent_id_attachment_versions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_attachment_versions'))
    )
    with op.batch_alter_table('attachment_versions', schema=None) as batch_op:
        batch_op.create_index('ix_attachment_versions_content_digest', ['content_digest'], unique=False)
        batch_op.create_index('ix_attachment_versions_name_label', ['name', 'label'], unique=False)

    op.create_table('attachment_files',
    sa.Column('attachment_version_id', sa.String(length=40), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.ForeignKeyConstraint(['attachment_version_id'], ['attachment_versions.id'], name=op.f('fk_attachment_files_attachment_version_id_attachment_versions')),
    sa.PrimaryKeyConstraint('attachment_version_id', 'sha256', name=op.f('pk_attachment_files'))
    )
    with op.batch_alter_table('attachment_files', schema=None) as batch_op:
        batch_op.create_index('ix_attachment_files_sha256', ['sha256'], unique=False)

    op.create_table('attachment_uploads',
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=40), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('expires_at', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_attachment_uploads_user_id_users')),
    sa.PrimaryKeyConstraint('sha256', 'user_id', name=op.f('pk_attachment_uploads'))
    )
    with op.batch_alter_table('attachment_uploads', schema=None) as batch_op:
        batch_op.create_index('ix_attachment_uploads_expires_at', ['expires_at'], unique=False)

    op.create_table('run_attachments',
    sa.Column('run_id', sa.String(length=40), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('attachment_version_id', sa.String(length=40), nullable=False),
    sa.ForeignKeyConstraint(['attachment_version_id'], ['attachment_versions.id'], name=op.f('fk_run_attachments_attachment_version_id_attachment_versions')),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], name=op.f('fk_run_attachments_run_id_runs')),
    sa.PrimaryKeyConstraint('run_id', 'position', name=op.f('pk_run_attachments'))
    )
    with op.batch_alter_table('run_attachments', schema=None) as batch_op:
        batch_op.create_index('ix_run_attachments_version', ['attachment_version_id'], unique=False)


def downgrade() -> None:
    raise NotImplementedError("attachments from before N-06 are not restored")
