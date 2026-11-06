"""Opaque workflow notes attached to team objects, with immutable attributed versions."""

from __future__ import annotations

import json
import re

from sqlalchemy import Connection, insert, select, update

from maestro.core import clock
from maestro.core.errors import FieldError, MaestroError, not_found
from maestro.core.ids import new_id
from maestro.db import schema as t
from maestro.db.engine import Database
from maestro.domain import limits
from maestro.domain.states import PatPermission
from maestro.security.redaction import redact
from maestro.services import audit, deleted
from maestro.services.principal import Principal, can_manage_owned, require_experiment

TARGET_TYPES = ("collection", "run", "attachment", "dataset_version")
REFERENCE_TABLES = {
    "run": t.runs,
    "trial": t.trials,
    "artifact": t.artifacts,
    "attachment_snapshot": t.attachment_versions,
    "collection": t.collections,
    "analysis_snapshot": t.analysis_snapshots,
}
CONTENT_FIELDS = ("kind", "title", "body", "references", "excerpts")


def _invalid(path: str, message: str, limit: int | None = None) -> MaestroError:
    return MaestroError(
        "RECORD_INVALID",
        message,
        422,
        field_errors=[FieldError(path, "LIMIT" if limit else "INVALID", limit=limit)],
    )


def _size(text: str, path: str, limit: int) -> int:
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError:
        raise _invalid(path, "Text must be valid UTF-8") from None
    if size > limit:
        raise _invalid(path, "Text exceeds the byte limit", limit)
    return size


def check_target(raw: dict) -> dict:
    if not isinstance(raw, dict) or set(raw) - {"type", "id", "snapshot_id"}:
        raise _invalid("/target", "Invalid record target")
    kind, identity, snapshot = raw.get("type"), raw.get("id"), raw.get("snapshot_id")
    if kind not in TARGET_TYPES or not isinstance(identity, str) or not 1 <= len(identity) <= 128:
        raise _invalid("/target", "Choose a collection, run, attachment name or dataset version")
    if snapshot is not None and (kind != "attachment" or not isinstance(snapshot, str)):
        raise _invalid("/target/snapshot_id", "Only an attachment can pin a snapshot")
    return {"type": kind, "id": identity, "snapshot_id": snapshot}


def target_state(conn: Connection, target: dict) -> dict | None:
    """Reads retain recycled targets; a purged target is set aside. Attachment names
    exist through their snapshots, and remain live while any snapshot is live."""
    kind, identity = target["type"], target["id"]
    if kind != "attachment":
        table = t.dataset_versions if kind == "dataset_version" else REFERENCE_TABLES[kind]
        if conn.execute(select(table.c.id).where(table.c.id == identity)).first() is None:
            raise not_found("Record target")
        return (
            deleted.version_state(conn, identity)
            if kind == "dataset_version"
            else deleted.state(conn, kind, identity)
        )
    query = select(t.attachment_versions.c.id).where(t.attachment_versions.c.name == identity)
    if target["snapshot_id"] is not None:
        query = query.where(t.attachment_versions.c.id == target["snapshot_id"])
    ids = list(conn.execute(query).scalars())
    if not ids:
        raise not_found("Attachment target or snapshot")
    states = deleted.states(conn, "attachment_version", ids)
    if len(states) < len(ids):
        return None
    return next((s for s in states.values() if s["state"] == deleted.RECYCLED), states[ids[0]])


def _target_access(conn: Connection, target: dict, *, write: bool = False) -> dict | None:
    state = target_state(conn, target)
    if state and state["state"] == deleted.PURGED:
        raise deleted.purged_error("Record target")
    if state and write:
        raise MaestroError("RECORD_TARGET_DELETED", "The record target is in the recycle bin", 409)
    return state


def _reference(conn: Connection, raw, path: str) -> dict:
    if not isinstance(raw, dict) or set(raw) != {"type", "id"}:
        raise _invalid(path, "A reference has a type and an id")
    kind, identity = raw["type"], raw["id"]
    if not isinstance(kind, str) or kind not in REFERENCE_TABLES or not isinstance(identity, str):
        raise _invalid(path, "Unknown reference type or invalid id")
    table = REFERENCE_TABLES[kind]
    row = conn.execute(select(table).where(table.c.id == identity)).mappings().first()
    if row is None:
        raise not_found("Referenced object")
    # Team business objects are shared with every authenticated caller, including read
    # PATs. Child identities follow their run's visibility; expired files keep theirs.
    deletion_type = "attachment_version" if kind == "attachment_snapshot" else kind
    deleted.refuse_purged(conn, deletion_type, identity, "Referenced object")
    if kind in ("trial", "artifact"):
        deleted.refuse_purged(conn, "run", row["run_id"], "Referenced run")
    return {"type": kind, "id": identity}


