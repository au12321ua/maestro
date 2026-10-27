"""Team-readable dataset catalog (02, 10 section 4).

Admission, content availability and the local cache are reported separately;
``can_submit`` derives from admission, availability and required configuration,
never from cache state. A cold cache only adds a warning and a restore hint.
"""

from sqlalchemy import Connection, select

from maestro.core.errors import not_found
from maestro.db import schema as t
from maestro.db.engine import Database
from maestro.domain import scoring
from maestro.execution.materials import configuration_blockers, required_parameters
from maestro.services import deleted, pagination

ADMITTED = "admitted"


def admission_blocker(admission) -> dict | None:
    if admission is not None and admission["state"] == ADMITTED:
        return None
    state = admission["state"] if admission else "pending"
    if state == "revoked":
        return {"code": "TASK_ADMISSION_REVOKED", "admission_state": state}
    blocker = {"code": "TASK_NOT_ADMITTED", "admission_state": state}
    error = (admission["evidence_json"] or {}).get("error") if admission else None
    if error:
        blocker["first_preparation_error"] = {
            k: error.get(k)
            for k in ("code", "kind", "message", "suggested_actions")
            if error.get(k)
        }
    return blocker


def expected_image(admission) -> str | None:
    if admission is None or admission["state"] != ADMITTED:
        return None
    return ((admission["evidence_json"] or {}).get("output") or {}).get("image_id")


def task_blockers(task, admission, availability: dict) -> list[dict]:
    """Shared by the catalog and run acceptance, so both apply the same rules."""
    blockers = []
    blocked = admission_blocker(admission)
    if blocked:
        blockers.append(blocked)
    resources = [("task_content", task["content_digest"])]
    image = expected_image(admission)
    if image:
        resources.append(("environment_image", image))
    for kind, digest in resources:
        row = availability.get((digest, kind))
        if row is not None and row["availability"] == "unavailable":
            evidence = row["evidence_json"] or {}
            blockers.append(
                {
                    "code": "RESOURCE_CONTENT_UNAVAILABLE",
                    "resource_kind": kind,
                    "expected_digest": digest,
                    "evidence": {k: evidence[k] for k in ("reason", "message") if k in evidence},
                }
            )
    blockers.extend(configuration_blockers(task["definition_json"] or {}))
    return blockers


def load_admissions(conn: Connection, task_ids: list[str]) -> dict:
    """Current admission per task: the latest revision of its preparation method."""
    rows = conn.execute(
        select(t.task_admissions)
        .where(t.task_admissions.c.task_id.in_(task_ids))
        .order_by(t.task_admissions.c.revision)
    ).mappings()
    return {a["task_id"]: a for a in rows}


def load_availability(conn: Connection, digests: list[str]) -> dict:
    rows = conn.execute(
        select(t.content_availability).where(t.content_availability.c.content_digest.in_(digests))
    ).mappings()
    return {(a["content_digest"], a["resource_kind"]): a for a in rows}


def preparation_view(row, location_id: str) -> dict:
    if row is None:
        return {
            "id": None,
            "location_id": location_id,
            "cache_state": "absent",
            "last_restore_error": None,
            "operation_id": None,
            "prepared_at": None,
            "last_used_at": None,
            "setup_network_revision": None,
        }
    output = row["output_json"] or {}
    return {
        "id": row["id"],
        "location_id": row["location_id"],
        "cache_state": row["cache_state"],
        "last_restore_error": row["last_restore_error_json"],
        "operation_id": row["active_operation_id"],
        "prepared_at": row["prepared_at"],
        "last_used_at": row["last_used_at"],
        "setup_network_revision": (output.get("detail") or {}).get("setup_network_revision"),
    }


