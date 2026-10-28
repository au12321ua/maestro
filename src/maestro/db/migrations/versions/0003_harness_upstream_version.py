"""harness upstream version (N-09)

The version OpenCode installs moves from a harness parameter (``version``, defaulting to
1.18.35 in the Runner) into the harness version itself; members no longer choose it per
run. Existing OpenCode versions keep installing what they installed, and their content
digest is recomputed to cover it.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

from maestro.core.digests import digest_json

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None

# What the Runner installed when an OpenCode harness version declared no version.
OPENCODE_DEFAULT = "1.18.35"


def upgrade() -> None:
    with op.batch_alter_table('harness_versions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('upstream_version', sa.String(length=64), nullable=True))
    table = sa.table(
        'harness_versions',
        sa.column('id', sa.String),
        sa.column('name', sa.String),
        sa.column('version', sa.String),
        sa.column('agent', sa.String),
        sa.column('protocols_json', sa.JSON),
        sa.column('schema_json', sa.JSON),
        sa.column('content_digest', sa.String),
        sa.column('upstream_version', sa.String),
    )
    conn = op.get_bind()
    for row in conn.execute(sa.select(table).where(table.c.agent == 'opencode')).mappings():
        schema = dict(row['schema_json'] or {})
        parameters = dict(schema.get('parameters') or {})
        declared = parameters.pop('version', None) or {}
        upstream = declared.get('default') or OPENCODE_DEFAULT
        schema['parameters'] = parameters
        digest = digest_json(
            {
                'agent': row['agent'],
                'name': row['name'],
                'version': row['version'],
                'protocols': row['protocols_json'],
                'schema': schema,
                'upstream_version': upstream,
            }
        )
        conn.execute(
            table.update()
            .where(table.c.id == row['id'])
            .values(schema_json=schema, upstream_version=upstream, content_digest=digest)
        )


def downgrade() -> None:
    with op.batch_alter_table('harness_versions', schema=None) as batch_op:
        batch_op.drop_column('upstream_version')
