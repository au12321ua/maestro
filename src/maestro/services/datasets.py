"""Dataset sources and administration.

Listing the source kinds is open to every authenticated caller. Searching a source
uses the team Hub credential, and adding versions, first preparation and its retry,
cache eviction and admission revocation change the platform: all of them need an
administrator's web session or a datasets-enabled administrator PAT. Reading what is already imported is in
``services.catalog``. The long work runs as
Operations on the worker loop (``services.preparation``); these methods only
validate, record and return the Operation.
"""

from sqlalchemy import insert, select, update

from maestro.boundaries.dataset_source import DatasetSource, SourceError
from maestro.core import clock
from maestro.core.errors import FieldError, MaestroError, not_found
from maestro.db import schema as t
from maestro.db.engine import Database
from maestro.domain import limits, scoring
from maestro.domain.states import PatPermission
from maestro.execution.attachments import NAME, PackageError
from maestro.services import audit, deleted, operations
from maestro.services.attachments import _held
from maestro.services.preparation import (
    KIND_EVICT,
    KIND_IMPORT,
    create_first,
    first_in_progress,
)
from maestro.services.principal import Principal, require_admin

# Contract fields whose only supported value is the platform default's.
_FIXED_RULES = ("task_aggregation", "bench_aggregation", "count_as_failure")

SOURCE_ERRORS = {
    "auth": ("SOURCE_AUTH_FAILED", 502),
    "transient": ("SOURCE_UNAVAILABLE_TRY_AGAIN", 503),
    "not_found": ("SOURCE_VERSION_NOT_FOUND", 404),
    "mismatch": ("SOURCE_CONTENT_MISMATCH", 502),
    "unsupported": ("SOURCE_MANIFEST_UNSUPPORTED", 422),
}


def _source_error(error: SourceError) -> MaestroError:
    code, status = SOURCE_ERRORS.get(error.kind, ("SOURCE_FAILED", 502))
    actions = [{"type": "check_dataset_credential"}] if error.kind == "auth" else []
    return MaestroError(
        code,
        error.message or code,
        status,
        stage="source",
        retryable=error.kind == "transient",
        suggested_actions=actions,
    )