def task_view(task, admission, availability: dict, preparation, location_id: str) -> dict:
    definition = task["definition_json"] or {}
    blockers = task_blockers(task, admission, availability)
    local = preparation_view(preparation, location_id)
    warnings, required = [], []
    if not blockers and local["cache_state"] != "ready":
        warnings.append({"code": "PREPARATION_CACHE_MISS", "action": "restore_on_run"})
        required.append({"action": "restore_cache", "spec_digest": task["spec_digest"]})
    resources = [("task_content", task["content_digest"])]
    image = expected_image(admission)
    if image:
        resources.append(("environment_image", image))
    content = []
    for kind, digest in resources:
        row = availability.get((digest, kind))
        content.append(
            {
                "resource_kind": kind,
                "digest": digest,
                "availability": row["availability"] if row else "unknown",
                "checked_at": row["checked_at"] if row else None,
            }
        )
    worst = next((c for c in content if c["availability"] == "unavailable"), content[0])
    return {
        "id": task["id"],
        "dataset_version_id": task["dataset_version_id"],
        "task_key": task["task_key"],
        "admission": (
            {
                "id": admission["id"],
                "state": admission["state"],
                "revision": admission["revision"],
                "approved_at": admission["approved_at"],
            }
            if admission
            else None
        ),
        "content": {
            "digest": task["content_digest"],
            "material_digest": task["material_digest"] or None,
            "availability": worst["availability"],
            "checked_at": worst["checked_at"],
            "resources": content,
        },
        "local_preparation": local,
        "spec_digest": task["spec_digest"] or None,
        "resources": definition.get("resources") or {},
        "timeouts": definition.get("timeouts") or {},
        # Author constraints are not modelled yet (always empty): not part of the API.
        "parameters": [
            {k: v for k, v in x.items() if k != "hard_constraints"}
            for x in definition.get("parameters") or []
        ],
        # Values a run must supply (03); not a blocker of the task itself.
        "required_parameters": required_parameters(definition),
        "instruction": definition.get("instruction") or "",
        "can_submit": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "required_preparation": required,
    }


