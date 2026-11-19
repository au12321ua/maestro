"""Artifact index, bounded reads, retention and index/file reconciliation.

Raw artifacts expire a fixed TTL after their trial's execution ended (run-level
artifacts: after the run finished). The index row, digest and status stay as
business metadata after the file is reclaimed. Team retention reserves space for
selected files; read pins protect in-progress exports for a bounded time.
"""

import hashlib
import logging
import os
import time
from typing import BinaryIO

from sqlalchemy import Connection, delete, func, insert, or_, select, update

from maestro.boundaries.storage import ArtifactStore
from maestro.core import clock
from maestro.core.errors import MaestroError, not_found
from maestro.db import schema as t
from maestro.db.engine import Database
from maestro.domain.states import RUN_TERMINAL, ArtifactState
from maestro.services import audit, deleted, policy, task_reads
from maestro.services.principal import Principal, require_experiment

log = logging.getLogger(__name__)
SWEEP_BATCH = 500
READ_MAX = 1024 * 1024
MISSING_REASON = "STORAGE_CONTENT_MISSING"
# Start-up checks: files up to this size are re-hashed before they are adopted again;
# half of the indexed files missing at once means the volume, not individual files.
READOPT_DIGEST_MAX = 8 * 1024 * 1024
MISSING_ALARM_SHARE = 0.5
# A file is written before its row is inserted, so it is not 'unindexed' until it is old.
UNINDEXED_GRACE_SECONDS = 3600


def public_artifact(row) -> dict:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "trial_id": row["trial_id"],
        "attempt_id": row["attempt_id"],
        "kind": row["kind"],
        "relative_path": row["relative_path"],
        "digest": row["digest"],
        "size_bytes": row["size_bytes"],
        "status": row["status"],
        "status_reason": row["status_reason"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
    }


def expiry_after(conn: Connection, ended_at: str | None) -> str | None:
    """TTL of the team policy in force when the file's owner ended."""
    if ended_at is None:
        return None
    days = policy.current(conn).artifact_ttl_days
    return clock.to_text(clock.from_text(ended_at) + clock.timedelta(days=days))


def expiry_for(conn: Connection, run_id: str, trial_id: str | None) -> str | None:
    """For a file stored now: a late upload does not restart the TTL."""
    if trial_id:
        ended = conn.execute(
            select(t.trials.c.execution_terminal_at).where(t.trials.c.id == trial_id)
        ).scalar()
    else:
        ended = conn.execute(select(t.runs.c.finished_at).where(t.runs.c.id == run_id)).scalar()
    return expiry_after(conn, ended)


def stamp_expiry(conn: Connection, run_id: str, trial_id: str | None = None) -> None:
    """Set expires_at on artifacts whose owner has ended and that still have none."""
    trials = select(t.trials.c.id, t.trials.c.execution_terminal_at).where(
        t.trials.c.run_id == run_id, t.trials.c.execution_terminal_at.is_not(None)
    )
    if trial_id:
        trials = trials.where(t.trials.c.id == trial_id)
    for owner, ended in conn.execute(trials).all():
        conn.execute(
            update(t.artifacts)
            .where(t.artifacts.c.trial_id == owner, t.artifacts.c.expires_at.is_(None))
            .values(expires_at=expiry_after(conn, ended))
        )
    if trial_id is None:
        finished = conn.execute(select(t.runs.c.finished_at).where(t.runs.c.id == run_id)).scalar()
        if finished:
            conn.execute(
                update(t.artifacts)
                .where(
                    t.artifacts.c.run_id == run_id,
                    t.artifacts.c.trial_id.is_(None),
                    t.artifacts.c.expires_at.is_(None),
                )
                .values(expires_at=expiry_after(conn, finished))
            )


def _all_reclaimed(statuses: set[str]) -> bool:
    return "expired" in statuses and not statuses & {"available", "pending"}


def refresh_artifact_states(conn: Connection, trial_ids: set[str], run_ids: set[str]) -> None:
    """A trial (or run) whose saved files have all been reclaimed reads 'expired'."""
    for trial_id in trial_ids:
        statuses = set(
            conn.execute(
                select(t.artifacts.c.status).where(t.artifacts.c.trial_id == trial_id)
            ).scalars()
        )
        if _all_reclaimed(statuses):
            conn.execute(
                update(t.trials)
                .where(t.trials.c.id == trial_id)
                .values(artifact_state=ArtifactState.EXPIRED)
            )
    for run_id in run_ids:
        statuses = set(
            conn.execute(select(t.artifacts.c.status).where(t.artifacts.c.run_id == run_id))
            .scalars()
            .all()
        )
        if _all_reclaimed(statuses):
            conn.execute(
                update(t.runs)
                .where(t.runs.c.id == run_id)
                .values(artifact_state=ArtifactState.EXPIRED)
            )


