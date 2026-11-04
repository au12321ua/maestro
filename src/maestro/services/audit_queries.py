"""Administrator view of the audit trail."""

from sqlalchemy import select

from maestro.db import schema as t
from maestro.db.engine import Database
from maestro.domain.states import PatPermission
from maestro.services import pagination
from maestro.services.principal import Principal, require_admin


def public_event(row) -> dict:
    return {
        "id": row["id"],
        "actor_user_id": row["actor_user_id"],
        "pat_id": row["pat_id"],
        "auth_channel": row["auth_channel"],
        "action": row["action"],
        "object_type": row["object_type"],
        "object_id": row["object_id"],
        "outcome": row["outcome"],
        "detail": row["detail_json"],
        "request_id": row["request_id"],
        "created_at": row["created_at"],
    }


def list_events(db: Database, principal: Principal, *, object_type, object_id, cursor, limit):
    require_admin(principal, PatPermission.AUDIT)
    limit = pagination.check_limit(limit)
    filters = {"object_type": object_type, "object_id": object_id}
    query = select(t.audit_events)
    if object_type:
        query = query.where(t.audit_events.c.object_type == object_type)
    if object_id:
        query = query.where(t.audit_events.c.object_id == object_id)
    query = pagination.page(
        query, t.audit_events.c.created_at, t.audit_events.c.id, cursor, limit, filters
    )
    with db.read() as conn:
        rows = list(conn.execute(query).mappings().all())
    rows, next_cursor = pagination.split(rows, limit, "created_at", filters)
    return [public_event(r) for r in rows], next_cursor
