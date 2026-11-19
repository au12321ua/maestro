"""Explicit experiment references and bounded reads of the objects they organize."""

from collections import defaultdict

from sqlalchemy import Connection, delete, func, insert, literal, select, tuple_, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from maestro.core import clock
from maestro.core.errors import FieldError, MaestroError
from maestro.core.ids import new_id
from maestro.db import schema as t
from maestro.db.engine import Database
from maestro.domain.scoring import compact
from maestro.domain.states import RUN_TERMINAL, PatPermission
from maestro.services import audit, deleted, pagination, results
from maestro.services.principal import Principal, can_manage_owned, require_experiment
from maestro.services.records import Records

PREVIEW_SIZE = 8
HANDOFF_KIND = "handoff"
REFERENCE_TYPES = {
    "run": (t.runs, "run"),
    "collection": (t.collections, "collection"),
    "dataset": (t.datasets, "dataset"),
    "attachment_snapshot": (t.attachment_versions, "attachment_version"),
}


def topic_objects(topic_id: str, kind: str):
    refs = t.topic_references.c
    return select(refs.object_id).where(refs.topic_id == topic_id, refs.kind == kind)


def require_topic(conn: Connection, topic_id: str, *, write: bool = False):
    row = conn.execute(select(t.topics).where(t.topics.c.id == topic_id)).mappings().first()
    if row is None:
        raise MaestroError(
            "TOPIC_NOT_FOUND",
            "Experiment topic does not exist",
            404,
            field_errors=[FieldError("/topic_id", "NOT_FOUND")],
        )
    mark = deleted.state(conn, "topic", topic_id)
    if write and mark:
        raise MaestroError(
            "TOPIC_DELETED",
            "Experiment topic has been deleted",
            409 if mark["state"] == deleted.RECYCLED else 410,
            field_errors=[FieldError("/topic_id", "DELETED")],
        )
    if mark and mark["state"] == deleted.PURGED:
        raise deleted.purged_error("Experiment topic")
    return row, mark


def _add_references(conn: Connection, topic_id: str, objects: set[tuple[str, str]]) -> int:
    if not objects:
        return 0
    at = clock.to_text(clock.now())
    return conn.execute(
        sqlite_insert(t.topic_references).on_conflict_do_nothing(),
        [
            {"topic_id": topic_id, "kind": kind, "object_id": identity, "added_at": at}
            for kind, identity in objects
        ],
    ).rowcount


def link_run(conn: Connection, topic_id: str, run_id: str) -> None:
    """The caller must validate the topic in the same write transaction."""
    _add_references(conn, topic_id, {("run", run_id)})


def _public(row, principal: Principal, mark: dict | None = None) -> dict:
    allowed = ["view"]
    if not mark and principal.has(PatPermission.EXPERIMENT):
        allowed += ["edit", "manage_references", "submit"]
    if not mark and can_manage_owned(principal, row["owner_id"]):
        allowed.append("delete")
    return {
        **{k: row[k] for k in ("id", "name", "purpose", "owner_id", "created_at", "updated_at")},
        "settings": row["settings_json"],
        "web_path": f"/topics/{row['id']}",
        "allowed_actions": allowed,
        "deletion": mark,
    }


def _changes(body: dict) -> dict:
    values = {}
    if "name" in body:
        name = body["name"].strip()
        if not name:
            raise MaestroError(
                "TOPIC_INVALID",
                "Enter a name for the experiment topic",
                422,
                field_errors=[FieldError("/name", "REQUIRED")],
            )
        values["name"] = name
    if "purpose" in body:
        values["purpose"] = body["purpose"].strip()
    if "settings" in body:
        values["settings_json"] = body["settings"]
    return values


def _reference_page(conn, topic_id, kind, cursor, limit):
    table, deletion_type = REFERENCE_TYPES[kind]
    c = table.c
    refs = t.topic_references.c
    name = c.namespace + "/" + c.name if kind == "dataset" else c.name
    label = c.label if kind == "attachment_snapshot" else literal(None)
    filters = {"topic_id": topic_id, "kind": kind}
    query = (
        select(c.id, name.label("name"), label.label("label"), refs.added_at)
        .select_from(t.topic_references)
        .join(table, c.id == refs.object_id)
        .where(refs.topic_id == topic_id, refs.kind == kind)
    )
    rows = (
        conn.execute(pagination.page(query, refs.added_at, c.id, cursor, limit, filters))
        .mappings()
        .all()
    )
    rows, next_cursor = pagination.split(rows, limit, "added_at", filters)
    marks = deleted.states(conn, deletion_type, [r["id"] for r in rows])
    return [{**r, "kind": kind, "deletion": marks.get(r["id"])} for r in rows], next_cursor


