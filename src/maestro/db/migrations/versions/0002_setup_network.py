"""setup network revisions (N-08)

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('setup_networks',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('value_json', sa.JSON(), nullable=False),
    sa.Column('note', sa.String(length=500), nullable=False),
    sa.Column('created_by', sa.String(length=40), nullable=False),
    sa.Column('created_at', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_setup_networks_created_by_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_setup_networks')),
    sa.UniqueConstraint('revision', name=op.f('uq_setup_networks_revision'))
    )
    with op.batch_alter_table('execution_attempts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('setup_network_revision', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('execution_attempts', schema=None) as batch_op:
        batch_op.drop_column('setup_network_revision')

    op.drop_table('setup_networks')