def _content(conn: Connection, body: dict, *, check_references: bool = True) -> dict:
    kind, title, raw = body.get("kind"), body.get("title"), body.get("body")
    if not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", kind):
        raise _invalid("/kind", "Kind must be a lowercase slug")
    if len(kind) > limits.RECORD_KIND_MAX:
        raise _invalid("/kind", "Kind is too long", limits.RECORD_KIND_MAX)
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= limits.RECORD_TITLE_MAX:
        raise _invalid("/title", "A title is required", limits.RECORD_TITLE_MAX)
    if not isinstance(raw, dict):
        raise _invalid("/body", "Body must be a JSON object")
    try:
        encoded = json.dumps(raw, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise _invalid("/body", "Body must be a JSON object") from None
    _size(encoded, "/body", limits.RECORD_BODY_MAX_BYTES)
    refs = body.get("references", [])
    if not isinstance(refs, list) or len(refs) > limits.RECORD_MAX_REFERENCES:
        raise _invalid("/references", "Too many references", limits.RECORD_MAX_REFERENCES)
    if check_references:
        refs = [_reference(conn, ref, f"/references/{i}") for i, ref in enumerate(refs)]
    excerpts = body.get("excerpts", [])
    if not isinstance(excerpts, list) or len(excerpts) > limits.RECORD_MAX_EXCERPTS:
        raise _invalid("/excerpts", "Too many excerpts", limits.RECORD_MAX_EXCERPTS)
    found, total = [], 0
    for i, excerpt in enumerate(excerpts):
        path = f"/excerpts/{i}"
        if not isinstance(excerpt, dict) or set(excerpt) - {"text", "reference"}:
            raise _invalid(path, "An excerpt has text and an optional reference")
        text = excerpt.get("text")
        if not isinstance(text, str) or not text.strip():
            raise _invalid(path + "/text", "Excerpt text is required")
        before = _size(text, path + "/text", limits.RECORD_EXCERPT_MAX_BYTES)
        text = redact(text)
        after = _size(text, path + "/text", limits.RECORD_EXCERPT_MAX_BYTES)
        total += max(before, after)
        ref = excerpt.get("reference")
        if ref is not None and ref not in refs:
            raise _invalid(path + "/reference", "Excerpt reference must be in references")
        found.append({"text": text, "reference": ref})
    if total > limits.RECORD_EXCERPTS_MAX_BYTES:
        raise _invalid(
            "/excerpts", "Excerpts exceed the total byte limit", limits.RECORD_EXCERPTS_MAX_BYTES
        )
    return {
        "kind": kind,
        "title": title.strip(),
        "body": raw,
        "references": refs,
        "excerpts": found,
    }


def _values(content: dict) -> dict:
    return {
        "kind": content["kind"],
        "title": content["title"],
        **{f + "_json": content[f] for f in ("body", "references", "excerpts")},
    }


def _from_row(row) -> dict:
    return {
        "kind": row["kind"],
        "title": row["title"],
        **{f: row[f + "_json"] for f in ("body", "references", "excerpts")},
    }


def _target(row) -> dict:
    return {
        "type": row["target_type"],
        "id": row["target_id"],
        "snapshot_id": row["target_snapshot_id"],
    }


class Records:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _query():
        return select(t.workflow_records, t.users.c.username.label("author_username")).join(
            t.users, t.users.c.id == t.workflow_records.c.author_id
        )

    def _row(self, conn: Connection, record_id: str):
        row = (
            conn.execute(self._query().where(t.workflow_records.c.id == record_id))
            .mappings()
            .first()
        )
        if row is None:
            raise not_found("Workflow record")
        deleted.refuse_purged(conn, "workflow_record", record_id, "Workflow record")
        return row

    @staticmethod
    def _public(conn: Connection, principal: Principal, row) -> dict:
        state = _target_access(conn, _target(row))
        deletion = deleted.state(conn, "workflow_record", row["id"])
        allowed = ["view"]
        if (
            not state
            and not deletion
            and row["author_id"] == principal.user_id
            and principal.has(PatPermission.EXPERIMENT)
        ):
            allowed.append("edit")
        if can_manage_owned(principal, row["author_id"]):
            allowed.append("delete")
        return {
            "id": row["id"],
            "target": _target(row),
            **_from_row(row),
            "author_id": row["author_id"],
            "author_username": row["author_username"],
            "pat_id": row["pat_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "revision": row["revision"],
            "deletion": deletion,
            "target_deletion": state,
            "allowed_actions": allowed,
        }

    @staticmethod
    def _version(conn, principal, record_id, revision, content, at):
        conn.execute(
            insert(t.workflow_record_revisions).values(
                record_id=record_id,
                revision=revision,
                **_values(content),
                actor_id=principal.user_id,
                pat_id=principal.pat_id,
                at=at,
            )
        )

    def create(self, principal: Principal, body: dict) -> dict:
        require_experiment(principal)
        target = check_target(body.get("target"))
        with self.db.write() as conn:
            _target_access(conn, target, write=True)
            content = _content(conn, body)
            identity, at = new_id("rec"), clock.to_text(clock.now())
            conn.execute(
                insert(t.workflow_records).values(
                    id=identity,
                    target_type=target["type"],
                    target_id=target["id"],
                    target_snapshot_id=target["snapshot_id"],
                    **_values(content),
                    author_id=principal.user_id,
                    pat_id=principal.pat_id,
                    revision=1,
                    created_at=at,
                    updated_at=at,
                )
            )
            self._version(conn, principal, identity, 1, content, at)
            audit.record(
                conn, principal, "record.create", object_type="workflow_record", object_id=identity
            )
        return self.get(principal, identity)

    def get(self, principal: Principal, record_id: str) -> dict:
        with self.db.read() as conn:
            return self._public(conn, principal, self._row(conn, record_id))

    def list(self, principal: Principal, target: dict, kind: str | None = None) -> list[dict]:
        target = check_target(target)
        with self.db.read() as conn:
            state = target_state(conn, target)
            if state and state["state"] == deleted.PURGED:
                return []
            query = self._query().where(
                t.workflow_records.c.target_type == target["type"],
                t.workflow_records.c.target_id == target["id"],
                deleted.visible(t.workflow_records.c.id, "workflow_record"),
            )
            if target["snapshot_id"] is not None:
                query = query.where(
                    t.workflow_records.c.target_snapshot_id == target["snapshot_id"]
                )
            if kind is not None:
                query = query.where(t.workflow_records.c.kind == kind)
            rows = conn.execute(
                query.order_by(t.workflow_records.c.created_at, t.workflow_records.c.id)
            ).mappings()
            result = []
            for row in rows:
                state = target_state(conn, _target(row))
                if not state or state["state"] != deleted.PURGED:
                    result.append(self._public(conn, principal, row))
            return result

    def update(self, principal: Principal, record_id: str, body: dict) -> dict:
        require_experiment(principal)
        with self.db.write() as conn:
            row = self._row(conn, record_id)
            if row["author_id"] != principal.user_id:
                raise MaestroError("FORBIDDEN", "Only the author edits a workflow record", 403)
            _target_access(conn, _target(row), write=True)
            if deleted.state(conn, "workflow_record", record_id):
                raise MaestroError("RECORD_DELETED", "This record is in the recycle bin", 409)
            if body.get("expected_revision") != row["revision"]:
                raise MaestroError(
                    "RECORD_REVISION_CHANGED",
                    "The record changed; reload it before editing",
                    409,
                    field_errors=[FieldError("/expected_revision", "STALE")],
                )
            merged = {**_from_row(row), **{k: body[k] for k in CONTENT_FIELDS if k in body}}
            # References are checked when given. Kept ones were checked when stored, and a
            # later purge of their object must not block editing the rest of the record.
            content = _content(conn, merged, check_references="references" in body)
            revision, at = row["revision"] + 1, clock.to_text(clock.now())
            conn.execute(
                update(t.workflow_records)
                .where(t.workflow_records.c.id == record_id)
                .values(
                    **_values(content),
                    revision=revision,
                    updated_at=at,
                )
            )
            self._version(conn, principal, record_id, revision, content, at)
            audit.record(
                conn,
                principal,
                "record.update",
                object_type="workflow_record",
                object_id=record_id,
                detail={"revision": revision},
            )
        return self.get(principal, record_id)

    def history(self, principal: Principal, record_id: str) -> list[dict]:
        with self.db.read() as conn:
            row = self._row(conn, record_id)
            _target_access(conn, _target(row))
            rows = conn.execute(
                select(t.workflow_record_revisions, t.users.c.username.label("actor_username"))
                .join(t.users, t.users.c.id == t.workflow_record_revisions.c.actor_id)
                .where(t.workflow_record_revisions.c.record_id == record_id)
                .order_by(t.workflow_record_revisions.c.revision)
            ).mappings()
            return [
                {
                    **_from_row(r),
                    "revision": r["revision"],
                    "actor_id": r["actor_id"],
                    "actor_username": r["actor_username"],
                    "pat_id": r["pat_id"],
                    "at": r["at"],
                }
                for r in rows
            ]
