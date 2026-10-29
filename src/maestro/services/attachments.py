"""Attachment snapshots (03 sections 2 and 6): members publish fixed file lists.

Any member or experiment PAT may publish; everyone may read and choose any enabled
snapshot (results are team-wide). Publishing is incremental: the client asks which
contents the store lacks, uploads only those, then commits the file list. A snapshot is
identified by its content digest, so the same content published again returns the
existing snapshot; the optional label names it within its attachment and never moves,
and the optional parent only records what it was made from (snapshots may branch). The
owner or an administrator may disable a snapshot or put it in the recycle bin.
Publishing never runs the package's script: installation only happens inside accepted
runs (03 section 7).
"""

from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from datetime import timedelta
from pathlib import Path

from sqlalchemy import Connection, delete, func, insert, select, update

from maestro.core import clock
from maestro.core.errors import FieldError, MaestroError, not_found
from maestro.core.ids import new_id
from maestro.db import schema as t
from maestro.db.engine import Database
from maestro.domain import limits
from maestro.execution.attachments import (
    LABEL,
    SHA256,
    AttachmentStore,
    PackageError,
    display,
)
from maestro.services import audit, deleted
from maestro.services.principal import Principal, require_experiment, require_manage_owned

# Contents a query names at once in ``IN (...)``.
_CHUNK = 500


def _not_purged():
    return t.attachment_versions.c.id.not_in(
        select(t.deletions.c.object_id).where(
            t.deletions.c.object_type == "attachment_version",
            t.deletions.c.state == deleted.PURGED,
        )
    )


def _chunks(values: list[str]):
    for start in range(0, len(values), _CHUNK):
        yield values[start : start + _CHUNK]


def _held(conn: Connection, shas: list[str], user_id: str | None = None) -> set[str]:
    """The contents a snapshot holds, or an upload does (any live upload of ``user_id``;
    with no user, any upload row at all)."""
    found: set[str] = set()
    now = clock.to_text(clock.now())
    for chunk in _chunks(shas):
        found |= set(
            conn.execute(
                select(t.attachment_files.c.sha256).where(t.attachment_files.c.sha256.in_(chunk))
            ).scalars()
        )
        uploads = select(t.attachment_uploads.c.sha256).where(
            t.attachment_uploads.c.sha256.in_(chunk)
        )
        if user_id is not None:
            uploads = uploads.where(
                t.attachment_uploads.c.user_id == user_id,
                t.attachment_uploads.c.expires_at > now,
            )
        found |= set(conn.execute(uploads).scalars())
    return found


def release_contents(db: Database, store: AttachmentStore, shas) -> int:
    """Remove the contents no snapshot and no upload holds any more. Checked and moved
    away under the write lock, so a publish or upload of the same content in between
    keeps it; deleting happens after."""
    shas = sorted(set(shas))
    retired = []
    with db.write() as conn:
        held = _held(conn, shas)
        for sha in shas:
            if sha not in held and (path := store.retire_blob(sha)) is not None:
                retired.append(path)
    for path in retired:
        shutil.rmtree(path, ignore_errors=True)
    return len(retired)


