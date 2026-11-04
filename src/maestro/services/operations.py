"""Unified long-running Operation records.

An Operation tracks work; it never replaces the business object, grants write
permission, or offers a resource-agnostic cancel.
"""

from __future__ import annotations

from sqlalchemy import Connection, insert, or_, select, update

from maestro.core import clock
from maestro.core.errors import not_found
from maestro.core.ids import new_id
from maestro.db import schema as t
from maestro.db.engine import Database
from maestro.domain.states import OPERATION_TERMINAL, OperationStatus
from maestro.services import pagination
from maestro.services.principal import Principal


def create(
    conn: Connection,
    kind: str,
    *,
    owner_id: str | None,
    pat_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    progress: dict | None = None,
    phase: str | None = None,
) -> str:
    op_id = new_id("op")
    at = clock.to_text(clock.now())
    conn.execute(
        insert(t.operations).values(
            id=op_id,
            kind=kind,
            owner_id=owner_id,
            pat_id=pat_id,
            resource_type=resource_type,
            resource_id=resource_id,
            status=OperationStatus.ACCEPTED,
            phase=phase,
            progress_json=progress or {},
            revision=1,
            created_at=at,
            updated_at=at,
        )
    )
    return op_id


def set_resource(conn: Connection, op_id: str, resource_type: str, resource_id: str) -> None:
    conn.execute(
        update(t.operations)
        .where(t.operations.c.id == op_id)
        .values(resource_type=resource_type, resource_id=resource_id)
    )


def advance(
    conn: Connection,
    op_id: str,
    *,
    status: OperationStatus | None = None,
    phase: str | None = None,
    progress: dict | None = None,
    error: dict | None = None,
    result: dict | None = None,
) -> None:
    current = conn.execute(select(t.operations).where(t.operations.c.id == op_id)).mappings().one()
    if current["status"] in OPERATION_TERMINAL:
        return
    values: dict = {"revision": current["revision"] + 1, "updated_at": clock.to_text(clock.now())}
    if status is not None:
        values["status"] = status
        if status in OPERATION_TERMINAL:
            values["finished_at"] = values["updated_at"]
    if phase is not None:
        values["phase"] = phase
    if progress is not None:
        values["progress_json"] = progress
    if error is not None:
        values["error_json"] = error
    if result is not None:
        values["result_json"] = result
    changed = any(current[k] != v for k, v in values.items() if k not in {"revision", "updated_at"})
    if changed:
        conn.execute(update(t.operations).where(t.operations.c.id == op_id).values(**values))


def public(row, principal: Principal | None = None, allowed_actions: list[str] | None = None):
    resource = (
        {"type": row["resource_type"], "id": row["resource_id"]} if row["resource_id"] else None
    )
    links = {}
    if resource and resource["type"] in {"run", "export"}:
        links["resource"] = f"/api/v1/{resource['type']}s/{resource['id']}"
    if resource and resource["type"] == "batch":
        links["resource"] = f"/api/v1/run-batches/{resource['id']}"
    return {
        "id": row["id"],
        "kind": row["kind"],
        "resource": resource,
        "owner_id": row["owner_id"],
        "status": row["status"],
        "phase": row["phase"],
        "progress": row["progress_json"],
        "error": row["error_json"],
        "result": row["result_json"],
        "allowed_actions": allowed_actions or ["view"],
        "links": links,
        "revision": row["revision"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "finished_at": row["finished_at"],
    }


PRIVATE_KINDS = ("export",)


def _hidden_from(row, principal: Principal | None) -> bool:
    """Export Operations carry the export's id, size and digest, so like the export itself
    they belong to its owner and to administrator web sessions. Other kinds track
    team-shared work and stay visible to everyone."""
    return (
        principal is not None
        and row["kind"] in PRIVATE_KINDS
        and row["owner_id"] != principal.user_id
        and not principal.can_override
    )


class Operations:
    def __init__(self, db: Database):
        self.db = db

    def get(self, op_id: str, principal: Principal | None = None) -> dict:
        """``principal`` None is for the service's own bookkeeping; every API caller passes one."""
        with self.db.read() as conn:
            row = conn.execute(select(t.operations).where(t.operations.c.id == op_id)).mappings()
            row = row.first()
        if row is None or _hidden_from(row, principal):
            raise not_found("Operation")
        return public(row)

    def list(
        self,
        *,
        kind: str | None,
        owner_id: str | None,
        cursor: str | None,
        limit: int | None,
        principal: Principal | None = None,
    ):
        limit = pagination.check_limit(limit)
        filters = {"kind": kind, "owner_id": owner_id}
        query = select(t.operations)
        if principal is not None and not principal.can_override:
            query = query.where(
                or_(
                    t.operations.c.kind.not_in(PRIVATE_KINDS),
                    t.operations.c.owner_id == principal.user_id,
                )
            )
        if kind:
            query = query.where(t.operations.c.kind == kind)
        if owner_id:
            query = query.where(t.operations.c.owner_id == owner_id)
        query = pagination.page(
            query, t.operations.c.created_at, t.operations.c.id, cursor, limit, filters
        )
        with self.db.read() as conn:
            rows = conn.execute(query).mappings().all()
        rows, next_cursor = pagination.split(list(rows), limit, "created_at", filters)
        return [public(r) for r in rows], next_cursor
