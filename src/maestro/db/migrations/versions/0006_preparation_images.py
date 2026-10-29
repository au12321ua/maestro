"""preparations as image lists in the local image registry (N-01)

Not converted (U-53): every task goes back to "pending" under the new preparation
method and its old evidence is dropped; a revoked task stays revoked. Cache rows, their
waiters and pins, and environment image availability go too; the results of earlier
preparation Operations (single images and their originals) are cleared, their status and
errors stay. The administrator runs the first preparation again, which also inspects each
task again. The ``docker save`` originals under ``preparations/`` are removed at the next
start.

Revision ID: 0006
Revises: 0005
"""

from alembic import op

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None

# maestro.execution.materials.PREPARATION_METHOD_DIGEST of this release
# ("maestro-docker-prep/2"), written as a value: a later method gets its own migration.
METHOD = "sha256:0533b601d2101ee880f7859a7239f6e96ecbcd4bcbc78b630d820e48c9be5f37"


def upgrade() -> None:
    op.execute("DELETE FROM preparation_pins")
    op.execute("DELETE FROM preparation_waiters")
    op.execute("DELETE FROM preparations")
    op.execute("DELETE FROM content_availability WHERE resource_kind = 'environment_image'")
    op.execute(
        "UPDATE operations SET result_json = NULL "
        "WHERE kind IN ('preparation_first', 'preparation_restore')"
    )
    op.execute(
        "UPDATE task_admissions SET "
        f"preparation_method_digest = '{METHOD}', "
        "state = CASE WHEN state = 'revoked' THEN 'revoked' ELSE 'pending' END, "
        "revision = revision + 1, evidence_json = '{}', "
        "approved_by = NULL, approved_at = NULL"
    )


def downgrade() -> None:
    raise NotImplementedError("prepared images are not converted back (U-53)")