def check_readable(row) -> None:
    status = row["status"]
    if status in {"expired", "deleting"}:
        raise MaestroError("ARTIFACT_EXPIRED", "Artifact expired; its index and digest remain", 410)
    if status == "deleted":
        raise MaestroError("ARTIFACT_DELETED", "Artifact was deleted", 410)
    if status == "pending":
        raise MaestroError(
            "ARTIFACT_NOT_READY", "Artifact is still being saved", 409, retryable=True
        )
    if status != "available":
        raise MaestroError(
            "ARTIFACT_UNAVAILABLE",
            f"Artifact content is not available ({row['status_reason'] or status})",
            409,
        )


def release_removed_files(conn: Connection, keys: list[str]) -> None:
    """Release reservations only for keys whose unlink succeeded (or were already absent)."""
    for start in range(0, len(keys), SWEEP_BATCH):
        conn.execute(
            delete(t.artifact_retentions).where(
                t.artifact_retentions.c.storage_key.in_(keys[start : start + SWEEP_BATCH])
            )
        )


def _purged_retention_keys(conn: Connection) -> set[str]:
    """Retry a purge interrupted after committing index changes, even after an unlink."""
    return set(
        conn.scalars(
            select(t.artifact_retentions.c.storage_key)
            .outerjoin(
                t.artifacts, t.artifacts.c.storage_key == t.artifact_retentions.c.storage_key
            )
            .where(or_(t.artifacts.c.id.is_(None), t.artifacts.c.status == "deleted"))
        )
    )