class Attachments:
    def __init__(self, db: Database, store: AttachmentStore, adapters=None):
        self.db = db
        self.store = store
        # Harness agents the executor can run; None when there is no executor.
        self.adapters = adapters

    @staticmethod
    def _public(row, owner: str | None) -> dict:
        manifest = row["manifest_json"]
        return {
            "id": row["id"],
            "name": row["name"],
            "label": row["label"],
            "parent_id": row["parent_id"],
            "description": row["description"],
            "owner_id": row["owner_id"],
            "owner_username": owner,
            "content_digest": row["content_digest"],
            "effects": manifest["effects"],
            "compatible_harnesses": manifest["compatible_harnesses"],
            "provides": manifest["provides"],
            "has_bootstrap": row["has_bootstrap"],
            "file_count": row["file_count"],
            "size_bytes": row["size_bytes"],
            "enabled": row["enabled"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _select():
        return select(t.attachment_versions, t.users.c.username).join(
            t.users, t.users.c.id == t.attachment_versions.c.owner_id
        )

    def list(
        self, principal: Principal, name: str | None = None, include_disabled: bool = True
    ) -> list[dict]:
        query = self._select().where(
            deleted.visible(t.attachment_versions.c.id, "attachment_version")
        )
        if name:
            query = query.where(t.attachment_versions.c.name == name)
        if not include_disabled:
            query = query.where(t.attachment_versions.c.enabled.is_(True))
        query = query.order_by(t.attachment_versions.c.name, t.attachment_versions.c.created_at)
        with self.db.read() as conn:
            return [self._public(r, r["username"]) for r in conn.execute(query).mappings()]

    def _row(self, conn: Connection, attachment_id: str):
        row = (
            conn.execute(self._select().where(t.attachment_versions.c.id == attachment_id))
            .mappings()
            .first()
        )
        if row is None:
            raise not_found("Attachment")
        return row

    def _readable(self, conn: Connection, attachment_id: str):
        """Any snapshot not purged: what a purged one held is gone for readers, even
        while another snapshot still holds the same contents."""
        row = self._row(conn, attachment_id)
        deleted.refuse_purged(conn, "attachment_version", attachment_id, "Attachment")
        return row

    def get(self, principal: Principal, attachment_id: str) -> dict:
        with self.db.read() as conn:
            row = self._readable(conn, attachment_id)
            return {
                **self._public(row, row["username"]),
                "files": row["files_json"],
                "deletion": deleted.state(conn, "attachment_version", attachment_id),
            }

    # --- uploading -----------------------------------------------------------------------

    def missing(self, principal: Principal, shas) -> list[str]:
        """Which of these contents a publish by this principal would still need: those no
        snapshot holds and this user has not uploaded (or whose stored copy is gone)."""
        require_experiment(principal)
        if (
            not isinstance(shas, list)
            or len(shas) > limits.ATTACHMENT_MAX_FILES
            or not all(isinstance(s, str) and SHA256.fullmatch(s) for s in shas)
        ):
            raise MaestroError(
                "VALIDATION_FAILED",
                f"At most {limits.ATTACHMENT_MAX_FILES} SHA-256 values in lowercase hex",
                422,
                field_errors=[FieldError("/sha256", "INVALID")],
            )
        wanted = sorted(set(shas))
        with self.db.read() as conn:
            known = _held(conn, wanted, principal.user_id)
        return [s for s in wanted if s not in known or self.store.blob(s) is None]

    @staticmethod
    def _upload_allowance(conn: Connection, user_id: str, sha256: str) -> int:
        published = select(t.attachment_files.c.sha256)
        if conn.execute(published.where(t.attachment_files.c.sha256 == sha256)).first():
            return limits.ATTACHMENT_FILE_MAX_BYTES
        staged = conn.execute(
            select(func.coalesce(func.sum(t.attachment_uploads.c.size), 0)).where(
                t.attachment_uploads.c.user_id == user_id,
                t.attachment_uploads.c.expires_at > clock.to_text(clock.now()),
                t.attachment_uploads.c.sha256 != sha256,
                t.attachment_uploads.c.sha256.not_in(published),
            )
        ).scalar_one()
        return max(0, limits.ATTACHMENT_STAGED_MAX_BYTES - staged)

    def upload_allowance(self, principal: Principal, sha256: str) -> int:
        """Bytes this upload may add; retrying the same SHA does not charge it twice."""
        with self.db.read() as conn:
            return self._upload_allowance(conn, principal.user_id, sha256)

    def add_upload(self, principal: Principal, sha256: str, source: Path, size: int) -> None:
        """Keep a received file whose SHA-256 the caller checked, for this user's next
        publish; it expires unless a snapshot holds it by then."""
        require_experiment(principal)
        expires = clock.to_text(clock.now() + timedelta(hours=limits.ATTACHMENT_UPLOAD_HOURS))
        key = (
            t.attachment_uploads.c.sha256 == sha256,
            t.attachment_uploads.c.user_id == principal.user_id,
        )
        try:
            with self.db.write() as conn:
                if size > self._upload_allowance(conn, principal.user_id, sha256):
                    raise MaestroError(
                        "ATTACHMENT_TOO_LARGE",
                        "Unpublished uploads are over the limit; publish or wait for them to expire",
                        413,
                    )
                self.store.add_blob(source, sha256)
                if conn.execute(select(t.attachment_uploads.c.sha256).where(*key)).first():
                    conn.execute(
                        update(t.attachment_uploads).where(*key).values(expires_at=expires)
                    )
                else:
                    conn.execute(
                        insert(t.attachment_uploads).values(
                            sha256=sha256, user_id=principal.user_id, size=size, expires_at=expires
                        )
                    )
        finally:
            source.unlink(missing_ok=True)

    def sweep_uploads(self) -> int:
        """Forget expired uploads and remove the contents nothing holds any more."""
        now = clock.to_text(clock.now())
        expired = t.attachment_uploads.c.expires_at <= now
        with self.db.write() as conn:
            shas = list(
                conn.execute(select(t.attachment_uploads.c.sha256).where(expired)).scalars()
            )
            if shas:
                conn.execute(delete(t.attachment_uploads).where(expired))
        return release_contents(self.db, self.store, shas) if shas else 0

    # --- publishing ----------------------------------------------------------------------

    def publish(self, principal: Principal, body: dict) -> tuple[dict, bool]:
        """Publish ``{files, label?, parent_id?}``. Returns the snapshot and whether it
        was created now."""
        require_experiment(principal)
        label = body.get("label")
        parent_id = body.get("parent_id")
        if label is not None and not (isinstance(label, str) and LABEL.fullmatch(label)):
            raise MaestroError(
                "VALIDATION_FAILED",
                "A label is letters, digits and ._+- (at most 64)",
                422,
                field_errors=[FieldError("/label", "PATTERN")],
            )
        try:
            package = self.store.check(
                body.get("files"), limits.ATTACHMENT_MAX_FILES, limits.ATTACHMENT_MAX_BYTES
            )
        except PackageError as error:
            raise MaestroError(
                "ATTACHMENT_INVALID",
                str(error),
                422,
                field_errors=[FieldError(p, code, message) for p, code, message in error.problems],
            ) from None
        unknown = [
            name
            for name in package.manifest["compatible_harnesses"]
            if name != "*" and self.adapters is not None and name not in self.adapters
        ]
        if unknown:
            raise MaestroError(
                "ATTACHMENT_INVALID",
                "The executor has no harness of this name",
                422,
                field_errors=[
                    FieldError("/manifest/compatible_harnesses", "UNSUPPORTED", name)
                    for name in unknown
                ],
            )
        shas = sorted({f["sha256"] for f in package.files})
        with self.db.write() as conn:
            held = _held(conn, shas, principal.user_id)
            lacking = [
                f["path"]
                for f in package.files
                if f["sha256"] not in held or self.store.blob(f["sha256"]) is None
            ]
            if lacking:
                raise MaestroError(
                    "ATTACHMENT_CONTENT_MISSING",
                    "Upload these files' contents first",
                    409,
                    field_errors=[FieldError(f"/files/{p}", "MISSING") for p in lacking[:50]],
                    retryable=True,
                )
            existing = conn.execute(
                select(t.attachment_versions.c.id).where(
                    t.attachment_versions.c.content_digest == package.content_digest,
                    _not_purged(),
                )
            ).first()
            if existing is None:
                attachment_id = self._insert(conn, principal, package, label, parent_id, shas)
                created = True
            elif deleted.state(conn, "attachment_version", existing.id):
                raise MaestroError(
                    "ATTACHMENT_DELETED",
                    "A snapshot with this content is in the recycle bin; restore it",
                    409,
                    suggested_actions=[{"type": "restore", "attachment_id": existing.id}],
                )
            else:
                # Publishing again repairs a file list that went missing.
                self.store.record(package)
                attachment_id, created = existing.id, False
        return self.get(principal, attachment_id), created

    def _insert(self, conn: Connection, principal, package, label, parent_id, shas) -> str:
        name = package.manifest["name"]
        if label is not None:
            taken = conn.execute(
                select(t.attachment_versions.c.id).where(
                    t.attachment_versions.c.name == name,
                    t.attachment_versions.c.label == label,
                    _not_purged(),
                )
            ).first()
            if taken is not None:
                raise MaestroError(
                    "ATTACHMENT_LABEL_TAKEN",
                    f"{name} already has a snapshot labelled {label}; labels never move",
                    409,
                    field_errors=[FieldError("/label", "TAKEN")],
                    suggested_actions=[{"type": "choose_label", "attachment_id": taken.id}],
                )
        if parent_id is not None:
            parent = conn.execute(
                select(t.attachment_versions.c.id).where(
                    t.attachment_versions.c.id == parent_id, _not_purged()
                )
            ).first()
            if parent is None:
                raise MaestroError(
                    "VALIDATION_FAILED",
                    "The parent snapshot does not exist",
                    422,
                    field_errors=[FieldError("/parent_id", "NOT_FOUND")],
                )
        attachment_id = new_id("att")
        # The file list goes in under the write lock, so a purge of the same content
        # cannot remove it between here and the insert.
        self.store.record(package)
        conn.execute(
            insert(t.attachment_versions).values(
                id=attachment_id,
                owner_id=principal.user_id,
                name=name,
                label=label,
                parent_id=parent_id,
                description=package.manifest["description"],
                content_digest=package.content_digest,
                manifest_json=package.manifest,
                files_json=package.files,
                file_count=len(package.files),
                size_bytes=package.size_bytes,
                has_bootstrap=package.has_bootstrap,
                enabled=True,
                created_at=clock.to_text(clock.now()),
            )
        )
        conn.execute(
            insert(t.attachment_files),
            [{"attachment_version_id": attachment_id, "sha256": sha} for sha in shas],
        )
        for chunk in _chunks(shas):
            conn.execute(
                delete(t.attachment_uploads).where(
                    t.attachment_uploads.c.user_id == principal.user_id,
                    t.attachment_uploads.c.sha256.in_(chunk),
                )
            )
        audit.record(
            conn,
            principal,
            "attachment.publish",
            object_type="attachment_version",
            object_id=attachment_id,
            detail={
                "name": name,
                "label": label,
                "parent_id": parent_id,
                "content_digest": package.content_digest,
            },
        )
        return attachment_id

    def set_enabled(self, principal: Principal, attachment_id: str, enabled: bool) -> dict:
        with self.db.write() as conn:
            row = self._row(conn, attachment_id)
            require_manage_owned(principal, row["owner_id"])
            if deleted.state(conn, "attachment_version", attachment_id):
                raise MaestroError("ATTACHMENT_DELETED", "Attachment was deleted", 409)
            if row["enabled"] != enabled:
                conn.execute(
                    update(t.attachment_versions)
                    .where(t.attachment_versions.c.id == attachment_id)
                    .values(enabled=enabled)
                )
                audit.record(
                    conn,
                    principal,
                    f"attachment.{'enable' if enabled else 'disable'}",
                    object_type="attachment_version",
                    object_id=attachment_id,
                )
        return self.get(principal, attachment_id)

    # --- reading -------------------------------------------------------------------------

    def file(self, principal: Principal, attachment_id: str, path: str) -> tuple[Path, dict]:
        """One file's stored content and its entry."""
        with self.db.read() as conn:
            row = self._readable(conn, attachment_id)
        entry = next((f for f in row["files_json"] if f["path"] == path), None)
        if entry is None:
            raise not_found("File")
        blob = self.store.blob(entry["sha256"])
        if blob is None:
            raise MaestroError("ATTACHMENT_CONTENT_MISSING", "The stored content is gone", 410)
        return blob, entry

    def archive(self, principal: Principal, attachment_id: str) -> tuple[Path, str]:
        """The snapshot as a zip file (paths and executable bits kept) that the caller
        sends and deletes, and its file name. Written whole before anything is sent, so
        a content missing fails the request instead of truncating the archive."""
        with self.db.read() as conn:
            row = self._readable(conn, attachment_id)
        folder = f"{row['name']}-{display(row['label'], row['content_digest'])}"
        self.store.root.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(prefix=".archive-", suffix=".zip", dir=self.store.root)
        os.close(handle)
        target = Path(name)
        try:
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
                for entry in row["files_json"]:
                    blob = self.store.blob(entry["sha256"])
                    if blob is None:
                        raise MaestroError(
                            "ATTACHMENT_CONTENT_MISSING", "The stored content is gone", 410
                        )
                    info = zipfile.ZipInfo(f"{folder}/{entry['path']}")
                    info.compress_type = zipfile.ZIP_DEFLATED
                    mode = 0o755 if entry["executable"] else 0o644
                    info.external_attr = (0o100000 | mode) << 16
                    with blob.open("rb") as source, archive.open(info, "w") as sink:
                        shutil.copyfileobj(source, sink, 1 << 20)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return target, f"{folder}.zip"

    def diff(self, principal: Principal, attachment_id: str, base_id: str | None) -> dict:
        """Files added, removed and changed from ``base_id`` (default: the parent) to
        this snapshot."""
        with self.db.read() as conn:
            row = self._readable(conn, attachment_id)
            base_id = base_id or row["parent_id"]
            base = self._readable(conn, base_id) if base_id else None
        before = {f["path"]: f for f in base["files_json"]} if base else {}
        after = {f["path"]: f for f in row["files_json"]}
        return {
            "attachment_id": attachment_id,
            "base_id": base_id,
            "added": [after[p] for p in sorted(after.keys() - before.keys())],
            "removed": [before[p] for p in sorted(before.keys() - after.keys())],
            "changed": [
                {"path": p, "before": before[p], "after": after[p]}
                for p in sorted(after.keys() & before.keys())
                if (before[p]["sha256"], before[p]["executable"])
                != (after[p]["sha256"], after[p]["executable"])
            ],
        }