def _live_runs(topic_id: str):
    return (
        t.runs.c.id.in_(topic_objects(topic_id, "run")),
        deleted.visible(t.runs.c.id, "run"),
    )


def _material_page(conn, topic_id, kind, cursor, limit):
    """Use the accepted dataset version and attachment rows, with no reverse discovery."""
    runs = t.runs.c
    if kind == "dataset_version":
        table = t.dataset_versions
        key = runs.dataset_version_id
        usage = select(key.label("material_id"), runs.id.label("run_id"), runs.accepted_at)
        label = func.coalesce(table.c.label, table.c.upstream_ref)
        name = t.datasets.c.namespace + "/" + t.datasets.c.name
    else:
        table = t.attachment_versions
        key = t.run_attachments.c.attachment_version_id
        usage = (
            select(key.label("material_id"), runs.id.label("run_id"), runs.accepted_at)
            .select_from(t.runs)
            .join(t.run_attachments, t.run_attachments.c.run_id == runs.id)
        )
        label = table.c.label
        name = table.c.name
    usage = usage.where(*_live_runs(topic_id)).distinct().cte("material_usage")
    counts = (
        select(usage.c.material_id, func.count().label("source_run_count"))
        .group_by(usage.c.material_id)
        .subquery()
    )
    c = table.c
    query = (
        select(
            c.id, c.created_at, name.label("name"), label.label("label"), counts.c.source_run_count
        )
        .select_from(table)
        .join(counts, counts.c.material_id == c.id)
    )
    if kind == "dataset_version":
        query = query.join(t.datasets, t.datasets.c.id == c.dataset_id).add_columns(c.dataset_id)
    filters = {"topic_id": topic_id, "material_kind": kind}
    rows = (
        conn.execute(pagination.page(query, c.created_at, c.id, cursor, limit, filters))
        .mappings()
        .all()
    )
    rows, next_cursor = pagination.split(rows, limit, "created_at", filters)
    ids = [row["id"] for row in rows]
    if not ids:
        return [], next_cursor
    ranked = (
        select(
            usage.c.material_id,
            usage.c.run_id,
            func.row_number()
            .over(
                partition_by=usage.c.material_id,
                order_by=(usage.c.accepted_at.desc(), usage.c.run_id.desc()),
            )
            .label("position"),
        )
        .where(usage.c.material_id.in_(ids))
        .subquery()
    )
    sources = defaultdict(list)
    for material_id, run_id in conn.execute(
        select(ranked.c.material_id, ranked.c.run_id)
        .where(ranked.c.position <= PREVIEW_SIZE)
        .order_by(ranked.c.position)
    ):
        sources[material_id].append(run_id)
    marks = deleted.states(
        conn, "dataset_version" if kind == "dataset_version" else "attachment_version", ids
    )
    parent_marks = deleted.states(conn, "dataset", [r.get("dataset_id") for r in rows])
    return [
        {
            "kind": kind,
            "id": r["id"],
            "name": r["name"],
            "label": r["label"],
            "deletion": marks.get(r["id"]) or parent_marks.get(r.get("dataset_id")),
            "source_run_count": r["source_run_count"],
            "source_run_ids": sources[r["id"]],
        }
        for r in rows
    ], next_cursor


def _run_preview(conn: Connection, query):
    c = t.runs.c
    return (
        conn.execute(query.order_by(c.accepted_at.desc(), c.id.desc()).limit(PREVIEW_SIZE))
        .mappings()
        .all()
    )


