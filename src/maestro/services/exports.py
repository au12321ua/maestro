"""Batch export of runs as one ZIP with a manifest.

Accepting an export creates an Operation; the maintenance loop builds it. Files
included are read-pinned for at most ``EXPORT_PIN_MINUTES`` so retention cannot
take them mid-build; a build that outlives its pins fails instead of holding them.
Items that are expired, deleted or failed to save are listed under ``missing``
with their reason, never silently left out. Packages expire after 24 hours.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import zipfile
from pathlib import Path

from sqlalchemy import delete, func, insert, select, update

from maestro import __version__
from maestro.boundaries.storage import ArtifactStore
from maestro.core import clock
from maestro.core.errors import FieldError, MaestroError, not_found
from maestro.core.ids import new_id
from maestro.db import schema as t
from maestro.db.engine import Database
from maestro.domain import limits
from maestro.domain.states import OperationStatus
from maestro.execution.policy import MAPPING_ID, POLICY_VERSION
from maestro.services import audit, deleted, operations, pagination, policy
from maestro.services.principal import Principal, require_admin_session

log = logging.getLogger(__name__)
SCHEMA_VERSION = 1
ARCHIVE = "export.zip"
# Default largest export (``export_max_gb`` in config.toml).
DEFAULT_MAX_BYTES = 10 * 1024**3
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


class ExportFailed(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def safe_name(value: str, fallback: str) -> str:
    """Archive names are generated, never task or artifact paths taken as is."""
    name = _SAFE.sub("_", value).strip("._")[:120]
    return name or fallback


def public_export(row) -> dict:
    ready = row["state"] == "ready"
    return {
        "id": row["id"],
        "owner_id": row["owner_id"],
        "operation_id": row["operation_id"],
        "state": row["state"],
        "request": row["request_json"],
        "size_bytes": row["size_bytes"],
        "digest": row["digest"],
        "summary": row["summary_json"],
        "error": row["error_json"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
        "expires_at": row["expires_at"],
        "links": {
            "self": f"/api/v1/exports/{row['id']}",
            "operation": f"/api/v1/operations/{row['operation_id']}",
            **({"download": f"/api/v1/exports/{row['id']}/download"} if ready else {}),
        },
    }


def _check_request(body: dict, team: policy.Policy) -> dict:
    errors = []
    run_ids = body.get("run_ids")
    if (
        not isinstance(run_ids, list)
        or not 1 <= len(run_ids) <= team.export_max_runs
        or not all(isinstance(x, str) for x in run_ids)
        or len(set(run_ids)) != len(run_ids)
    ):
        errors.append(FieldError("/run_ids", "INVALID", limit=team.export_max_runs))
    include = body.get("include_artifacts", True)
    if not isinstance(include, bool):
        errors.append(FieldError("/include_artifacts", "TYPE"))
    lists = {}
    for name in ("artifact_kinds", "task_keys"):
        value = body.get(name)
        if value is not None and (
            not isinstance(value, list)
            or not 1 <= len(value) <= team.run_max_tasks
            or not all(isinstance(x, str) and 1 <= len(x) <= 256 for x in value)
        ):
            errors.append(FieldError(f"/{name}", "INVALID"))
        lists[name] = sorted(set(value)) if isinstance(value, list) else None
    if errors:
        raise MaestroError("EXPORT_INVALID", "Invalid export request", 422, field_errors=errors)
    return {"run_ids": run_ids, "include_artifacts": include, **lists}


class Exports:
    def __init__(
        self,
        db: Database,
        store: ArtifactStore,
        directory: Path,
        runs,
        disk=None,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ):
        self.db = db
        self.store = store
        self.directory = directory
        self.runs = runs
        self.disk = disk
        self.max_bytes = max_bytes

    # --- requests ----------------------------------------------------------------

    def create(self, principal: Principal, body: dict) -> dict:
        """Exporting reads team-shared results, so a read PAT may export too. A package
        can take up to ``max_bytes``, so creation is limited per user and, like
        new runs, paused while free disk space is low."""
        team = policy.Policies(self.db).current()
        request = _check_request(body, team)
        if self.disk:
            self.disk.refuse_if_low("exports")
        export_id = new_id("exp")
        with self.db.write() as conn:
            active = conn.execute(
                select(func.count()).where(
                    t.exports.c.owner_id == principal.user_id,
                    t.exports.c.state.in_(["queued", "building"]),
                )
            ).scalar()
            if active >= team.export_max_active_per_user:
                raise MaestroError(
                    "EXPORT_LIMIT_REACHED",
                    f"At most {team.export_max_active_per_user} of your exports can be "
                    "queued or building at once; wait for one to finish",
                    429,
                    retryable=True,
                    headers={"Retry-After": "30"},
                )
            found = set(
                conn.execute(
                    select(t.runs.c.id).where(t.runs.c.id.in_(request["run_ids"]))
                ).scalars()
            )
            purged = {
                run_id
                for run_id, mark in deleted.states(conn, "run", request["run_ids"]).items()
                if mark["state"] == deleted.PURGED
            }
            bad = [x for x in request["run_ids"] if x not in found or x in purged]
            if bad:
                raise MaestroError(
                    "EXPORT_RUNS_UNAVAILABLE",
                    "Some runs do not exist or were permanently deleted",
                    422,
                    field_errors=[FieldError("/run_ids", "NOT_FOUND", x) for x in bad],
                )
            op_id = operations.create(
                conn,
                "export",
                owner_id=principal.user_id,
                pat_id=principal.pat_id,
                resource_type="export",
                resource_id=export_id,
                progress={"runs": len(request["run_ids"]), "files": 0},
                phase="queued",
            )
            conn.execute(
                insert(t.exports).values(
                    id=export_id,
                    owner_id=principal.user_id,
                    pat_id=principal.pat_id,
                    operation_id=op_id,
                    request_json=request,
                    state="queued",
                    created_at=clock.to_text(clock.now()),
                )
            )
            audit.record(
                conn,
                principal,
                "export.create",
                object_type="export",
                object_id=export_id,
                detail={"runs": len(request["run_ids"])},
            )
            return public_export(self._row(conn, export_id))

    @staticmethod
    def _row(conn, export_id: str):
        row = conn.execute(select(t.exports).where(t.exports.c.id == export_id)).mappings().first()
        if row is None:
            raise not_found("Export")
        return row

    def _visible(self, principal: Principal, row) -> None:
        if row["owner_id"] != principal.user_id:
            try:
                require_admin_session(principal)
            except MaestroError:
                raise not_found("Export") from None

    def get(self, principal: Principal, export_id: str) -> dict:
        with self.db.read() as conn:
            row = self._row(conn, export_id)
        self._visible(principal, row)
        return public_export(row)

    def list(self, principal: Principal, cursor: str | None, limit: int | None):
        limit = pagination.check_limit(limit)
        filters = {"owner_id": principal.user_id}
        query = pagination.page(
            select(t.exports).where(t.exports.c.owner_id == principal.user_id),
            t.exports.c.created_at,
            t.exports.c.id,
            cursor,
            limit,
            filters,
        )
        with self.db.read() as conn:
            rows = list(conn.execute(query).mappings().all())
        rows, next_cursor = pagination.split(rows, limit, "created_at", filters)
        return [public_export(r) for r in rows], next_cursor

    def archive(self, principal: Principal, export_id: str) -> tuple[dict, Path]:
        with self.db.read() as conn:
            row = self._row(conn, export_id)
        self._visible(principal, row)
        if row["state"] == "expired":
            raise MaestroError("EXPORT_EXPIRED", "Export package expired; create a new one", 410)
        if row["state"] != "ready":
            raise MaestroError(
                "EXPORT_NOT_READY",
                f"Export is {row['state']}",
                409,
                retryable=row["state"] in {"queued", "building"},
            )
        path = self.directory / export_id / ARCHIVE
        if not path.is_file():
            raise MaestroError("EXPORT_UNAVAILABLE", "Export package is missing", 409)
        return public_export(row), path

    # --- building (maintenance loop) ---------------------------------------------

    def next_queued(self) -> str | None:
        with self.db.read() as conn:
            return conn.execute(
                select(t.exports.c.id)
                .where(t.exports.c.state == "queued")
                .order_by(t.exports.c.created_at, t.exports.c.id)
                .limit(1)
            ).scalar()

    def recover(self) -> int:
        """A build interrupted by a restart starts again from the records."""
        with self.db.write() as conn:
            building = conn.execute(
                select(t.exports.c.id).where(t.exports.c.state == "building")
            ).scalars()
            ids = list(building)
            for export_id in ids:
                conn.execute(
                    delete(t.artifact_pins).where(
                        t.artifact_pins.c.holder_kind == "export",
                        t.artifact_pins.c.holder_id == export_id,
                    )
                )
            if ids:
                conn.execute(
                    update(t.exports).where(t.exports.c.id.in_(ids)).values(state="queued")
                )
        for export_id in ids:
            shutil.rmtree(self.directory / export_id, ignore_errors=True)
        return len(ids)

    def build(self, export_id: str) -> None:
        """Build one queued export. Whatever goes wrong ends in 'failed' with a record the
        owner can read: a queued export that kept raising would be retried every poll
        and hold up everything behind it."""
        try:
            self._build(export_id)
        except Exception:
            log.exception("export %s could not be built", export_id)
            self.fail(
                export_id, "EXPORT_BUILD_FAILED", "The export could not be built; try again later"
            )

    def fail(self, export_id: str, code: str, message: str) -> None:
        """End an export that has not finished, whichever step it stopped at. The message
        is stored as given, so it must not carry exception text."""
        with self.db.read() as conn:
            state = self._row(conn, export_id)["state"]
        if state in {"queued", "building"}:
            shutil.rmtree(self.directory / export_id, ignore_errors=True)
            self._finish(export_id, None, {"code": code, "message": message})

    def _build(self, export_id: str) -> None:
        pin_until = clock.now() + clock.timedelta(minutes=limits.EXPORT_PIN_MINUTES)
        with self.db.write() as conn:
            row = self._row(conn, export_id)
            if row["state"] != "queued":
                return
            request = row["request_json"]
            selected = self._select_artifacts(conn, request)
            pinned = [a for a in selected if a["status"] == "available"]
            for item in pinned:
                conn.execute(
                    insert(t.artifact_pins).values(
                        id=new_id("pin"),
                        artifact_id=item["id"],
                        holder_kind="export",
                        holder_id=export_id,
                        expires_at=clock.to_text(pin_until),
                    )
                )
            conn.execute(
                update(t.exports).where(t.exports.c.id == export_id).values(state="building")
            )
            operations.advance(
                conn, row["operation_id"], status=OperationStatus.RUNNING, phase="building"
            )
        target = self.directory / export_id
        try:
            if sum(a["size_bytes"] or 0 for a in pinned) > self.max_bytes:
                raise ExportFailed(
                    "EXPORT_TOO_LARGE",
                    f"Selected files exceed {self.max_bytes} bytes; narrow the export",
                )
            target.mkdir(parents=True, exist_ok=True)
            summary = self._write(export_id, request, selected, target, pin_until)
        except (ExportFailed, MaestroError) as failure:
            # MaestroError: a run was purged after the export was accepted.
            shutil.rmtree(target, ignore_errors=True)
            self._finish(export_id, None, {"code": failure.code, "message": failure.message})
            return
        except Exception:
            log.exception("export %s failed", export_id)
            shutil.rmtree(target, ignore_errors=True)
            self._finish(
                export_id, None, {"code": "EXPORT_FAILED", "message": "Unexpected export error"}
            )
            return
        self._finish(export_id, summary, None)

    def _select_artifacts(self, conn, request: dict) -> list[dict]:
        if not request["include_artifacts"]:
            return []
        query = (
            select(t.artifacts, t.tasks.c.task_key)
            .outerjoin(t.trials, t.trials.c.id == t.artifacts.c.trial_id)
            .outerjoin(t.tasks, t.tasks.c.id == t.trials.c.task_id)
            .where(t.artifacts.c.run_id.in_(request["run_ids"]))
            .order_by(t.artifacts.c.run_id, t.artifacts.c.created_at, t.artifacts.c.id)
        )
        if request.get("artifact_kinds"):
            query = query.where(t.artifacts.c.kind.in_(request["artifact_kinds"]))
        rows = [dict(r) for r in conn.execute(query).mappings()]
        if request.get("task_keys"):
            rows = [r for r in rows if r["task_key"] in request["task_keys"]]
        removed = deleted.states(conn, "artifact", [r["id"] for r in rows])
        for row in rows:
            if row["id"] in removed:
                row["status"], row["status_reason"] = "deleted", "DELETED"
        return rows

    def _write(
        self, export_id: str, request: dict, selected: list[dict], target: Path, pin_until
    ) -> dict:
        root = f"export-{export_id}"
        files, missing, warnings = [], [], []
        partial = target / f".{ARCHIVE}.tmp"
        task_filter = set(request.get("task_keys") or [])
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:

            def add_json(path: str, value) -> None:
                data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode()
                archive.writestr(f"{root}/{path}", data)
                files.append(
                    {
                        "path": path,
                        "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                        "size_bytes": len(data),
                    }
                )

            runs_index = []
            for run_id in request["run_ids"]:
                run = self.runs.get(None, run_id)
                summary = self.runs.summary(run_id)
                trials = self.runs.trials(run_id)
                if task_filter:
                    trials = [x for x in trials if x["task_key"] in task_filter]
                run_dir = f"runs/{safe_name(run_id, 'run')}"
                task_dirs = {}
                for trial in trials:
                    task_dirs.setdefault(
                        trial["task_key"],
                        f"{run_dir}/tasks/{safe_name(trial['task_key'], 'task')}",
                    )
                if len(set(task_dirs.values())) != len(task_dirs):
                    # Distinct keys that sanitize alike get a stable suffix.
                    task_dirs = {
                        key: f"{run_dir}/tasks/{safe_name(key, 'task')}-"
                        f"{hashlib.sha256(key.encode()).hexdigest()[:8]}"
                        for key in task_dirs
                    }
                by_trial: dict[str | None, list[dict]] = {}
                for item in selected:
                    if item["run_id"] == run_id:
                        by_trial.setdefault(item["trial_id"], []).append(item)
                for trial in trials:
                    entries = []
                    trial_dir = f"{task_dirs[trial['task_key']]}/trials/{trial['id']}"
                    for item in by_trial.get(trial["id"], []):
                        entries.append(
                            self._add_artifact(
                                archive,
                                root,
                                f"{trial_dir}/artifacts",
                                item,
                                files,
                                missing,
                                warnings,
                                pin_until,
                            )
                        )
                    add_json(f"{trial_dir}/result.json", {**trial, "artifacts": entries})
                run_entries = [
                    self._add_artifact(
                        archive,
                        root,
                        f"{run_dir}/artifacts",
                        item,
                        files,
                        missing,
                        warnings,
                        pin_until,
                    )
                    for item in by_trial.get(None, [])
                ]
                add_json(
                    f"{run_dir}/config.json",
                    {
                        "run": {k: v for k, v in run.items() if k not in {"snapshot", "links"}},
                        "snapshot": run["snapshot"],
                        "run_artifacts": run_entries,
                    },
                )
                add_json(f"{run_dir}/summary.json", summary)
                if run["deletion"]:
                    warnings.append({"code": "RUN_DELETED", "run_id": run_id})
                runs_index.append(
                    {
                        "run_id": run_id,
                        "name": run["name"],
                        "owner": run["owner"],
                        "state": run["state"],
                        "execution_mode": run["snapshot"]["effective"].get("execution_mode"),
                        "dataset": run["snapshot"]["effective"].get("dataset"),
                        "config_digest": run["snapshot"]["config_digest"],
                        "snapshot_digest": run["snapshot"]["snapshot_digest"],
                        "comparison_digest": run["snapshot"]["comparison_digest"],
                        "scoring_contract": summary["contract"],
                        "aggregation_version": run["snapshot"]["policy"].get("aggregation_version"),
                        "policy_version": run["snapshot"]["policy"].get("policy_version"),
                        "deletion": run["deletion"],
                        "path": run_dir,
                        "tasks": task_dirs,
                    }
                )
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "export_id": export_id,
                "generated_at": clock.to_text(clock.now()),
                "service_version": __version__,
                "executor_mapping": MAPPING_ID,
                "policy_version": POLICY_VERSION,
                "request": request,
                "runs": runs_index,
                "files": files,
                "missing": missing,
                "warnings": warnings,
            }
            archive.writestr(
                f"{root}/manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode(),
            )
        with open(partial, "rb") as file:
            os.fsync(file.fileno())
        os.replace(partial, target / ARCHIVE)
        digest = hashlib.sha256()
        with open(target / ARCHIVE, "rb") as file:
            for chunk in iter(lambda: file.read(1 << 20), b""):
                digest.update(chunk)
        return {
            "runs": len(runs_index),
            "files": len(files),
            "missing": len(missing),
            "warnings": len(warnings),
            # Only files that went into the package, trial and run level alike.
            "artifact_bytes": sum(f["size_bytes"] for f in files if "artifact_id" in f),
            "size_bytes": (target / ARCHIVE).stat().st_size,
            "digest": "sha256:" + digest.hexdigest(),
        }

    def _add_artifact(
        self, archive, root, directory, item, files, missing, warnings, pin_until
    ) -> dict:
        entry = {
            "artifact_id": item["id"],
            "kind": item["kind"],
            "relative_path": item["relative_path"],
            "attempt_id": item["attempt_id"],
            "digest": item["digest"],
            "size_bytes": item["size_bytes"],
        }

        def absent(reason: str) -> dict:
            missing.append(
                {
                    **entry,
                    "run_id": item["run_id"],
                    "trial_id": item["trial_id"],
                    "reason": reason,
                    "expired_at": item["expires_at"] if reason == "EXPIRED" else None,
                }
            )
            return {**entry, "included": False, "reason": reason}

        status = item["status"]
        if status != "available":
            reason = {
                "expired": "EXPIRED",
                "deleting": "EXPIRED",
                "deleted": "DELETED",
                "failed": "SAVE_FAILED",
            }.get(status, item["status_reason"] or status.upper())
            return absent(reason)
        if clock.now() >= pin_until:
            raise ExportFailed(
                "EXPORT_PIN_EXPIRED",
                f"Export took longer than {limits.EXPORT_PIN_MINUTES} minutes; narrow it",
            )
        name = safe_name(item["relative_path"].rsplit("/", 1)[-1], "file")
        path = f"{directory}/{item['id']}/{name}"
        digest = hashlib.sha256()
        size = 0
        try:
            with (
                self.store.open(item["storage_key"]) as source,
                archive.open(f"{root}/{path}", "w", force_zip64=True) as sink,
            ):
                for chunk in iter(lambda: source.read(1 << 20), b""):
                    digest.update(chunk)
                    size += len(chunk)
                    sink.write(chunk)
        except (FileNotFoundError, KeyError):
            return absent("STORAGE_CONTENT_MISSING")
        actual = "sha256:" + digest.hexdigest()
        files.append(
            {"path": path, "digest": actual, "size_bytes": size, "artifact_id": item["id"]}
        )
        if actual != item["digest"]:
            warnings.append(
                {"code": "DIGEST_MISMATCH", "artifact_id": item["id"], "indexed": item["digest"]}
            )
        return {**entry, "included": True, "path": path}

    def _finish(self, export_id: str, summary: dict | None, error: dict | None) -> None:
        now = clock.now()
        at = clock.to_text(now)
        with self.db.write() as conn:
            row = self._row(conn, export_id)
            if summary and self._purged_since(conn, row):
                # A purge ran during the build; its content must not be published.
                shutil.rmtree(self.directory / export_id, ignore_errors=True)
                summary, error = (
                    None,
                    {
                        "code": "EXPORT_CONTENT_PURGED",
                        "message": "A run or file in this export was permanently deleted; "
                        "export again",
                    },
                )
            conn.execute(
                delete(t.artifact_pins).where(
                    t.artifact_pins.c.holder_kind == "export",
                    t.artifact_pins.c.holder_id == export_id,
                )
            )
            conn.execute(
                update(t.exports)
                .where(t.exports.c.id == export_id)
                .values(
                    state="ready" if summary else "failed",
                    size_bytes=summary and summary["size_bytes"],
                    digest=summary and summary["digest"],
                    summary_json=summary,
                    error_json=error,
                    finished_at=at,
                    expires_at=clock.to_text(
                        now + clock.timedelta(hours=policy.current(conn).export_ttl_hours)
                    ),
                )
            )
            operations.advance(
                conn,
                row["operation_id"],
                status=OperationStatus.SUCCEEDED if summary else OperationStatus.FAILED,
                phase="ready" if summary else "failed",
                progress={
                    "runs": len(row["request_json"]["run_ids"]),
                    "files": summary["files"] if summary else 0,
                },
                result={"export_id": export_id, **(summary or {})},
                error=error,
            )

    @staticmethod
    def _purged_since(conn, row) -> bool:
        """Whether a requested run, or a file of one, was purged after the export was
        accepted. ``expire`` cannot reach a package that is still being built."""
        run_ids = row["request_json"]["run_ids"]
        purged = select(t.deletions.c.object_id).where(
            t.deletions.c.state == deleted.PURGED, t.deletions.c.purged_at >= row["created_at"]
        )
        runs = conn.execute(
            purged.where(
                t.deletions.c.object_type == "run", t.deletions.c.object_id.in_(run_ids)
            ).limit(1)
        ).first()
        files = conn.execute(
            purged.where(
                t.deletions.c.object_type == "artifact",
                t.deletions.c.object_id.in_(
                    select(t.artifacts.c.id).where(t.artifacts.c.run_id.in_(run_ids))
                ),
            ).limit(1)
        ).first()
        return bool(runs or files)

    # --- retention -----------------------------------------------------------------

    def expire(self, run_ids: set[str] | None = None) -> int:
        """Remove packages past their TTL, or any package containing ``run_ids``
        (a purged run must not survive in an export)."""
        at = clock.to_text(clock.now())
        with self.db.read() as conn:
            rows = conn.execute(
                select(t.exports.c.id, t.exports.c.request_json, t.exports.c.expires_at).where(
                    t.exports.c.state.in_(["ready", "failed"])
                )
            ).all()
        due = [
            r.id
            for r in rows
            if (r.expires_at and r.expires_at <= at)
            or (run_ids and run_ids & set(r.request_json["run_ids"]))
        ]
        for export_id in due:
            shutil.rmtree(self.directory / export_id, ignore_errors=True)
        if due:
            with self.db.write() as conn:
                conn.execute(
                    update(t.exports).where(t.exports.c.id.in_(due)).values(state="expired")
                )
        return len(due)
