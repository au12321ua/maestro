"""Deletion state lookups shared by every service.

A row in ``deletions`` means the object is in the recycle bin ('recycled') or its
content is gone ('purged'). Lists and selectors hide both; direct references
still resolve and show the state.
"""

from sqlalchemy import Column, Connection, and_, select

from maestro.core.errors import MaestroError
from maestro.db import schema as t

RECYCLED = "recycled"
PURGED = "purged"


def ids_of(object_type: str):
    return select(t.deletions.c.object_id).where(t.deletions.c.object_type == object_type)


def visible(column: Column, object_type: str):
    """Filter for lists and selectors: the object is not deleted."""
    return column.not_in(ids_of(object_type))


def version_visible(column: Column):
    """A dataset version is hidden when it or its dataset is deleted."""
    return and_(
        visible(column, "dataset_version"),
        column.not_in(
            select(t.dataset_versions.c.id).where(
                t.dataset_versions.c.dataset_id.in_(ids_of("dataset"))
            )
        ),
    )


def states(conn: Connection, object_type: str, ids) -> dict[str, dict]:
    ids = [x for x in set(ids) if x]
    if not ids:
        return {}
    rows = conn.execute(
        select(t.deletions).where(
            t.deletions.c.object_type == object_type, t.deletions.c.object_id.in_(ids)
        )
    ).mappings()
    return {r["object_id"]: public_state(r) for r in rows}


def state(conn: Connection, object_type: str, object_id: str) -> dict | None:
    return states(conn, object_type, [object_id]).get(object_id)


def public_state(row) -> dict:
    return {
        "state": row["state"],
        "deleted_at": row["deleted_at"],
        "purged_at": row["purged_at"],
    }


def version_state(conn: Connection, version_id: str) -> dict | None:
    """The version's own deletion, else its dataset's."""
    found = state(conn, "dataset_version", version_id)
    if found:
        return found
    dataset_id = conn.execute(
        select(t.dataset_versions.c.dataset_id).where(t.dataset_versions.c.id == version_id)
    ).scalar()
    return state(conn, "dataset", dataset_id) if dataset_id else None


def purged_error(kind: str) -> MaestroError:
    return MaestroError(
        "OBJECT_PURGED", f"{kind} was permanently deleted; only its receipt remains", 410
    )


def refuse_purged(conn: Connection, object_type: str, object_id: str, kind: str) -> None:
    found = state(conn, object_type, object_id)
    if found and found["state"] == PURGED:
        raise purged_error(kind)