class Datasets:
    def __init__(self, db: Database, sources: dict[str, DatasetSource], credentials, store=None):
        self.db = db
        self.sources = sources
        self.credentials = credentials
        self.store = store

    # --- sources ---------------------------------------------------------------------

    def source_list(self) -> list[dict]:
        return [{"kind": kind, "available": True} for kind in sorted(self.sources)]

    def _source(self, kind: str) -> DatasetSource:
        source = self.sources.get(kind)
        if source is None:
            raise MaestroError("SOURCE_NOT_CONFIGURED", f"No dataset source {kind!r}", 422)
        return source

    @staticmethod
    def _upstream(kind: str) -> None:
        if kind == "upload":
            raise MaestroError(
                "SOURCE_OPERATION_UNAVAILABLE",
                "Uploaded datasets have no upstream updates or Hub credentials; upload a directory",
                422,
            )

    def _check_credential(self, credential_id: str | None) -> None:
        if credential_id is None:
            return
        with self.db.read() as conn:
            row = conn.execute(
                select(t.credentials.c.kind, t.credentials.c.enabled).where(
                    t.credentials.c.id == credential_id
                )
            ).first()
            removed = deleted.state(conn, "credential", credential_id)
        if row is None or row.kind != "hub_token":
            raise MaestroError(
                "CONFIG_VALIDATION_FAILED",
                "Choose a dataset-source credential",
                422,
                field_errors=[FieldError("/credential_id", "INVALID")],
            )
        if removed:  # the refusal of Credentials._live, before an import can be accepted
            raise MaestroError("CREDENTIAL_DELETED", "Credential was deleted", 409)
        if not row.enabled:
            raise MaestroError("CREDENTIAL_DISABLED", "Credential is disabled", 409)

    def _value(self, credential_id: str | None) -> str | None:
        self._check_credential(credential_id)
        return self.credentials.resolve_value(credential_id)[0] if credential_id else None

    async def search(
        self, principal: Principal, kind: str, query: str, credential_id: str | None
    ) -> list[dict]:
        require_admin(principal, PatPermission.DATASETS)
        self._upstream(kind)
        if not 1 <= len(query.strip()) <= 128:
            raise MaestroError(
                "CONFIG_VALIDATION_FAILED",
                "Enter 1-128 characters",
                422,
                field_errors=[FieldError("/q", "LENGTH")],
            )
        try:
            return await self._source(kind).search(query.strip(), self._value(credential_id))
        except SourceError as error:
            raise _source_error(error) from None

    async def versions(
        self, principal: Principal, kind: str, namespace: str, name: str, credential_id: str | None
    ):
        require_admin(principal, PatPermission.DATASETS)
        self._upstream(kind)
        try:
            return await self._source(kind).list_versions(
                namespace, name, self._value(credential_id)
            )
        except SourceError as error:
            raise _source_error(error) from None

    async def describe(
        self,
        principal: Principal,
        kind: str,
        namespace: str,
        name: str,
        ref: str,
        credential_id: str | None,
    ) -> dict:
        """Preview of a fixed version: digest and task list, nothing is recorded."""
        require_admin(principal, PatPermission.DATASETS)
        self._upstream(kind)
        try:
            version = await self._source(kind).describe(
                namespace, name, ref, self._value(credential_id)
            )
        except SourceError as error:
            raise _source_error(error) from None
        with self.db.read() as conn:
            existing = conn.execute(
                select(t.dataset_versions.c.id)
                .join(t.datasets, t.datasets.c.id == t.dataset_versions.c.dataset_id)
                .where(
                    t.datasets.c.source_kind == kind,
                    t.datasets.c.namespace == version.namespace,
                    t.datasets.c.name == version.name,
                    t.dataset_versions.c.resolved_digest == version.resolved_digest,
                    deleted.version_visible(t.dataset_versions.c.id),
                )
            ).scalar()
        return {
            "namespace": version.namespace,
            "name": version.name,
            "upstream_ref": version.upstream_ref,
            "ref_kind": version.ref_kind,
            "resolved_digest": version.resolved_digest,
            "description": version.description,
            "tasks": [
                {"task_key": x.task_key, "content_digest": x.content_digest} for x in version.tasks
            ],
            "existing_version_id": existing,
        }

    # --- administration ---------------------------------------------------------------

    def request_upload(self, principal: Principal, body: dict) -> dict:
        """Hold the original file list for a normal import and first preparation."""
        require_admin(principal, PatPermission.DATASETS)
        source = self._source("upload")
        if source.validator.runner_python is None:
            raise MaestroError(
                "RUNNER_NOT_CONFIGURED", "Dataset upload validation needs the Runner", 422
            )
        for key in ("namespace", "name"):
            if not isinstance(body.get(key), str) or not NAME.fullmatch(body[key]):
                raise MaestroError(
                    "DATASET_UPLOAD_INVALID",
                    f"Invalid {key}",
                    422,
                    field_errors=[FieldError(f"/{key}", "PATTERN")],
                )
        try:
            package = self.store.check(
                body.get("files"),
                limits.DATASET_UPLOAD_MAX_FILES,
                limits.DATASET_UPLOAD_MAX_BYTES,
                attachment=False,
            )
        except PackageError as error:
            raise MaestroError(
                "DATASET_UPLOAD_INVALID",
                str(error),
                422,
                field_errors=[FieldError(p, code, message) for p, code, message in error.problems],
            ) from None
        if any("/" not in f["path"] for f in package.files):
            raise MaestroError(
                "DATASET_UPLOAD_INVALID", "Every file must be inside a task directory", 422
            )
        request = {
            "source_kind": "upload",
            "namespace": body["namespace"],
            "name": body["name"],
            "ref": package.content_digest,
            "prepare": True,
            "credential_id": None,
            "accept_changed_content": False,
        }
        with self.db.write() as conn:
            held = _held(conn, [f["sha256"] for f in package.files], principal.user_id)
            if any(
                f["sha256"] not in held or self.store.blob(f["sha256"]) is None
                for f in package.files
            ):
                raise MaestroError(
                    "DATASET_CONTENT_MISSING", "Upload missing file contents first", 422
                )
            op_id = operations.create(
                conn,
                KIND_IMPORT,
                owner_id=principal.user_id,
                progress={"request": request},
                phase="queued",
            )
            conn.execute(
                insert(t.dataset_files),
                [
                    {**f, "operation_id": op_id, "dataset_digest": package.content_digest}
                    for f in package.files
                ],
            )
            audit.record(
                conn,
                principal,
                "dataset.upload",
                object_type="operation",
                object_id=op_id,
                detail={
                    "namespace": body["namespace"],
                    "name": body["name"],
                    "digest": package.content_digest,
                },
            )
            return operations.public(
                conn.execute(select(t.operations).where(t.operations.c.id == op_id))
                .mappings()
                .one()
            )

    def request_import(self, principal: Principal, body: dict) -> dict:
        require_admin(principal, PatPermission.DATASETS)
        kind = body.get("source_kind") or "hub"
        self._upstream(kind)
        self._source(kind)
        errors = []
        for field in ("namespace", "name", "ref"):
            value = body.get(field)
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 256:
                errors.append(FieldError(f"/{field}", "REQUIRED"))
        if errors:
            raise MaestroError(
                "CONFIG_VALIDATION_FAILED", "Invalid import request", 422, field_errors=errors
            )
        self._check_credential(body.get("credential_id"))
        request = {
            "source_kind": kind,
            "namespace": body["namespace"].strip(),
            "name": body["name"].strip(),
            "ref": body["ref"].strip(),
            "credential_id": body.get("credential_id"),
            "accept_changed_content": bool(body.get("accept_changed_content")),
            "prepare": body.get("prepare", True) is not False,
        }
        with self.db.write() as conn:
            op_id = operations.create(
                conn,
                KIND_IMPORT,
                owner_id=principal.user_id,
                progress={"request": request},
                phase="queued",
            )
            audit.record(
                conn,
                principal,
                "dataset.import",
                object_type="operation",
                object_id=op_id,
                detail={k: request[k] for k in ("source_kind", "namespace", "name", "ref")},
            )
            row = conn.execute(select(t.operations).where(t.operations.c.id == op_id)).mappings()
            return operations.public(row.one())

    def request_first_preparation(
        self, principal: Principal, version_id: str, task_ids: list[str] | None
    ) -> dict:
        """First preparation, or a retry of the tasks whose first preparation failed."""
        require_admin(principal, PatPermission.DATASETS)
        with self.db.write() as conn:
            if not conn.execute(
                select(t.dataset_versions.c.id).where(t.dataset_versions.c.id == version_id)
            ).first():
                raise not_found("Dataset version")
            if first_in_progress(conn, version_id):
                raise MaestroError(
                    "PREPARATION_IN_PROGRESS", "A first preparation is already running", 409
                )
            rows = conn.execute(
                select(t.tasks.c.id, t.task_admissions.c.state)
                .join(t.task_admissions, t.task_admissions.c.task_id == t.tasks.c.id)
                .where(t.tasks.c.dataset_version_id == version_id)
            ).all()
            states = dict(rows)
            if task_ids is None:
                chosen = [k for k, s in states.items() if s in {"pending", "first_failed"}]
            else:
                bad = [x for x in task_ids if states.get(x) not in {"pending", "first_failed"}]
                if bad:
                    raise MaestroError(
                        "TASKS_NOT_PREPARABLE",
                        "Only tasks without a successful first preparation can be prepared",
                        422,
                        field_errors=[FieldError("/task_ids", "INVALID", x) for x in bad],
                    )
                chosen = list(dict.fromkeys(task_ids))
            if not chosen:
                raise MaestroError("NOTHING_TO_PREPARE", "Every task is already admitted", 409)
            op_id = create_first(conn, principal.user_id, version_id, chosen)
            audit.record(
                conn,
                principal,
                "dataset.first_preparation",
                object_type="dataset_version",
                object_id=version_id,
                detail={"operation_id": op_id, "tasks": len(chosen)},
            )
            row = conn.execute(select(t.operations).where(t.operations.c.id == op_id)).mappings()
            return operations.public(row.one())

    def request_eviction(self, principal: Principal, preparation_id: str) -> dict:
        require_admin(principal, PatPermission.DATASETS)
        with self.db.write() as conn:
            row = conn.execute(
                select(t.preparations.c.cache_state).where(t.preparations.c.id == preparation_id)
            ).first()
            if row is None:
                raise not_found("Preparation")
            if row.cache_state != "ready":
                raise MaestroError("NOT_READY", "Only a ready cache can be evicted", 409)
            op_id = operations.create(
                conn,
                KIND_EVICT,
                owner_id=principal.user_id,
                resource_type="preparation",
                resource_id=preparation_id,
                phase="queued",
            )
            audit.record(
                conn,
                principal,
                "preparation.evict",
                object_type="preparation",
                object_id=preparation_id,
                detail={"operation_id": op_id},
            )
            found = conn.execute(select(t.operations).where(t.operations.c.id == op_id)).mappings()
            return operations.public(found.one())

    def confirm_scoring_contract(self, principal: Principal, version_id: str, body: dict) -> dict:
        """The administrator confirms how this version is scored. New runs
        freeze it in their snapshot; accepted runs keep the contract they started with."""
        require_admin(principal, PatPermission.DATASETS)
        contract = {
            "metric_id": body.get("metric_id", "reward"),
            "name": body.get("name", "Reward"),
            "unit": body.get("unit"),
            "direction": body.get("direction"),
            "passed": (
                {"rule": "threshold", "threshold": body["pass_threshold"]}
                if body.get("pass_threshold") is not None
                else None
            ),
            # Aggregation rules the maths does not apply are refused here, not stored
            # and later echoed in summaries as if they had been used.
            **{key: body[key] for key in _FIXED_RULES if key in body},
        }
        errors = scoring.contract_errors({**scoring.DEFAULT_CONTRACT, **contract})
        if errors:
            raise MaestroError(
                "CONFIG_VALIDATION_FAILED",
                "The scoring contract has fields the platform cannot compute",
                422,
                field_errors=[
                    FieldError(
                        "/pass_threshold" if name == "passed" else f"/{name}",
                        "INVALID",
                        f"Only {scoring.DEFAULT_CONTRACT[name]!r} is supported"
                        if name in _FIXED_RULES
                        else None,
                    )
                    for name in errors
                ],
            )
        with self.db.write() as conn:
            row = (
                conn.execute(
                    select(t.dataset_versions).where(t.dataset_versions.c.id == version_id)
                )
                .mappings()
                .first()
            )
            if row is None:
                raise not_found("Dataset version")
            if deleted.version_state(conn, version_id):
                raise MaestroError("DATASET_VERSION_DELETED", "This version is deleted", 409)
            manifest = {
                **(row["manifest_json"] or {}),
                "scoring_contract": contract,
                "scoring_contract_confirmed": {"by": principal.user_id, "at": clock_text()},
            }
            conn.execute(
                update(t.dataset_versions)
                .where(t.dataset_versions.c.id == version_id)
                .values(manifest_json=manifest)
            )
            audit.record(
                conn,
                principal,
                "dataset.scoring_contract",
                object_type="dataset_version",
                object_id=version_id,
                detail=contract,
            )
        return scoring.contract_for(manifest)

    def revoke_admission(
        self, principal: Principal, admission_id: str, reason: str, expected_revision: int
    ) -> dict:
        """Explicit safety stop; runs already accepted keep their snapshot."""
        require_admin(principal, PatPermission.DATASETS)
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 512:
            raise MaestroError(
                "CONFIG_VALIDATION_FAILED",
                "Give a reason",
                422,
                field_errors=[FieldError("/reason", "LENGTH")],
            )
        with self.db.write() as conn:
            row = (
                conn.execute(
                    select(t.task_admissions).where(t.task_admissions.c.id == admission_id)
                )
                .mappings()
                .first()
            )
            if row is None:
                raise not_found("Admission")
            if row["revision"] != expected_revision:
                raise MaestroError("REVISION_CONFLICT", "Admission changed; reload", 409)
            if row["state"] == "revoked":
                raise MaestroError("ALREADY_REVOKED", "Admission is already revoked", 409)
            conn.execute(
                update(t.task_admissions)
                .where(t.task_admissions.c.id == admission_id)
                .values(
                    state="revoked",
                    revision=row["revision"] + 1,
                    evidence_json={
                        **(row["evidence_json"] or {}),
                        "revoked": {"by": principal.user_id, "reason": reason.strip()},
                    },
                )
            )
            audit.record(
                conn,
                principal,
                "admission.revoke",
                object_type="task_admission",
                object_id=admission_id,
                detail={"reason": reason.strip()},
            )
            return {"id": admission_id, "state": "revoked", "revision": row["revision"] + 1}


def clock_text() -> str:
    return clock.to_text(clock.now())