class Artifacts:
    def __init__(self, db: Database, store: ArtifactStore):
        self.db = db
        self.store = store

    # --- queries -----------------------------------------------------------------

    def for_run(self, run_id: str, trial_id: str | None = None) -> list[dict]:
        query = select(t.artifacts).where(
            t.artifacts.c.run_id == run_id, deleted.visible(t.artifacts.c.id, "artifact")
        )
        if trial_id:
            query = query.where(t.artifacts.c.trial_id == trial_id)
        with self.db.read() as conn:
            if not conn.execute(select(t.runs.c.id).where(t.runs.c.id == run_id)).first():
                raise not_found("Run")
            deleted.refuse_purged(conn, "run", run_id, "Run")
            rows = conn.execute(query.order_by(t.artifacts.c.created_at)).mappings().all()
        return [public_artifact(r) for r in rows]

    def open(
        self,
        artifact_id: str,
        offset: int = 0,
        length: int | None = None,
        *,
        principal: Principal | None = None,
    ) -> tuple[dict, BinaryIO, int, int]:
        """Metadata, a reader positioned at ``offset``, the byte count to send and the
        total size. The reader stays valid if retention unlinks the file meanwhile."""
        if offset < 0 or (length is not None and not 1 <= length <= READ_MAX):
            raise MaestroError("INVALID_RANGE", f"offset >= 0 and length 1-{READ_MAX} bytes", 400)
        with self.db.read() as conn:
            row = self._row(conn, artifact_id)
            # A run in the recycle bin still resolves, files included; a purged run
            # has no files left and its rows are gone.
            gone = deleted.state(conn, "artifact", artifact_id)
        if gone:
            raise MaestroError("ARTIFACT_DELETED", "Artifact was deleted", 410)
        check_readable(row)
        try:
            reader = self.store.open(row["storage_key"])
        except (FileNotFoundError, KeyError):
            # Retention or a purge won the race between the index read and the open.
            with self.db.read() as conn:
                check_readable(self._row(conn, artifact_id))
            raise MaestroError(
                "ARTIFACT_UNAVAILABLE", "Artifact content is not available", 409
            ) from None
        total = row["size_bytes"] or 0
        offset = min(offset, total)
        reader.seek(offset)
        count = total - offset if length is None else min(length, total - offset)
        try:
            if principal is not None and row["trial_id"]:
                with self.db.read() as conn:
                    task_id = conn.execute(
                        select(t.trials.c.task_id).where(t.trials.c.id == row["trial_id"])
                    ).scalar_one()
                task_reads.record(self.db, principal, [task_id], "trial_artifacts")
        except BaseException:
            reader.close()
            raise
        return public_artifact(row), reader, count, total

    @staticmethod
    def _row(conn: Connection, artifact_id: str):
        row = (
            conn.execute(select(t.artifacts).where(t.artifacts.c.id == artifact_id))
            .mappings()
            .first()
        )
        if row is None:
            raise not_found("Artifact")
        return row

    def read(self, artifact_id: str) -> tuple[dict, bytes]:
        meta, reader, count, _ = self.open(artifact_id)
        with reader:
            return meta, reader.read(count)

    # --- retention ---------------------------------------------------------------

    @staticmethod
    def _retention_rows(conn: Connection, run_id: str, *, retaining: bool = False) -> list:
        state = conn.scalar(select(t.runs.c.state).where(t.runs.c.id == run_id))
        if state is None:
            raise not_found("Run")
        deletion = deleted.state(conn, "run", run_id)
        if deletion and deletion["state"] == deleted.PURGED:
            raise deleted.purged_error("Run")
        if retaining and deletion:
            raise MaestroError("RUN_RECYCLED", "Restore the run before retaining artifacts", 409)
        if state not in RUN_TERMINAL:
            raise MaestroError(
                "RUN_ACTIVE", "Wait for the run to end before retaining artifacts", 409
            )

        return list(
            conn.execute(
                select(
                    t.artifacts,
                    t.artifact_retentions.c.storage_key.is_not(None).label("retained"),
                    (~deleted.visible(t.artifacts.c.id, "artifact")).label("in_bin"),
                )
                .outerjoin(
                    t.artifact_retentions,
                    t.artifact_retentions.c.storage_key == t.artifacts.c.storage_key,
                )
                .where(t.artifacts.c.run_id == run_id)
                .order_by(t.artifacts.c.created_at, t.artifacts.c.id)
            ).mappings()
        )

    def _check_retention_files(self, rows: list) -> dict:
        # Only file probes happen here. Recheck index state under the write lock before
        # retaining so a file already claimed for expiry or purge cannot be protected.
        return {
            row["storage_key"]: (
                row["digest"],
                row["size_bytes"],
                self.store.exists(row["storage_key"]),
            )
            for row in rows
            if row["status"] == "available" and not row["in_bin"]
        }

    @staticmethod
    def _retention_view(conn: Connection, run_id: str, rows: list, checked: dict) -> dict:
        used, run_bytes = conn.execute(
            select(
                func.coalesce(func.sum(t.artifact_retentions.c.size_bytes), 0),
                func.coalesce(
                    func.sum(t.artifact_retentions.c.size_bytes).filter(
                        t.artifact_retentions.c.run_id == run_id
                    ),
                    0,
                ),
            )
        ).one()
        at = clock.to_text(clock.now())
        files = []
        for row in rows:
            status, reason = row["status"], row["status_reason"]
            if row["in_bin"]:
                status, reason = "deleted", "DELETED"
            elif status == "available" and not checked[row["storage_key"]][2]:
                status, reason = "unavailable", MISSING_REASON
            files.append(
                {
                    "artifact_id": row["id"],
                    "relative_path": row["relative_path"],
                    "trial_id": row["trial_id"],
                    "attempt_id": row["attempt_id"],
                    "status": status,
                    "status_reason": reason,
                    "size_bytes": row["size_bytes"],
                    "retained": row["retained"] and row["status"] != "deleted",
                    "expires_at": row["expires_at"],
                    "past_expiry": bool(row["expires_at"] and row["expires_at"] <= at),
                }
            )
        available = [file for file in files if file["status"] == "available"]
        return {
            "available_bytes": sum(file["size_bytes"] or 0 for file in available),
            "additional_bytes": sum(
                file["size_bytes"] or 0 for file in available if not file["retained"]
            ),
            "retained_bytes": run_bytes,
            "used_bytes": used,
            "quota_bytes": policy.current(conn).artifact_retention_quota_bytes,
            "files": files,
        }

    def retention(self, run_id: str) -> dict:
        with self.db.read() as conn:
            rows = self._retention_rows(conn, run_id)
            return self._retention_view(conn, run_id, rows, self._check_retention_files(rows))

    def retain(self, principal: Principal, run_id: str) -> dict:
        require_experiment(principal)
        for _ in range(3):
            with self.db.read() as conn:
                rows = self._retention_rows(conn, run_id, retaining=True)
            checked = self._check_retention_files(rows)
            with self.db.write() as conn:
                rows = self._retention_rows(conn, run_id, retaining=True)
                candidates = [
                    row for row in rows if row["status"] == "available" and not row["in_bin"]
                ]
                if any(
                    checked.get(row["storage_key"], ())[:2] != (row["digest"], row["size_bytes"])
                    for row in candidates
                ):
                    continue
                view = self._retention_view(conn, run_id, rows, checked)
                new = [
                    {
                        "storage_key": row["storage_key"],
                        "run_id": run_id,
                        "size_bytes": row["size_bytes"],
                    }
                    for row in candidates
                    if not row["retained"] and checked[row["storage_key"]][2]
                ]
                if not new:
                    return view
                needed = view["additional_bytes"]
                shortfall = view["used_bytes"] + needed - view["quota_bytes"]
                if needed and shortfall > 0:
                    raise MaestroError(
                        "ARTIFACT_RETENTION_QUOTA",
                        f"Free {shortfall} more bytes of retention space to retain this run's artifacts",
                        409,
                        suggested_actions=[
                            {"type": "release_artifact_retention", "shortfall_bytes": shortfall}
                        ],
                    )
                conn.execute(insert(t.artifact_retentions), new)
                audit.record(
                    conn,
                    principal,
                    "run.artifact_retention.retain",
                    object_type="run",
                    object_id=run_id,
                    detail={"files": len(new), "bytes": needed},
                )
                return self._retention_view(
                    conn, run_id, self._retention_rows(conn, run_id), checked
                )
        raise MaestroError(
            "ARTIFACT_RETENTION_CHANGED",
            "Artifacts changed while checking storage; try again",
            409,
            retryable=True,
        )

    def release(self, principal: Principal, run_id: str) -> dict:
        require_experiment(principal)
        with self.db.write() as conn:
            self._retention_rows(conn, run_id)
            # Purged keys still pending unlink must stay charged until cleanup succeeds.
            removed = (
                conn.execute(
                    delete(t.artifact_retentions)
                    .where(
                        t.artifact_retentions.c.run_id == run_id,
                        t.artifact_retentions.c.storage_key.in_(
                            select(t.artifacts.c.storage_key).where(
                                t.artifacts.c.run_id == run_id, t.artifacts.c.status != "deleted"
                            )
                        ),
                    )
                    .returning(t.artifact_retentions.c.size_bytes)
                )
                .scalars()
                .all()
            )
            if removed:
                audit.record(
                    conn,
                    principal,
                    "run.artifact_retention.release",
                    object_type="run",
                    object_id=run_id,
                    detail={"files": len(removed), "bytes": sum(removed)},
                )
        return self.retention(run_id)

    def sweep(self) -> dict:
        """Reclaim expired, unpinned files: mark deleting, delete, then mark expired.

        A crash between the steps leaves 'deleting', which is never downloadable and
        is finished by the next sweep.
        """
        at = clock.to_text(clock.now())
        with self.db.write() as conn:
            conn.execute(delete(t.artifact_pins).where(t.artifact_pins.c.expires_at <= at))
            rows = conn.execute(
                select(t.artifacts.c.id, t.artifacts.c.storage_key)
                .where(
                    t.artifacts.c.status.in_(["available", "deleting"]),
                    t.artifacts.c.expires_at <= at,
                    t.artifacts.c.id.not_in(select(t.artifact_pins.c.artifact_id)),
                    t.artifacts.c.storage_key.not_in(select(t.artifact_retentions.c.storage_key)),
                )
                .limit(SWEEP_BATCH)
            ).all()
            if rows:
                conn.execute(
                    update(t.artifacts)
                    .where(t.artifacts.c.id.in_([r.id for r in rows]))
                    .values(status="deleting")
                )
        reclaimed, failed = [], 0
        for row in rows:
            try:
                self.store.delete(row.storage_key)
                reclaimed.append(row.id)
            except OSError:
                failed += 1
                log.warning("artifact %s could not be reclaimed; will retry", row.id)
        if reclaimed:
            with self.db.write() as conn:
                conn.execute(
                    update(t.artifacts)
                    .where(t.artifacts.c.id.in_(reclaimed), t.artifacts.c.status == "deleting")
                    .values(status="expired", status_reason="RETENTION_EXPIRED")
                )
                owners = conn.execute(
                    select(t.artifacts.c.run_id, t.artifacts.c.trial_id).where(
                        t.artifacts.c.id.in_(reclaimed)
                    )
                ).all()
                refresh_artifact_states(
                    conn, {o.trial_id for o in owners if o.trial_id}, {o.run_id for o in owners}
                )
        return {"reclaimed": len(reclaimed), "failed": failed}

    def sweep_unindexed(self, grace_seconds: float = UNINDEXED_GRACE_SECONDS) -> dict:
        """Remove stored files that no live index row stands for: what a purge could not
        delete, or a write whose row was never inserted.

        ``RunSink`` writes a file before it inserts the row, so only files last written
        more than ``grace_seconds`` ago are judged; a fresh file is never touched.
        """
        cutoff = time.time() - grace_seconds
        old = [
            key
            for key in list(self.store.keys())
            if (written := self.store.modified_at(key)) is not None and written < cutoff
        ]
        status: dict[str, str] = {}
        with self.db.read() as conn:
            pending = _purged_retention_keys(conn)
            for start in range(0, len(old), SWEEP_BATCH):
                status.update(
                    conn.execute(
                        select(t.artifacts.c.storage_key, t.artifacts.c.status).where(
                            t.artifacts.c.storage_key.in_(old[start : start + SWEEP_BATCH])
                        )
                    ).all()
                )
        # Rows that are not available any more, and 'deleting' ones that retention is
        # still working on, do not need the file; everything else does.
        stray = pending | {key for key in old if status.get(key) in {None, "deleted", "expired"}}
        removed, failed = self._remove_stray(stray, pending)
        if removed:
            log.warning("removed %d stored files that no artifact row needs", removed)
        return {"removed": removed, "failed": failed}

    def _remove_stray(self, keys: set[str], pending: set[str]) -> tuple[int, int]:
        removed = []
        failed = 0
        for key in keys:
            try:
                self.store.delete(key)
                removed.append(key)
            except OSError:
                failed += 1
                log.warning("stray file %s could not be removed; will retry", key)
        released = [key for key in removed if key in pending]
        if released:
            with self.db.write() as conn:
                release_removed_files(conn, released)
        return len(removed), failed

    def reconcile(self) -> dict:
        """At start, before any run executes: an indexed file that is missing
        becomes unavailable; a published file nobody indexes is an orphan and goes.

        The mark is not permanent: a start while the artifact volume was absent marks
        everything missing, so a file that is back, with the indexed size (and digest,
        when small), is available again."""
        partial = self.store.discard_partial()
        with self.db.read() as conn:
            pending = _purged_retention_keys(conn)
            indexed = {
                r.storage_key: r
                for r in conn.execute(
                    select(
                        t.artifacts.c.id,
                        t.artifacts.c.storage_key,
                        t.artifacts.c.status,
                        t.artifacts.c.status_reason,
                        t.artifacts.c.digest,
                        t.artifacts.c.size_bytes,
                    )
                ).all()
            }
        available = [key for key, row in indexed.items() if row.status == "available"]
        missing = [key for key in available if not self.store.exists(key)]
        readopted = [
            row.id
            for key, row in indexed.items()
            if row.status == "unavailable"
            and row.status_reason == MISSING_REASON
            and self._intact(key, row)
        ]
        stored_keys = self.store.keys()
        orphans = pending | {
            key
            for key in stored_keys
            if key not in indexed or indexed[key].status in {"expired", "deleted", "deleting"}
        }
        self._remove_stray(orphans, pending)
        if missing or readopted:
            with self.db.write() as conn:
                conn.execute(
                    update(t.artifacts)
                    .where(
                        t.artifacts.c.id.in_([indexed[key].id for key in missing]),
                        t.artifacts.c.status == "available",
                    )
                    .values(status="unavailable", status_reason=MISSING_REASON)
                )
                conn.execute(
                    update(t.artifacts)
                    .where(
                        t.artifacts.c.id.in_(readopted),
                        t.artifacts.c.status == "unavailable",
                        t.artifacts.c.status_reason == MISSING_REASON,
                    )
                    .values(status="available", status_reason=None)
                )
        report = {
            "missing": len(missing),
            "orphans": len(orphans),
            "partial": partial,
            "readopted": len(readopted),
        }
        if missing or orphans or partial or readopted:
            log.warning("artifact storage reconciliation: %s", report)
        if missing and len(missing) >= MISSING_ALARM_SHARE * len(available):
            log.error(
                "%d of %d indexed artifact files are missing: is the artifact volume mounted? "
                "They are marked unavailable and are adopted again at the next start once "
                "the files are back",
                len(missing),
                len(available),
            )
        return report

    def _intact(self, key: str, row) -> bool:
        """The file at ``key`` is the one the index describes."""
        try:
            with self.store.open(key) as reader:
                if reader.seek(0, os.SEEK_END) != row.size_bytes:
                    return False
                if row.size_bytes > READOPT_DIGEST_MAX:
                    return True  # too large to hash at start; the size has to do
                reader.seek(0)
                return row.digest == "sha256:" + hashlib.sha256(reader.read()).hexdigest()
        except (FileNotFoundError, KeyError):
            return False