class Topics:
    def __init__(self, db: Database):
        self.db = db

    def create(self, principal: Principal, body: dict) -> dict:
        require_experiment(principal)
        at = clock.to_text(clock.now())
        row = {
            "id": new_id("top"),
            "owner_id": principal.user_id,
            "purpose": "",
            "settings_json": {},
            **_changes(body),
            "created_at": at,
            "updated_at": at,
        }
        with self.db.write() as conn:
            conn.execute(insert(t.topics).values(**row))
            audit.record(conn, principal, "topic.create", object_type="topic", object_id=row["id"])
        return _public(row, principal)

    def update(self, principal: Principal, topic_id: str, body: dict) -> dict:
        require_experiment(principal)
        values = _changes(body)
        with self.db.write() as conn:
            row, _ = require_topic(conn, topic_id, write=True)
            if values:
                values["updated_at"] = clock.to_text(clock.now())
                conn.execute(update(t.topics).where(t.topics.c.id == topic_id).values(**values))
                audit.record(
                    conn, principal, "topic.update", object_type="topic", object_id=topic_id
                )
            return _public({**row, **values}, principal)

    def get(self, principal: Principal, topic_id: str) -> dict:
        with self.db.read() as conn:
            row, mark = require_topic(conn, topic_id)
            return _public(row, principal, mark)

    def list(self, principal: Principal, q: str | None, cursor: str | None, limit: int | None):
        limit = pagination.check_limit(limit)
        c = t.topics.c
        filters = {"q": q}
        query = select(t.topics).where(deleted.visible(c.id, "topic"))
        if q:
            query = query.where(pagination.matching(q, c.name, c.id))
        with self.db.read() as conn:
            rows = (
                conn.execute(pagination.page(query, c.created_at, c.id, cursor, limit, filters))
                .mappings()
                .all()
            )
        rows, next_cursor = pagination.split(rows, limit, "created_at", filters)
        return [_public(row, principal) for row in rows], next_cursor

    def overview(self, principal: Principal, topic_id: str) -> dict:
        c = t.runs.c
        with self.db.read() as conn:
            row, mark = require_topic(conn, topic_id)
            scope = _live_runs(topic_id)
            active = c.state.not_in(RUN_TERMINAL)
            query = select(c.id, c.name, c.state, c.accepted_at).where(*scope)
            active_runs = _run_preview(conn, query.where(active))
            recent_runs = _run_preview(conn, query)
            summaries = results.summaries(
                conn, list({run["id"] for run in (*active_runs, *recent_runs)})
            )
            return {
                "topic": _public(row, principal, mark),
                "latest_handoff": Records.latest(conn, principal, "topic", topic_id, HANDOFF_KIND),
                "active_run_count": conn.scalar(select(func.count()).where(*scope, active)),
                "active_runs": [{**r, "scoring": compact(summaries[r["id"]])} for r in active_runs],
                "recent_runs": [{**r, "scoring": compact(summaries[r["id"]])} for r in recent_runs],
                "collections": _reference_page(conn, topic_id, "collection", None, PREVIEW_SIZE)[0],
                "datasets": _reference_page(conn, topic_id, "dataset", None, PREVIEW_SIZE)[0],
                "attachments": _reference_page(
                    conn, topic_id, "attachment_snapshot", None, PREVIEW_SIZE
                )[0],
                "used_dataset_versions": _material_page(
                    conn, topic_id, "dataset_version", None, PREVIEW_SIZE
                )[0],
                "used_attachment_snapshots": _material_page(
                    conn, topic_id, "attachment_snapshot", None, PREVIEW_SIZE
                )[0],
            }

    def references(self, topic_id: str, kind: str, cursor: str | None, limit: int | None):
        limit = pagination.check_limit(limit)
        with self.db.read() as conn:
            require_topic(conn, topic_id)
            return _reference_page(conn, topic_id, kind, cursor, limit)

    def materials(self, topic_id: str, kind: str, cursor: str | None, limit: int | None):
        limit = pagination.check_limit(limit)
        with self.db.read() as conn:
            require_topic(conn, topic_id)
            return _material_page(conn, topic_id, kind, cursor, limit)

    def change_references(self, principal: Principal, topic_id: str, body: dict) -> dict:
        require_experiment(principal)
        adds = {(item["kind"], item["id"]) for item in body["add"]}
        removes = {(item["kind"], item["id"]) for item in body["remove"]}
        if adds & removes:
            raise MaestroError(
                "TOPIC_INVALID", "A reference cannot be added and removed together", 422
            )
        refs = t.topic_references.c
        with self.db.write() as conn:
            require_topic(conn, topic_id, write=True)
            for kind, (table, deletion_type) in REFERENCE_TYPES.items():
                ids = {identity for k, identity in adds if k == kind}
                if not ids:
                    continue
                live = set(
                    conn.execute(
                        select(table.c.id).where(
                            table.c.id.in_(ids), deleted.visible(table.c.id, deletion_type)
                        )
                    ).scalars()
                )
                if missing := ids - live:
                    raise MaestroError(
                        "TOPIC_REFERENCE_UNAVAILABLE",
                        "A referenced object is missing or deleted",
                        409,
                        field_errors=[
                            FieldError("/add", "UNAVAILABLE", ", ".join(sorted(missing)))
                        ],
                    )
            added = _add_references(conn, topic_id, adds)
            removed = 0
            if removes:
                removed = conn.execute(
                    delete(t.topic_references).where(
                        refs.topic_id == topic_id, tuple_(refs.kind, refs.object_id).in_(removes)
                    )
                ).rowcount
            if added or removed:
                audit.record(
                    conn,
                    principal,
                    "topic.references",
                    object_type="topic",
                    object_id=topic_id,
                    detail={"added": added, "removed": removed},
                )
            return {"added": added, "removed": removed}