class Catalog:
    def __init__(self, db: Database, location_id: str = "local-docker"):
        self.db = db
        self.location_id = location_id

    def list_datasets(self, cursor: str | None, limit: int | None) -> tuple[list, str | None]:
        limit = pagination.check_limit(limit)
        query = pagination.page(
            select(t.datasets).where(
                t.datasets.c.enabled.is_(True), deleted.visible(t.datasets.c.id, "dataset")
            ),
            t.datasets.c.created_at,
            t.datasets.c.id,
            cursor,
            limit,
            {},
        )
        with self.db.read() as conn:
            rows = list(conn.execute(query).mappings().all())
            rows, next_cursor = pagination.split(rows, limit, "created_at", {})
            result = []
            for row in rows:
                versions = conn.execute(
                    select(t.dataset_versions)
                    .where(
                        t.dataset_versions.c.dataset_id == row["id"],
                        deleted.visible(t.dataset_versions.c.id, "dataset_version"),
                    )
                    .order_by(t.dataset_versions.c.created_at.desc())
                ).mappings()
                result.append(
                    {
                        "id": row["id"],
                        "source_kind": row["source_kind"],
                        "namespace": row["namespace"],
                        "name": row["name"],
                        "description": row["description"],
                        "credential_id": row["credential_id"],
                        "versions": [
                            {**self._version(v), **self._counts(conn, v["id"])} for v in versions
                        ],
                    }
                )
        return result, next_cursor

    @staticmethod
    def _version(row) -> dict:
        manifest = row["manifest_json"] or {}
        return {
            "id": row["id"],
            "dataset_id": row["dataset_id"],
            "upstream_ref": row["upstream_ref"],
            "ref_kind": row["ref_kind"],
            "resolved_digest": row["resolved_digest"],
            "materialization_digest": row["materialization_digest"],
            "status": row["status"],
            "description": manifest.get("description") or "",
            "created_at": row["created_at"],
            "created_by": row["created_by"],
            "operation_id": row["operation_id"],
        }

    def _counts(self, conn, version_id: str) -> dict:
        tasks = self._tasks(conn, version_id)
        return {
            "task_count": len(tasks),
            "admitted_task_count": sum(
                1 for x in tasks if x["admission"] and x["admission"]["state"] == ADMITTED
            ),
            "submittable_task_count": sum(1 for x in tasks if x["can_submit"]),
            "cache_ready_task_count": sum(
                1 for x in tasks if x["local_preparation"]["cache_state"] == "ready"
            ),
        }

    def get_version(self, version_id: str) -> dict:
        with self.db.read() as conn:
            row = (
                conn.execute(
                    select(t.dataset_versions, t.datasets.c.namespace, t.datasets.c.name)
                    .join(t.datasets, t.datasets.c.id == t.dataset_versions.c.dataset_id)
                    .where(t.dataset_versions.c.id == version_id)
                )
                .mappings()
                .first()
            )
            if row is None:
                raise not_found("Dataset version")
            active = conn.execute(
                select(t.operations.c.id).where(
                    t.operations.c.kind == "preparation_first",
                    t.operations.c.resource_id == version_id,
                    t.operations.c.status.in_(["accepted", "running"]),
                )
            ).scalar()
            return {
                **self._version(row),
                "namespace": row["namespace"],
                "name": row["name"],
                "first_preparation_operation_id": active,
                "scoring_contract": scoring.contract_for(row["manifest_json"]),
                # Deleted versions still resolve for runs that reference them (07 section 8).
                "deletion": deleted.version_state(conn, version_id),
                **self._counts(conn, version_id),
            }

    def list_tasks(self, version_id: str) -> list[dict]:
        with self.db.read() as conn:
            if not conn.execute(
                select(t.dataset_versions.c.id).where(t.dataset_versions.c.id == version_id)
            ).first():
                raise not_found("Dataset version")
            return self._tasks(conn, version_id)

    def _tasks(self, conn, version_id: str) -> list[dict]:
        tasks = (
            conn.execute(
                select(t.tasks)
                .where(t.tasks.c.dataset_version_id == version_id)
                .order_by(t.tasks.c.task_key)
            )
            .mappings()
            .all()
        )
        admissions = load_admissions(conn, [x["id"] for x in tasks])
        digests = [x["content_digest"] for x in tasks] + [
            d for a in admissions.values() if (d := expected_image(a))
        ]
        availability = load_availability(conn, digests)
        preparations = {
            p["spec_digest"]: p
            for p in conn.execute(
                select(t.preparations).where(
                    t.preparations.c.spec_digest.in_([x["spec_digest"] for x in tasks]),
                    t.preparations.c.location_id == self.location_id,
                )
            ).mappings()
        }
        return [
            task_view(
                x,
                admissions.get(x["id"]),
                availability,
                preparations.get(x["spec_digest"]),
                self.location_id,
            )
            for x in tasks
        ]

    def get_preparation(self, preparation_id: str) -> dict:
        with self.db.read() as conn:
            row = (
                conn.execute(select(t.preparations).where(t.preparations.c.id == preparation_id))
                .mappings()
                .first()
            )
            if row is None:
                raise not_found("Preparation")
            waiters = []
            if row["active_operation_id"]:
                waiters = (
                    conn.execute(
                        select(t.preparation_waiters)
                        .where(t.preparation_waiters.c.operation_id == row["active_operation_id"])
                        .order_by(t.preparation_waiters.c.created_at)
                    )
                    .mappings()
                    .all()
                )
            pins = conn.execute(
                select(t.preparation_pins.c.holder_kind, t.preparation_pins.c.holder_id).where(
                    t.preparation_pins.c.preparation_id == preparation_id
                )
            ).all()
        output = row["output_json"] or {}
        archive = output.get("archive")
        return {
            **preparation_view(row, self.location_id),
            "spec_digest": row["spec_digest"],
            "generation": row["generation"],
            "image_id": output.get("image_id"),
            "archive": (
                {"digest": archive.get("digest"), "size_bytes": archive.get("size_bytes")}
                if archive
                else None
            ),
            "waiters": [
                {
                    "run_id": w["run_id"],
                    "operation_id": w["holder_operation_id"],
                    "state": w["state"],
                    "since": w["created_at"],
                }
                for w in waiters
            ],
            "pins": [{"holder_kind": k, "holder_id": h} for k, h in pins],
        }
