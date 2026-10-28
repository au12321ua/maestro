"""collections and analysis snapshots (N-11)

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('collections',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('description', sa.String(length=1024), nullable=False),
    sa.Column('owner_id', sa.String(length=40), nullable=False),
    sa.Column('pat_id', sa.String(length=40), nullable=True),
    sa.Column('scenario', sa.String(length=32), nullable=False),
    sa.Column('variable_json', sa.JSON(), nullable=False),
    sa.Column('fixed_json', sa.JSON(), nullable=False),
    sa.Column('template_json', sa.JSON(), nullable=False),
    sa.Column('base_run_id', sa.String(length=40), nullable=True),
    sa.Column('settings_json', sa.JSON(), nullable=False),
    sa.Column('copied_from', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.String(length=32), nullable=False),
    sa.Column('updated_at', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['base_run_id'], ['runs.id'], name=op.f('fk_collections_base_run_id_runs')),
    sa.ForeignKeyConstraint(['copied_from'], ['collections.id'], name=op.f('fk_collections_copied_from_collections')),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], name=op.f('fk_collections_owner_id_users')),
    sa.ForeignKeyConstraint(['pat_id'], ['pats.id'], name=op.f('fk_collections_pat_id_pats')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_collections'))
    )
    op.create_index('ix_collections_name', 'collections', ['name'], unique=False)
    op.create_table('analysis_snapshots',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=256), nullable=False),
    sa.Column('collection_id', sa.String(length=40), nullable=True),
    sa.Column('owner_id', sa.String(length=40), nullable=False),
    sa.Column('pat_id', sa.String(length=40), nullable=True),
    sa.Column('runs_json', sa.JSON(), nullable=False),
    sa.Column('settings_json', sa.JSON(), nullable=False),
    sa.Column('spec_json', sa.JSON(), nullable=True),
    sa.Column('result_json', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['collection_id'], ['collections.id'], name=op.f('fk_analysis_snapshots_collection_id_collections')),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], name=op.f('fk_analysis_snapshots_owner_id_users')),
    sa.ForeignKeyConstraint(['pat_id'], ['pats.id'], name=op.f('fk_analysis_snapshots_pat_id_pats')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_analysis_snapshots'))
    )
    op.create_index('ix_analysis_snapshots_collection', 'analysis_snapshots', ['collection_id'], unique=False)
    op.create_index('ix_analysis_snapshots_created', 'analysis_snapshots', ['created_at', 'id'], unique=False)
    op.create_table('collection_events',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('collection_id', sa.String(length=40), nullable=False),
    sa.Column('action', sa.String(length=32), nullable=False),
    sa.Column('run_id', sa.String(length=40), nullable=True),
    sa.Column('actor_id', sa.String(length=40), nullable=False),
    sa.Column('pat_id', sa.String(length=40), nullable=True),
    sa.Column('at', sa.String(length=32), nullable=False),
    sa.Column('detail_json', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['actor_id'], ['users.id'], name=op.f('fk_collection_events_actor_id_users')),
    sa.ForeignKeyConstraint(['collection_id'], ['collections.id'], name=op.f('fk_collection_events_collection_id_collections')),
    sa.ForeignKeyConstraint(['pat_id'], ['pats.id'], name=op.f('fk_collection_events_pat_id_pats')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_collection_events'))
    )
    op.create_index('ix_collection_events_collection_at', 'collection_events', ['collection_id', 'at', 'id'], unique=False)
    op.create_table('collection_members',
    sa.Column('collection_id', sa.String(length=40), nullable=False),
    sa.Column('run_id', sa.String(length=40), nullable=False),
    sa.Column('label', sa.String(length=128), nullable=True),
    sa.Column('added_by', sa.String(length=40), nullable=False),
    sa.Column('pat_id', sa.String(length=40), nullable=True),
    sa.Column('added_at', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['added_by'], ['users.id'], name=op.f('fk_collection_members_added_by_users')),
    sa.ForeignKeyConstraint(['collection_id'], ['collections.id'], name=op.f('fk_collection_members_collection_id_collections')),
    sa.ForeignKeyConstraint(['pat_id'], ['pats.id'], name=op.f('fk_collection_members_pat_id_pats')),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], name=op.f('fk_collection_members_run_id_runs')),
    sa.PrimaryKeyConstraint('collection_id', 'run_id', name=op.f('pk_collection_members'))
    )
    op.create_index('ix_collection_members_run', 'collection_members', ['run_id'], unique=False)
    op.create_table('snapshot_notes',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('snapshot_id', sa.String(length=40), nullable=False),
    sa.Column('author_id', sa.String(length=40), nullable=False),
    sa.Column('pat_id', sa.String(length=40), nullable=True),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('created_at', sa.String(length=32), nullable=False),
    sa.Column('updated_at', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['author_id'], ['users.id'], name=op.f('fk_snapshot_notes_author_id_users')),
    sa.ForeignKeyConstraint(['pat_id'], ['pats.id'], name=op.f('fk_snapshot_notes_pat_id_pats')),
    sa.ForeignKeyConstraint(['snapshot_id'], ['analysis_snapshots.id'], name=op.f('fk_snapshot_notes_snapshot_id_analysis_snapshots')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_snapshot_notes'))
    )
    op.create_index('ix_snapshot_notes_snapshot', 'snapshot_notes', ['snapshot_id', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_snapshot_notes_snapshot', table_name='snapshot_notes')
    op.drop_table('snapshot_notes')
    op.drop_index('ix_collection_members_run', table_name='collection_members')
    op.drop_table('collection_members')
    op.drop_index('ix_collection_events_collection_at', table_name='collection_events')
    op.drop_table('collection_events')
    op.drop_index('ix_analysis_snapshots_created', table_name='analysis_snapshots')
    op.drop_index('ix_analysis_snapshots_collection', table_name='analysis_snapshots')
    op.drop_table('analysis_snapshots')
    op.drop_index('ix_collections_name', table_name='collections')
    op.drop_table('collections')
