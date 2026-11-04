"""PAT permissions instead of the read / experiment scope

A read token becomes one with no extra permissions (every token may read) and an
experiment token one with the experiment permission, so existing tokens keep working.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('pats', schema=None) as batch_op:
        batch_op.add_column(sa.Column('permissions_json', sa.JSON(), nullable=True))
    op.execute(
        "UPDATE pats SET permissions_json = CASE WHEN scope = 'experiment' "
        "THEN '[\"experiment\"]' ELSE '[]' END"
    )
    with op.batch_alter_table('pats', schema=None) as batch_op:
        batch_op.alter_column('permissions_json', existing_type=sa.JSON(), nullable=False)
        batch_op.drop_constraint('scope', type_='check')
        batch_op.drop_column('scope')


def downgrade() -> None:
    raise NotImplementedError("permissions beyond experiment have no read / experiment scope")
