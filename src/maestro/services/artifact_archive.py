"""A run's artifacts, or one task's, trial's or attempt's, as a zip streamed on request
(07 section 5).

Only for runs that have ended, so the selection does not grow while it is sent. Nothing
is written to disk first and no read pin is taken: a file reclaimed or deleted while
the archive streams is left out and listed in the manifest, like an expired one.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import zipfile
from collections.abc import Iterator

from sqlalchemy import select

from maestro import __version__
from maestro.boundaries.storage import ArtifactStore
from maestro.core import clock
from maestro.core.errors import MaestroError, not_found
from maestro.db import schema as t
from maestro.db.engine import Database
from maestro.domain.states import RUN_TERMINAL
from maestro.services import deleted
from maestro.services.exports import safe_name

SCHEMA_VERSION = 1
CHUNK = 1 << 20
# Why a selected file is not in the archive, as in exports' missing list.
ABSENT = {
    "expired": "EXPIRED",
    "deleting": "EXPIRED",
    "deleted": "DELETED",
    "failed": "SAVE_FAILED",
}


class _Pipe:
    """A write-only, unseekable stream: zipfile then writes data descriptors, and
    whatever it wrote since the last ``take`` is the next chunk of the response."""

    def __init__(self):
        self.buffer = bytearray()

    def write(self, data) -> int:
        self.buffer += data
        return len(data)

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        data = bytes(self.buffer)
        self.buffer.clear()
        return data


def _unique(paths: dict[str, str]) -> dict[str, str]:
    """Distinct keys that sanitize alike get a stable suffix."""
    if len(set(paths.values())) == len(paths):
        return paths
    return {
        key: f"{value}-{hashlib.sha256(key.encode()).hexdigest()[:8]}"
        for key, value in paths.items()
    }


def _relative(path: str) -> str:
    """An Agent-chosen path made safe segment by segment; it cannot leave its folder."""
    parts = [safe_name(p, "") for p in path.replace("\\", "/").split("/")]
    return "/".join(p for p in parts if p) or "file"


class ArtifactArchives:
    def __init__(self, db: Database, store: ArtifactStore, max_bytes: int):
        self.db = db
        self.store = store
        self.max_bytes = max_bytes

    def open(
        self,
        run_id: str,
        task_key: str | None = None,
        trial_id: str | None = None,
        attempt_id: str | None = None,
    ) -> tuple[str, Iterator[bytes]]:
        """The archive's file name and its content; errors are raised before the first
        byte, from the scope check and the size limit."""
        with self.db.read() as conn:
            run = conn.execute(select(t.runs).where(t.runs.c.id == run_id)).mappings().first()
            if run is None:
                raise not_found("Run")
            deleted.refuse_purged(conn, "run", run_id, "Run")
            if run["state"] not in RUN_TERMINAL:
                raise MaestroError("RUN_ACTIVE", "Artifacts are packed once the run has ended", 409)
            trials = {
                r["id"]: r
                for r in conn.execute(
                    select(t.trials.c.id, t.tasks.c.task_key, t.trials.c.repetition_index)
                    .join(t.tasks, t.tasks.c.id == t.trials.c.task_id)
                    .where(t.trials.c.run_id == run_id)
                ).mappings()
            }
            attempts = {
                r["id"]: r
                for r in conn.execute(
                    select(
                        t.execution_attempts.c.id,
                        t.execution_attempts.c.trial_id,
                        t.execution_attempts.c.attempt_index,
                    ).where(t.execution_attempts.c.trial_id.in_(trials.keys()))
                ).mappings()
            }
            query = select(t.artifacts).where(
                t.artifacts.c.run_id == run_id, deleted.visible(t.artifacts.c.id, "artifact")
            )
            scope: dict = {"run_id": run_id}
            if attempt_id:
                if attempt_id not in attempts:
                    raise not_found("Execution attempt")
                query = query.where(t.artifacts.c.attempt_id == attempt_id)
                scope["attempt_id"] = attempt_id
            elif trial_id:
                if trial_id not in trials:
                    raise not_found("Trial")
                query = query.where(t.artifacts.c.trial_id == trial_id)
                scope["trial_id"] = trial_id
            elif task_key:
                chosen = [k for k, v in trials.items() if v["task_key"] == task_key]
                if not chosen:
                    raise not_found("Task")
                query = query.where(t.artifacts.c.trial_id.in_(chosen))
                scope["task_key"] = task_key
            selected = [
                dict(r)
                for r in conn.execute(
                    query.order_by(t.artifacts.c.trial_id, t.artifacts.c.relative_path)
                ).mappings()
            ]
        size = sum(a["size_bytes"] or 0 for a in selected if a["status"] == "available")
        if size > self.max_bytes:
            raise MaestroError(
                "ARCHIVE_TOO_LARGE",
                f"{size} bytes exceed the {self.max_bytes}-byte limit; download a narrower "
                "level or create an export",
                413,
            )
        root = f"{safe_name(run['name'] or '', 'run')}-{run_id[-8:]}"
        task_dirs = _unique(
            {v["task_key"]: safe_name(v["task_key"], "task") for v in trials.values()}
        )
        trial = None
        if attempt_id:
            trial = trials[attempts[attempt_id]["trial_id"]]
        elif trial_id:
            trial = trials[trial_id]
        name = root
        if trial or task_key:
            name += f"-{task_dirs[trial['task_key'] if trial else task_key]}"
        if trial:
            name += f"-trial-{trial['repetition_index']}"
        if attempt_id:
            name += f"-attempt-{attempts[attempt_id]['attempt_index']}"

        def folder(item: dict) -> str:
            trial = trials.get(item["trial_id"])
            if trial is None:
                return f"{root}/run"
            path = f"{root}/tasks/{task_dirs[trial['task_key']]}/trial-{trial['repetition_index']}"
            attempt = attempts.get(item["attempt_id"])
            return f"{path}/attempt-{attempt['attempt_index']}" if attempt else path

        return f"{name}.zip", self._stream(root, scope, selected, folder)

    def _stream(self, root, scope, selected, folder) -> Iterator[bytes]:
        pipe = _Pipe()
        files, missing, warnings, used = [], [], [], set()
        with zipfile.ZipFile(pipe, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for item in selected:
                entry = {
                    "artifact_id": item["id"],
                    "trial_id": item["trial_id"],
                    "attempt_id": item["attempt_id"],
                    "kind": item["kind"],
                    "relative_path": item["relative_path"],
                    "digest": item["digest"],
                    "size_bytes": item["size_bytes"],
                }
                if item["status"] != "available":
                    reason = ABSENT.get(item["status"], item["status_reason"] or item["status"])
                    missing.append({**entry, "reason": reason})
                    continue
                path = f"{folder(item)}/{_relative(item['relative_path'])}"
                if path in used:
                    stem, ext = posixpath.splitext(path)
                    path = f"{stem}-{item['id'][-8:]}{ext}"
                used.add(path)
                try:
                    source = self.store.open(item["storage_key"])
                except (FileNotFoundError, KeyError):
                    # Reclaimed or deleted since the selection was read.
                    missing.append({**entry, "reason": "STORAGE_CONTENT_MISSING"})
                    continue
                digest = hashlib.sha256()
                with source, archive.open(path, "w", force_zip64=True) as sink:
                    while chunk := source.read(CHUNK):
                        digest.update(chunk)
                        sink.write(chunk)
                        if len(pipe.buffer) >= CHUNK:
                            yield pipe.take()
                actual = "sha256:" + digest.hexdigest()
                files.append({**entry, "path": path[len(root) + 1 :], "digest": actual})
                if item["digest"] and actual != item["digest"]:
                    warnings.append(
                        {
                            "code": "DIGEST_MISMATCH",
                            "artifact_id": item["id"],
                            "indexed": item["digest"],
                        }
                    )
                yield pipe.take()
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "generated_at": clock.to_text(clock.now()),
                "service_version": __version__,
                "scope": scope,
                "files": files,
                "missing": missing,
                "warnings": warnings,
            }
            archive.writestr(
                f"{root}/manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode(),
            )
        yield pipe.take()
