"""Business audit events. Written in the same transaction as the action.

Never pass tokens, passwords or credential values in ``detail``.
"""

from sqlalchemy import Connection, insert

from maestro.core import clock, context
from maestro.core.ids import new_id
from maestro.db import schema as t
from maestro.services.principal import Principal


def record(
    conn: Connection,
    principal: Principal | None,
    action: str,
    *,
    object_type: str | None = None,
    object_id: str | None = None,
    outcome: str = "succeeded",
    detail: dict | None = None,
    actor_user_id: str | None = None,
) -> None:
    conn.execute(
        insert(t.audit_events).values(
            id=new_id("aud"),
            actor_user_id=principal.user_id if principal else actor_user_id,
            pat_id=principal.pat_id if principal else None,
            auth_channel=principal.channel.value if principal else None,
            action=action,
            object_type=object_type,
            object_id=object_id,
            outcome=outcome,
            detail_json=detail,
            request_id=context.request_id.get(),
            created_at=clock.to_text(clock.now()),
        )
    )
