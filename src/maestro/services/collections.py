"""Comparisons, collections and analysis snapshots (06 section 8, 01, 04 section 7).

A comparison is computed on demand from run snapshots and trial records and is never
stored. A collection is a list of run references the team keeps up; its specification
(scenario, the dimensions that may vary, the values of the others) is fixed when it is
created, and only runs that match it get in. A run can join one when it is submitted:
the collection fills in its fixed fields like an experiment plan, and acceptance adds
the run in the same transaction. A snapshot freezes one comparison with its settings
and keeps its own copy of everything it shows.
"""

from __future__ import annotations

from sqlalchemy import Connection, delete, func, insert, select, update

from maestro.core import clock
from maestro.core.errors import FieldError, MaestroError, not_found
from maestro.core.ids import new_id
from maestro.db import schema as t
from maestro.db.engine import Database
from maestro.domain import limits
from maestro.domain.comparison import (
    DEFAULT_EXCLUDED,
    DIMENSIONS,
    GROUPS,
    SCENARIOS,
    SUBJECT_DIMENSIONS,
    VARIABLE_DIMENSIONS,
    ComparedRun,
    ComparedTrial,
    compare,
    dimensions_of,
    label,
    nonconforming,
)
from maestro.domain.scoring import contract_for, snapshot_contract
from maestro.domain.states import RUN_TERMINAL
from maestro.services import audit, deleted
from maestro.services.configuration import (
    Blocker,
    Resolution,
    Resolver,
    check_request,
    comparison_view,
    pinned_request,
    request_input,
)
from maestro.services.plans import Plans, merge
from maestro.services.principal import (
    Channel,
    Principal,
    can_manage_owned,
    require_experiment,
)

SCOPES = ("common", "all")
LAYOUTS = ("leaderboard", "matrix")

# Request fields that set each dimension: a collection's template leaves out the fields
# of the dimensions that may vary, so runs submitted from it choose those themselves.
REQUEST_FIELDS_OF = {
    "model": ("model_config_id", "model_revision"),
    "harness": ("harness_version_id", "harness_params"),
    "attachments": ("attachments",),
    "resources": ("resources",),
    "repetitions": ("repetitions",),
    "parameters": ("parameters",),
}
TIMEOUTS_OF = {"attachments": ("bootstrap_sec",), "resources": ("agent_sec", "verifier_sec")}
# Where a mismatch shows in a configuration request (for blockers and field errors).
REQUEST_PATH_OF = {
    "dataset": "/dataset_version_id",
    "scoring": "/dataset_version_id",
    "model": "/model_config_id",
    "harness": "/harness_version_id",
    "attachments": "/attachments",
    "resources": "/resources",
    "repetitions": "/repetitions",
    "parameters": "/parameters",
    "other": "/execution_mode",
}


def _invalid(code: str, message: str, errors: list[FieldError]) -> MaestroError:
    return MaestroError(code, message, 422, field_errors=errors)


def _check_text(body: dict, partial: bool, kind: str = "COLLECTION_INVALID") -> dict:
    found, errors = {}, []
    if "name" in body or not partial:
        name = body.get("name")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 128:
            errors.append(FieldError("/name", "LENGTH", "1-128 characters"))
        else:
            found["name"] = name.strip()
    if "description" in body or not partial:
        description = body.get("description") or ""
        if not isinstance(description, str) or len(description) > 1024:
            errors.append(FieldError("/description", "LENGTH", "at most 1024 characters"))
        else:
            found["description"] = description
    if errors:
        raise _invalid(kind, "Invalid collection", errors)
    return found


def _variable(body: dict) -> tuple[str, list[str]]:
    scenario = body.get("scenario")
    if scenario not in SCENARIOS:
        raise _invalid(
            "COLLECTION_INVALID",
            "Unknown scenario",
            [FieldError("/scenario", "ENUM", ", ".join(SCENARIOS))],
        )
    variable = SCENARIOS[scenario]
    if variable is None:
        chosen = body.get("variable_dimensions")
        if (
            not isinstance(chosen, list)
            or len(set(chosen)) != len(chosen)
            or not set(chosen) <= set(VARIABLE_DIMENSIONS)
        ):
            raise _invalid(
                "COLLECTION_INVALID",
                "Choose dimensions that may vary: the dataset and the scoring contract never do",
                [FieldError("/variable_dimensions", "ENUM", ", ".join(VARIABLE_DIMENSIONS))],
            )
        variable = tuple(d for d in VARIABLE_DIMENSIONS if d in chosen)
    return scenario, list(variable)


def default_settings(variable: list[str] | None = None) -> dict:
    layout = {"mode": "leaderboard", "rows": None, "columns": None, "sort": None}
    if variable is not None and len(variable) == 2:
        layout = {**layout, "mode": "matrix", "rows": variable[0], "columns": variable[1]}
    return {
        "layout": layout,
        "exclude": list(DEFAULT_EXCLUDED),
        "scope": "common",
        "baseline_run_id": None,
    }


def check_settings(raw, variable: list[str] | None = None, base: dict | None = None) -> dict:
    """Layout, excluded failure groups, task scope and baseline; unset keys keep
    ``base`` (or the defaults). A matrix needs two different dimensions; in a collection
    they must be ones that may vary."""
    settings = dict(base or default_settings(variable))
    if raw is None:
        return settings
    errors = []
    if not isinstance(raw, dict):
        raise _invalid("SETTINGS_INVALID", "Invalid settings", [FieldError("/settings", "TYPE")])
    if "exclude" in raw:
        exclude = raw["exclude"]
        if not isinstance(exclude, list) or not set(exclude) <= set(GROUPS):
            errors.append(FieldError("/settings/exclude", "ENUM", ", ".join(GROUPS)))
        else:
            settings["exclude"] = [g for g in GROUPS if g in exclude]
    if "scope" in raw:
        if raw["scope"] not in SCOPES:
            errors.append(FieldError("/settings/scope", "ENUM", ", ".join(SCOPES)))
        else:
            settings["scope"] = raw["scope"]
    if "baseline_run_id" in raw:
        if raw["baseline_run_id"] is not None and not isinstance(raw["baseline_run_id"], str):
            errors.append(FieldError("/settings/baseline_run_id", "TYPE"))
        else:
            settings["baseline_run_id"] = raw["baseline_run_id"]
    if "layout" in raw:
        layout = raw["layout"]
        allowed = variable if variable is not None else list(DIMENSIONS)
        if not isinstance(layout, dict) or layout.get("mode") not in LAYOUTS:
            errors.append(FieldError("/settings/layout/mode", "ENUM", ", ".join(LAYOUTS)))
        else:
            rows, columns = layout.get("rows"), layout.get("columns")
            if layout["mode"] == "matrix" and (
                rows not in allowed or columns not in allowed or rows == columns
            ):
                errors.append(
                    FieldError(
                        "/settings/layout", "MATRIX", "two different dimensions that may vary"
                    )
                )
            sort = layout.get("sort")
            if sort is not None and not (
                isinstance(sort, dict)
                and isinstance(sort.get("key"), str)
                and len(sort["key"]) <= 64
                and isinstance(sort.get("descending", False), bool)
            ):
                errors.append(FieldError("/settings/layout/sort", "TYPE"))
            settings["layout"] = {
                "mode": layout["mode"],
                "rows": rows if layout["mode"] == "matrix" else None,
                "columns": columns if layout["mode"] == "matrix" else None,
                "sort": {"key": sort["key"], "descending": bool(sort.get("descending"))}
                if sort
                else None,
            }
    if errors:
        raise _invalid("SETTINGS_INVALID", "Invalid comparison settings", errors)
    return settings


def _run_ids(raw, path: str = "/run_ids") -> list[str]:
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(x, str) for x in raw)
        or len(set(raw)) != len(raw)
    ):
        raise _invalid("COMPARISON_INVALID", "Choose distinct runs", [FieldError(path, "INVALID")])
    if len(raw) > limits.COMPARISON_MAX_RUNS:
        raise MaestroError(
            "COMPARISON_TOO_LARGE",
            f"At most {limits.COMPARISON_MAX_RUNS} runs are compared at once",
            413,
            field_errors=[FieldError(path, "TOO_MANY", limit=limits.COMPARISON_MAX_RUNS)],
        )
    return raw


def _experiment(principal: Principal) -> bool:
    return principal.channel != Channel.PAT or principal.pat_scope == "experiment"


# --- loading runs ------------------------------------------------------------------------


def _snapshots(conn: Connection, run_ids: list[str]) -> dict:
    rows = conn.execute(
        select(
            t.run_snapshots.c.run_id,
            t.run_snapshots.c.effective_json,
            t.run_snapshots.c.policy_json,
            t.run_snapshots.c.provenance_json,
        ).where(t.run_snapshots.c.run_id.in_(run_ids))
    ).all()
    return {r.run_id: r for r in rows}


def _trials(conn: Connection, run_ids: list[str]) -> dict[str, list[ComparedTrial]]:
    rows = conn.execute(
        select(
            t.trials.c.id,
            t.trials.c.run_id,
            t.trials.c.repetition_index,
            t.trials.c.state,
            t.trials.c.reason_code,
            t.tasks.c.task_key,
            t.trial_results.c.raw_score,
            t.trial_results.c.failure_cause,
            t.trial_results.c.exception_type,
            t.trial_results.c.usage_json,
            t.trial_results.c.actual_model_json,
        )
        .join(t.tasks, t.tasks.c.id == t.trials.c.task_id)
        .outerjoin(t.trial_results, t.trial_results.c.trial_id == t.trials.c.id)
        .where(t.trials.c.run_id.in_(run_ids))
        .order_by(t.tasks.c.task_key, t.trials.c.repetition_index)
    ).all()
    found: dict[str, list[ComparedTrial]] = {x: [] for x in run_ids}
    for row in rows:
        found[row.run_id].append(
            ComparedTrial(
                trial_id=row.id,
                task_key=row.task_key,
                repetition=row.repetition_index,
                state=row.state,
                raw_score=row.raw_score,
                # Trials ended without an executor result carry a platform reason only.
                failure_cause=row.failure_cause
                or (row.reason_code.lower() if row.reason_code else None),
                exception_type=row.exception_type,
                usage=row.usage_json,
                actual_model=row.actual_model_json,
            )
        )
    return found


def _evidence(conn: Connection, run_ids: list[str]) -> dict[str, str]:
    """Whether a run's original files can still be opened: deleted, purged, available
    (some artifact is), or none (expired, failed or never saved)."""
    available = dict(
        conn.execute(
            select(t.artifacts.c.run_id, func.count())
            .where(t.artifacts.c.run_id.in_(run_ids), t.artifacts.c.status == "available")
            .group_by(t.artifacts.c.run_id)
        ).all()
    )
    states = deleted.states(conn, "run", run_ids)
    found = {}
    for run_id in run_ids:
        state = states.get(run_id)
        if state:
            found[run_id] = "purged" if state["state"] == deleted.PURGED else "deleted"
        else:
            found[run_id] = "available" if available.get(run_id) else "none"
    return found


def _run_rows(conn: Connection, run_ids: list[str]) -> dict:
    rows = conn.execute(
        select(t.runs, t.users.c.username.label("owner_username"))
        .join(t.users, t.users.c.id == t.runs.c.owner_id)
        .where(t.runs.c.id.in_(run_ids))
    ).mappings()
    found = {r["id"]: r for r in rows}
    missing = [x for x in run_ids if x not in found]
    if missing:
        raise MaestroError(
            "NOT_FOUND",
            "Run not found or not visible",
            404,
            field_errors=[FieldError("/run_ids", "NOT_FOUND", x) for x in missing[:20]],
        )
    return found


def _compared(row, snapshot) -> ComparedRun:
    effective = snapshot.effective_json
    return ComparedRun(
        run_id=row["id"],
        view=comparison_view(effective),
        contract=snapshot_contract(snapshot.policy_json),
        terminal=row["state"] in RUN_TERMINAL,
        trials=(),
    )


def _refuse_oracle(snapshot, path: str) -> None:
    if snapshot.effective_json.get("execution_mode") == "oracle":
        raise MaestroError(
            "COMPARISON_ORACLE_RUN",
            "Oracle runs are a test mode and are never compared with model results",
            422,
            field_errors=[FieldError(path, "ORACLE")],
        )


def table(
    conn: Connection,
    run_ids: list[str],
    settings: dict,
    expected_variable=SUBJECT_DIMENSIONS,
    labels: dict | None = None,
) -> dict:
    """The comparison of these runs, in this order. Runs in the recycle bin (or purged)
    are set aside, not computed; they come back when restored."""
    rows = _run_rows(conn, run_ids)
    deletions = deleted.states(conn, "run", run_ids)
    snapshots = _snapshots(conn, run_ids)
    kept, set_aside = [], []
    for index, run_id in enumerate(run_ids):
        if deletions.get(run_id) or run_id not in snapshots:
            set_aside.append({"run_id": run_id, "name": rows[run_id]["name"], "reason": "deleted"})
            continue
        _refuse_oracle(snapshots[run_id], f"/run_ids/{index}")
        kept.append(run_id)
    trials = _trials(conn, kept)
    runs = []
    for run_id in kept:
        base = _compared(rows[run_id], snapshots[run_id])
        runs.append(
            ComparedRun(base.run_id, base.view, base.contract, base.terminal, tuple(trials[run_id]))
        )
    result = compare(
        runs,
        exclude=settings["exclude"],
        scope=settings["scope"],
        baseline=settings.get("baseline_run_id"),
        expected_variable=expected_variable,
    )
    evidence = _evidence(conn, kept)
    for row in result["runs"]:
        run = rows[row["run_id"]]
        row.update(
            {
                "name": run["name"],
                "label": (labels or {}).get(row["run_id"]),
                "owner_username": run["owner_username"],
                "state": run["state"],
                "relation": run["relation"],
                "source_run_id": run["source_run_id"],
                "batch_id": run["batch_id"],
                "accepted_at": run["accepted_at"],
                "finished_at": run["finished_at"],
                "evidence": evidence[row["run_id"]],
            }
        )
    result["set_aside"] = set_aside
    result["settings"]["layout"] = settings.get("layout") or default_settings()["layout"]
    return result


class Comparisons:
    def __init__(self, db: Database):
        self.db = db

    def compute(self, principal: Principal, body: dict) -> dict:
        """Any signed-in principal may compare: results are shared with the team."""
        run_ids = _run_ids(body.get("run_ids"))
        settings = check_settings(
            {k: body[k] for k in ("exclude", "scope", "baseline_run_id", "layout") if k in body}
        )
        with self.db.read() as conn:
            return table(conn, run_ids, settings)


# --- collections -------------------------------------------------------------------------


class Collections:
    def __init__(self, db: Database, resolver: Resolver | None = None):
        self.db = db
        self.resolver = resolver or Resolver()

    @staticmethod
    def _query():
        return select(t.collections, t.users.c.username.label("owner_username")).join(
            t.users, t.users.c.id == t.collections.c.owner_id
        )

    def _row(self, conn: Connection, collection_id: str):
        row = conn.execute(self._query().where(t.collections.c.id == collection_id))
        row = row.mappings().first()
        if row is None:
            raise not_found("Collection")
        return row

    def _live(self, conn: Connection, collection_id: str):
        row = self._row(conn, collection_id)
        state = deleted.state(conn, "collection", collection_id)
        if state:
            raise MaestroError(
                "COLLECTION_DELETED",
                "This collection was deleted",
                409 if state["state"] == deleted.RECYCLED else 410,
            )
        return row

    @staticmethod
    def _members(conn: Connection, collection_id: str) -> list:
        return (
            conn.execute(
                select(
                    t.collection_members,
                    t.runs.c.name.label("run_name"),
                    t.runs.c.state.label("run_state"),
                    t.users.c.username.label("added_by_username"),
                )
                .join(t.runs, t.runs.c.id == t.collection_members.c.run_id)
                .join(t.users, t.users.c.id == t.collection_members.c.added_by)
                .where(t.collection_members.c.collection_id == collection_id)
                .order_by(t.collection_members.c.added_at, t.collection_members.c.run_id)
            )
            .mappings()
            .all()
        )

    @staticmethod
    def _public(row, principal: Principal, count: int) -> dict:
        # The team keeps a collection up together; deleting it is the creator's.
        allowed = ["view", "compare"]
        if _experiment(principal):
            allowed += ["snapshot", "copy", "edit", "add_runs", "remove_runs", "submit"]
        if can_manage_owned(principal, row["owner_id"]):
            allowed.append("delete")
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "owner_id": row["owner_id"],
            "owner_username": row["owner_username"],
            "scenario": row["scenario"],
            "variable_dimensions": row["variable_json"],
            "fixed": row["fixed_json"],
            "fixed_labels": {d: label(d, v) for d, v in row["fixed_json"].items()},
            "template": request_input(row["template_json"]),
            "base_run_id": row["base_run_id"],
            "settings": row["settings_json"],
            "copied_from": row["copied_from"],
            "run_count": count,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "allowed_actions": allowed,
        }

    def list(self, principal: Principal) -> list[dict]:
        counts = (
            select(t.collection_members.c.collection_id, func.count().label("n"))
            .group_by(t.collection_members.c.collection_id)
            .subquery()
        )
        query = (
            self._query()
            .add_columns(func.coalesce(counts.c.n, 0).label("run_count"))
            .outerjoin(counts, counts.c.collection_id == t.collections.c.id)
            .where(deleted.visible(t.collections.c.id, "collection"))
            .order_by(t.collections.c.updated_at.desc(), t.collections.c.id)
        )
        with self.db.read() as conn:
            return [
                self._public(r, principal, r["run_count"]) for r in conn.execute(query).mappings()
            ]

    def get(self, principal: Principal, collection_id: str) -> dict:
        with self.db.read() as conn:
            row = self._row(conn, collection_id)
            deleted.refuse_purged(conn, "collection", collection_id, "Collection")
            members = self._members(conn, collection_id)
            run_deletions = deleted.states(conn, "run", [m["run_id"] for m in members])
            return {
                **self._public(row, principal, len(members)),
                "members": [
                    {
                        "run_id": m["run_id"],
                        "run_name": m["run_name"],
                        "run_state": m["run_state"],
                        "label": m["label"],
                        "added_by_username": m["added_by_username"],
                        "added_at": m["added_at"],
                        "deletion": run_deletions.get(m["run_id"]),
                    }
                    for m in members
                ],
                "deletion": deleted.state(conn, "collection", collection_id),
            }

    def compare(self, principal: Principal, collection_id: str, body: dict | None) -> dict:
        """The collection's comparison with its saved settings, or with settings given
        here for this view only."""
        with self.db.read() as conn:
            row = self._live(conn, collection_id)
            members = self._members(conn, collection_id)
            variable = row["variable_json"]
            settings = check_settings(body or None, variable, row["settings_json"])
            if not members:
                return {**compare([]), "set_aside": [], "settings": settings}
            return table(
                conn,
                [m["run_id"] for m in members],
                settings,
                expected_variable=variable,
                labels={m["run_id"]: m["label"] for m in members},
            )

    # --- the specification -----------------------------------------------------------

    def _base(self, conn: Connection, base, variable: list[str]) -> tuple[dict, dict, str | None]:
        """Fixed values and the request template from a run, or from a full request."""
        if not isinstance(base, dict) or not (("run_id" in base) ^ ("request" in base)):
            raise _invalid(
                "COLLECTION_INVALID",
                "Give the fixed values as a run or as a configuration request",
                [FieldError("/base", "REQUIRED", "run_id or request")],
            )
        if "run_id" in base:
            run_id = base["run_id"]
            row = conn.execute(select(t.runs).where(t.runs.c.id == run_id)).mappings().first()
            if row is None or deleted.state(conn, "run", run_id):
                raise MaestroError(
                    "NOT_FOUND",
                    "Run not found or deleted",
                    404,
                    field_errors=[FieldError("/base/run_id", "NOT_FOUND")],
                )
            snapshot = _snapshots(conn, [run_id])[run_id]
            _refuse_oracle(snapshot, "/base/run_id")
            effective = snapshot.effective_json
            contract = snapshot_contract(snapshot.policy_json)
            request = snapshot.provenance_json.get("request")
            if request is None:
                raise MaestroError(
                    "SOURCE_RUN_UNSUPPORTED", "This run predates reruns and cannot be a base", 422
                )
            request = pinned_request(request, effective)
        else:
            run_id = None
            if not isinstance(base["request"], dict):
                raise _invalid(
                    "COLLECTION_INVALID", "Invalid request", [FieldError("/base/request", "TYPE")]
                )
            request = check_request(Plans.apply(conn, base["request"]))
            res = self.resolver.resolve(conn, request)
            if res.effective is None or res.effective.get("model") is None:
                res.raise_if_blocked()
                raise MaestroError(
                    "COMPARISON_ORACLE_RUN",
                    "A collection compares model runs; Oracle is a test mode",
                    422,
                    field_errors=[FieldError("/base/request/execution_mode", "ORACLE")],
                )
            effective = res.effective
            contract = contract_for(res.version["manifest_json"])
            request = pinned_request(res.request, effective)
        dims = dimensions_of(comparison_view(effective), contract)
        fixed = {d: v for d, v in dims.items() if d not in variable}
        return fixed, template_of(request, variable), run_id

    def create(self, principal: Principal, body: dict) -> dict:
        require_experiment(principal)
        text = _check_text(body, partial=False)
        scenario, variable = _variable(body)
        settings = check_settings(body.get("settings"), variable)
        run_ids = body.get("run_ids") or []
        if run_ids:
            run_ids = _run_ids(run_ids)
        with self.db.write() as conn:
            fixed, template, base_run_id = self._base(conn, body.get("base"), variable)
            collection_id = self._insert(
                conn, principal, text, scenario, variable, fixed, template, base_run_id, settings
            )
            for index, run_id in enumerate(run_ids):
                self._add(conn, principal, collection_id, fixed, run_id, None, f"/run_ids/{index}")
        return self.get(principal, collection_id)

    def _insert(
        self,
        conn,
        principal,
        text,
        scenario,
        variable,
        fixed,
        template,
        base_run_id,
        settings,
        copied_from=None,
    ) -> str:
        collection_id = new_id("col")
        at = clock.to_text(clock.now())
        conn.execute(
            insert(t.collections).values(
                id=collection_id,
                name=text["name"],
                description=text["description"],
                owner_id=principal.user_id,
                pat_id=principal.pat_id,
                scenario=scenario,
                variable_json=variable,
                fixed_json=fixed,
                template_json=template,
                base_run_id=base_run_id,
                settings_json=settings,
                copied_from=copied_from,
                created_at=at,
                updated_at=at,
            )
        )
        _event(conn, principal, collection_id, "copy" if copied_from else "create", None, {})
        audit.record(
            conn,
            principal,
            "collection.copy" if copied_from else "collection.create",
            object_type="collection",
            object_id=collection_id,
            detail={"name": text["name"], "scenario": scenario, "variable": variable},
        )
        return collection_id

    # --- members -------------------------------------------------------------------

    def _add(self, conn, principal, collection_id, fixed, run_id, member_label, path) -> bool:
        """Add a run that matches the fixed dimensions; False when it already is in."""
        present = conn.execute(
            select(t.collection_members.c.run_id).where(
                t.collection_members.c.collection_id == collection_id,
                t.collection_members.c.run_id == run_id,
            )
        ).first()
        if present:
            return False
        count = conn.execute(
            select(func.count()).where(t.collection_members.c.collection_id == collection_id)
        ).scalar()
        if count >= limits.COMPARISON_MAX_RUNS:
            raise MaestroError(
                "COLLECTION_FULL",
                f"A collection holds at most {limits.COMPARISON_MAX_RUNS} runs",
                409,
                field_errors=[FieldError(path, "TOO_MANY", limit=limits.COMPARISON_MAX_RUNS)],
            )
        run = conn.execute(select(t.runs).where(t.runs.c.id == run_id)).mappings().first()
        if run is None or deleted.state(conn, "run", run_id):
            raise MaestroError(
                "NOT_FOUND",
                "Run not found or deleted",
                404,
                field_errors=[FieldError(path, "NOT_FOUND")],
            )
        snapshot = _snapshots(conn, [run_id])[run_id]
        _refuse_oracle(snapshot, path)
        dims = dimensions_of(
            comparison_view(snapshot.effective_json), snapshot_contract(snapshot.policy_json)
        )
        refuse_mismatch(nonconforming(fixed, dims), path)
        self._insert_member(conn, principal, collection_id, run_id, member_label)
        return True

    @staticmethod
    def _insert_member(conn, principal, collection_id, run_id, member_label, detail=None):
        at = clock.to_text(clock.now())
        conn.execute(
            insert(t.collection_members).values(
                collection_id=collection_id,
                run_id=run_id,
                label=member_label,
                added_by=principal.user_id,
                pat_id=principal.pat_id,
                added_at=at,
            )
        )
        _event(conn, principal, collection_id, "add", run_id, detail or {})
        _touch(conn, collection_id)

    def add_runs(self, principal: Principal, collection_id: str, body: dict) -> dict:
        require_experiment(principal)
        items = body.get("runs")
        if not isinstance(items, list) or not items:
            raise _invalid(
                "COLLECTION_INVALID", "Name runs to add", [FieldError("/runs", "REQUIRED")]
            )
        with self.db.write() as conn:
            row = self._live(conn, collection_id)
            for index, item in enumerate(items):
                path = f"/runs/{index}"
                if not isinstance(item, dict) or not isinstance(item.get("run_id"), str):
                    raise _invalid(
                        "COLLECTION_INVALID", "Invalid run", [FieldError(f"{path}/run_id", "TYPE")]
                    )
                member_label = _member_label(item.get("label"), f"{path}/label")
                self._add(
                    conn,
                    principal,
                    collection_id,
                    row["fixed_json"],
                    item["run_id"],
                    member_label,
                    f"{path}/run_id",
                )
        return self.get(principal, collection_id)

    def candidates(self, principal: Principal, collection_id: str) -> list[dict]:
        """Recent model runs not in the collection, each with where it differs from the
        fixed values (empty: it can be added), so a picker can offer them."""
        with self.db.read() as conn:
            row = self._live(conn, collection_id)
            members = select(t.collection_members.c.run_id).where(
                t.collection_members.c.collection_id == collection_id
            )
            runs = (
                conn.execute(
                    select(
                        t.runs.c.id,
                        t.runs.c.name,
                        t.runs.c.state,
                        t.runs.c.relation,
                        t.runs.c.accepted_at,
                        t.users.c.username.label("owner_username"),
                        t.model_configs.c.name.label("model_name"),
                        t.harness_versions.c.name.label("harness_name"),
                        t.harness_versions.c.version.label("harness_version"),
                    )
                    .join(t.users, t.users.c.id == t.runs.c.owner_id)
                    .join(t.model_configs, t.model_configs.c.id == t.runs.c.model_config_id)
                    .join(
                        t.harness_versions,
                        t.harness_versions.c.id == t.runs.c.harness_version_id,
                    )
                    .where(t.runs.c.id.not_in(members))
                    .order_by(t.runs.c.accepted_at.desc(), t.runs.c.id.desc())
                    .limit(limits.COLLECTION_CANDIDATES)
                )
                .mappings()
                .all()
            )
            ids = [r["id"] for r in runs]
            removed = deleted.states(conn, "run", ids)
            snapshots = _snapshots(conn, ids)
            found = []
            for run in runs:
                snapshot = snapshots.get(run["id"])
                if removed.get(run["id"]) or snapshot is None:
                    continue
                dims = dimensions_of(
                    comparison_view(snapshot.effective_json),
                    snapshot_contract(snapshot.policy_json),
                )
                found.append(
                    {
                        "run_id": run["id"],
                        "name": run["name"],
                        "state": run["state"],
                        "relation": run["relation"],
                        "owner_username": run["owner_username"],
                        "accepted_at": run["accepted_at"],
                        "model": run["model_name"],
                        "harness": f"{run['harness_name']} {run['harness_version']}",
                        "mismatches": nonconforming(row["fixed_json"], dims),
                    }
                )
            return found

    def remove_run(self, principal: Principal, collection_id: str, run_id: str) -> dict:
        require_experiment(principal)
        with self.db.write() as conn:
            self._live(conn, collection_id)
            removed = conn.execute(
                delete(t.collection_members).where(
                    t.collection_members.c.collection_id == collection_id,
                    t.collection_members.c.run_id == run_id,
                )
            ).rowcount
            if not removed:
                raise not_found("Collection member")
            _event(conn, principal, collection_id, "remove", run_id, {})
            _touch(conn, collection_id)
        return self.get(principal, collection_id)

    def label_run(self, principal: Principal, collection_id: str, run_id: str, body: dict):
        require_experiment(principal)
        member_label = _member_label(body.get("label"), "/label")
        with self.db.write() as conn:
            self._live(conn, collection_id)
            changed = conn.execute(
                update(t.collection_members)
                .where(
                    t.collection_members.c.collection_id == collection_id,
                    t.collection_members.c.run_id == run_id,
                )
                .values(label=member_label)
            ).rowcount
            if not changed:
                raise not_found("Collection member")
            _event(conn, principal, collection_id, "label", run_id, {"label": member_label})
        return self.get(principal, collection_id)

    def update(self, principal: Principal, collection_id: str, body: dict) -> dict:
        """Name, description and settings; any member may change them. The
        specification never changes: copy the collection to change it."""
        require_experiment(principal)
        text = _check_text(body, partial=True)
        with self.db.write() as conn:
            row = self._live(conn, collection_id)
            values = dict(text)
            if "settings" in body:
                values["settings_json"] = check_settings(
                    body["settings"], row["variable_json"], row["settings_json"]
                )
            if values:
                conn.execute(
                    update(t.collections)
                    .where(t.collections.c.id == collection_id)
                    .values(**values, updated_at=clock.to_text(clock.now()))
                )
                changed = sorted(k.removesuffix("_json") for k in values)
                _event(conn, principal, collection_id, "edit", None, {"changed": changed})
        return self.get(principal, collection_id)

    def copy(self, principal: Principal, collection_id: str, body: dict) -> dict:
        """A new collection with a new specification; runs of this one that match it are
        carried over, the others are listed with what differs."""
        require_experiment(principal)
        with self.db.write() as conn:
            row = self._row(conn, collection_id)
            deleted.refuse_purged(conn, "collection", collection_id, "Collection")
            text = _check_text(
                {
                    "name": body.get("name") or f"{row['name']} (copy)",
                    "description": body.get("description", row["description"]),
                },
                partial=False,
            )
            if "scenario" in body:
                scenario, variable = _variable(body)
            else:
                scenario, variable = row["scenario"], row["variable_json"]
            if body.get("base") is not None:
                fixed, template, base_run_id = self._base(conn, body["base"], variable)
            else:
                newly_fixed = [
                    d for d in DIMENSIONS if d not in variable and d not in row["fixed_json"]
                ]
                if newly_fixed:
                    raise MaestroError(
                        "COLLECTION_BASE_REQUIRED",
                        "Dimensions that varied are now fixed: give a run or a request to "
                        "take their values from",
                        422,
                        field_errors=[FieldError("/base", "REQUIRED", ", ".join(newly_fixed))],
                    )
                fixed = {d: v for d, v in row["fixed_json"].items() if d not in variable}
                template = template_of(row["template_json"], variable)
                base_run_id = row["base_run_id"]
            settings = check_settings(body.get("settings"), variable)
            new = self._insert(
                conn,
                principal,
                text,
                scenario,
                variable,
                fixed,
                template,
                base_run_id,
                settings,
                copied_from=collection_id,
            )
            members = self._members(conn, collection_id)
            run_deletions = deleted.states(conn, "run", [m["run_id"] for m in members])
            live = [m for m in members if not run_deletions.get(m["run_id"])]
            snapshots = _snapshots(conn, [m["run_id"] for m in live])
            not_carried = []
            for member in live:
                snapshot = snapshots[member["run_id"]]
                dims = dimensions_of(
                    comparison_view(snapshot.effective_json),
                    snapshot_contract(snapshot.policy_json),
                )
                found = nonconforming(fixed, dims)
                if found:
                    not_carried.append(
                        {
                            "run_id": member["run_id"],
                            "name": member["run_name"],
                            "mismatches": found,
                        }
                    )
                else:
                    self._insert_member(
                        conn,
                        principal,
                        new,
                        member["run_id"],
                        member["label"],
                        {"copied_from": collection_id},
                    )
        return {**self.get(principal, new), "not_carried": not_carried}

    def events(self, principal: Principal, collection_id: str) -> list[dict]:
        with self.db.read() as conn:
            self._row(conn, collection_id)
            rows = conn.execute(
                select(t.collection_events, t.users.c.username.label("actor_username"))
                .join(t.users, t.users.c.id == t.collection_events.c.actor_id)
                .where(t.collection_events.c.collection_id == collection_id)
                .order_by(t.collection_events.c.at.desc(), t.collection_events.c.id.desc())
            ).mappings()
            return [
                {
                    "id": r["id"],
                    "action": r["action"],
                    "run_id": r["run_id"],
                    "actor_username": r["actor_username"],
                    "pat_id": r["pat_id"],
                    "at": r["at"],
                    "detail": r["detail_json"],
                }
                for r in rows
            ]

    # --- submitting from a collection (04 section 7) --------------------------------------

    def apply(self, conn: Connection, body: dict) -> tuple[dict, dict | None]:
        """The request with the collection's fixed fields where the body has none, and
        which fields the collection supplied (``None`` without ``collection_id``)."""
        collection_id = body.get("collection_id")
        if collection_id is None:
            return body, None
        row = self._live(conn, collection_id)
        merged, supplied = merge(request_input(row["template_json"]), body)
        return merged, {"collection_id": collection_id, "fields": supplied, "row": row}

    def check(self, conn: Connection, collection_id: str, res: Resolution) -> list[Blocker]:
        """Blockers for a resolution that does not match the collection's fixed values."""
        row = self._live(conn, collection_id)
        if res.effective is None:
            return []
        if res.effective.get("execution_mode") == "oracle":
            return [
                Blocker(
                    "COLLECTION_MISMATCH",
                    "/execution_mode",
                    "Oracle runs cannot join a collection",
                    error="COLLECTION_MISMATCH",
                    status=409,
                )
            ]
        contract = contract_for(res.version["manifest_json"])
        dims = dimensions_of(comparison_view(res.effective), contract)
        return [
            Blocker(
                "COLLECTION_MISMATCH",
                REQUEST_PATH_OF[m["dimension"]],
                f"{m['dimension']} differs from the collection at {m['path']}",
                error="COLLECTION_MISMATCH",
                status=409,
            )
            for m in nonconforming(row["fixed_json"], dims)
        ]

    def join(self, conn: Connection, principal: Principal, collection_id: str, run_id: str):
        """Inside the acceptance transaction: the new run joins the collection."""
        self._live(conn, collection_id)
        count = conn.execute(
            select(func.count()).where(t.collection_members.c.collection_id == collection_id)
        ).scalar()
        if count >= limits.COMPARISON_MAX_RUNS:
            raise MaestroError(
                "COLLECTION_FULL",
                f"A collection holds at most {limits.COMPARISON_MAX_RUNS} runs",
                409,
                field_errors=[FieldError("/collection_id", "TOO_MANY")],
            )
        self._insert_member(conn, principal, collection_id, run_id, None, {"submitted": True})


def template_of(request: dict, variable: list[str]) -> dict:
    """A request without the fields of the dimensions that may vary, the task choice or
    service-set markers; nested values that are unset are left out."""
    drop = {f for d in variable for f in REQUEST_FIELDS_OF.get(d, ())}
    drop |= {"task_selection", "plan", "plan_id", "plan_revision"}
    template = {}
    for key, value in request_input(request).items():
        if key in drop or value is None:
            continue
        if key == "timeouts":
            gone = {x for d in variable for x in TIMEOUTS_OF.get(d, ())}
            value = {k: v for k, v in value.items() if k not in gone and v is not None}
        elif isinstance(value, dict):
            value = {k: v for k, v in value.items() if v is not None}
        if value == {} and key in ("resources", "timeouts"):
            continue
        template[key] = value
    return template


def refuse_mismatch(found: list[dict], path: str) -> None:
    if found:
        raise MaestroError(
            "COLLECTION_MISMATCH",
            "The run differs from the collection's fixed values: "
            + ", ".join(sorted({m["dimension"] for m in found})),
            409,
            field_errors=[
                FieldError(path, "DIFFERS", f"{m['dimension']}: {m['path']}") for m in found[:50]
            ],
        )


def mark_sources(res: Resolution, applied: dict) -> None:
    """Values the collection filled in come from the collection layer."""
    source = {"layer": "collection", "source_id": applied["collection_id"]}
    renamed = {
        "/model_config_id": "/model",
        "/model_revision": "/model",
        "/harness_version_id": "/harness",
    }
    for pointer in applied["fields"]:
        path = renamed.get(pointer) or pointer.replace("/harness_params/", "/harness/params/")
        if res.field_sources.get(path, {}).get("layer") == "override":
            res.field_sources[path] = dict(source)
    res.references["collection"] = {"id": applied["collection_id"]}


def _member_label(value, path: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value.strip()) > 128:
        raise _invalid("COLLECTION_INVALID", "Invalid label", [FieldError(path, "LENGTH")])
    return value.strip() or None


def _event(conn, principal: Principal, collection_id, action, run_id, detail: dict) -> None:
    conn.execute(
        insert(t.collection_events).values(
            id=new_id("cev"),
            collection_id=collection_id,
            action=action,
            run_id=run_id,
            actor_id=principal.user_id,
            pat_id=principal.pat_id,
            at=clock.to_text(clock.now()),
            detail_json=detail,
        )
    )


def _touch(conn, collection_id: str) -> None:
    conn.execute(
        update(t.collections)
        .where(t.collections.c.id == collection_id)
        .values(updated_at=clock.to_text(clock.now()))
    )


def drop_purged_run(conn: Connection, run_id: str, actor_id: str) -> None:
    """A purged run leaves every collection it was in; the change is recorded."""
    collections = (
        conn.execute(
            select(t.collection_members.c.collection_id).where(
                t.collection_members.c.run_id == run_id
            )
        )
        .scalars()
        .all()
    )
    conn.execute(delete(t.collection_members).where(t.collection_members.c.run_id == run_id))
    at = clock.to_text(clock.now())
    for collection_id in collections:
        conn.execute(
            insert(t.collection_events).values(
                id=new_id("cev"),
                collection_id=collection_id,
                action="purged",
                run_id=run_id,
                actor_id=actor_id,
                pat_id=None,
                at=at,
                detail_json={},
            )
        )


# --- analysis snapshots ------------------------------------------------------------------


class Snapshots:
    def __init__(self, db: Database, collections: Collections):
        self.db = db
        self.collections = collections

    def create(self, principal: Principal, body: dict) -> dict:
        """Freeze a comparison of ended runs: a collection's (with its settings unless
        others are given) or of runs named here."""
        require_experiment(principal)
        name = body.get("name")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 256:
            raise _invalid("SNAPSHOT_INVALID", "Invalid snapshot", [FieldError("/name", "LENGTH")])
        note = body.get("note")
        if note is not None and (not isinstance(note, str) or len(note) > NOTE_MAX):
            raise _invalid("SNAPSHOT_INVALID", "Invalid note", [FieldError("/note", "LENGTH")])
        collection_id = body.get("collection_id")
        with self.db.write() as conn:
            if collection_id is not None:
                row = self.collections._live(conn, collection_id)
                members = self.collections._members(conn, collection_id)
                if not members:
                    raise MaestroError("SNAPSHOT_EMPTY", "The collection has no runs", 409)
                run_ids = [m["run_id"] for m in members]
                labels = {m["run_id"]: m["label"] for m in members}
                variable = row["variable_json"]
                settings = check_settings(body.get("settings"), variable, row["settings_json"])
                spec = {
                    "scenario": row["scenario"],
                    "variable_dimensions": variable,
                    "fixed": row["fixed_json"],
                }
            else:
                run_ids = _run_ids(body.get("run_ids"))
                labels, variable, spec = {}, SUBJECT_DIMENSIONS, None
                settings = check_settings(body.get("settings"))
            result = table(conn, run_ids, settings, variable, labels)
            active = [r["run_id"] for r in result["runs"] if not r["terminal"]]
            if active:
                raise MaestroError(
                    "SNAPSHOT_RUNS_ACTIVE",
                    "Snapshots freeze ended runs only; wait for these to end",
                    409,
                    field_errors=[FieldError("/run_ids", "ACTIVE", x) for x in active[:20]],
                )
            snapshot_id = new_id("snp")
            at = clock.to_text(clock.now())
            conn.execute(
                insert(t.analysis_snapshots).values(
                    id=snapshot_id,
                    name=name.strip(),
                    collection_id=collection_id,
                    owner_id=principal.user_id,
                    pat_id=principal.pat_id,
                    runs_json=[
                        {"run_id": r["run_id"], "name": r["name"], "label": r["label"]}
                        for r in result["runs"]
                    ],
                    settings_json=settings,
                    spec_json=spec,
                    result_json=result,
                    created_at=at,
                )
            )
            if note:
                self._insert_note(conn, principal, snapshot_id, note)
            audit.record(
                conn,
                principal,
                "snapshot.create",
                object_type="analysis_snapshot",
                object_id=snapshot_id,
                detail={"runs": len(result["runs"]), "collection_id": collection_id},
            )
        return self.get(principal, snapshot_id)

    @staticmethod
    def _query():
        return select(t.analysis_snapshots, t.users.c.username.label("owner_username")).join(
            t.users, t.users.c.id == t.analysis_snapshots.c.owner_id
        )

    @staticmethod
    def _summary(row, principal: Principal) -> dict:
        allowed = ["view"]
        if _experiment(principal):
            allowed.append("note")
        if can_manage_owned(principal, row["owner_id"]):
            allowed.append("delete")
        return {
            "id": row["id"],
            "name": row["name"],
            "collection_id": row["collection_id"],
            "owner_id": row["owner_id"],
            "owner_username": row["owner_username"],
            "pat_id": row["pat_id"],
            "run_count": len(row["runs_json"] or []),
            "created_at": row["created_at"],
            "allowed_actions": allowed,
        }

    def list(self, principal: Principal, collection_id: str | None = None) -> list[dict]:
        query = self._query().where(deleted.visible(t.analysis_snapshots.c.id, "analysis_snapshot"))
        if collection_id is not None:
            query = query.where(t.analysis_snapshots.c.collection_id == collection_id)
        query = query.order_by(
            t.analysis_snapshots.c.created_at.desc(), t.analysis_snapshots.c.id.desc()
        )
        with self.db.read() as conn:
            return [self._summary(r, principal) for r in conn.execute(query).mappings()]

    def get(self, principal: Principal, snapshot_id: str) -> dict:
        with self.db.read() as conn:
            row = conn.execute(self._query().where(t.analysis_snapshots.c.id == snapshot_id))
            row = row.mappings().first()
            if row is None:
                raise not_found("Analysis snapshot")
            deleted.refuse_purged(conn, "analysis_snapshot", snapshot_id, "Analysis snapshot")
            run_ids = [r["run_id"] for r in row["runs_json"]]
            # The numbers are frozen; whether the original files still open is not.
            evidence = _evidence(conn, run_ids) if run_ids else {}
            notes = conn.execute(
                select(t.snapshot_notes, t.users.c.username.label("author_username"))
                .join(t.users, t.users.c.id == t.snapshot_notes.c.author_id)
                .where(t.snapshot_notes.c.snapshot_id == snapshot_id)
                .order_by(t.snapshot_notes.c.created_at, t.snapshot_notes.c.id)
            ).mappings()
            return {
                **self._summary(row, principal),
                "settings": row["settings_json"],
                "spec": row["spec_json"],
                "result": row["result_json"],
                "evidence": evidence,
                "notes": [
                    {
                        "id": n["id"],
                        "author_id": n["author_id"],
                        "author_username": n["author_username"],
                        "pat_id": n["pat_id"],
                        "body": n["body"],
                        "created_at": n["created_at"],
                        "updated_at": n["updated_at"],
                        "editable": n["author_id"] == principal.user_id and _experiment(principal),
                    }
                    for n in notes
                ],
                "deletion": deleted.state(conn, "analysis_snapshot", snapshot_id),
            }

    @staticmethod
    def _insert_note(conn, principal: Principal, snapshot_id: str, body: str) -> None:
        at = clock.to_text(clock.now())
        conn.execute(
            insert(t.snapshot_notes).values(
                id=new_id("snn"),
                snapshot_id=snapshot_id,
                author_id=principal.user_id,
                pat_id=principal.pat_id,
                body=body,
                created_at=at,
                updated_at=at,
            )
        )

    def add_note(self, principal: Principal, snapshot_id: str, body: dict) -> dict:
        require_experiment(principal)
        text = _note(body)
        with self.db.write() as conn:
            found = conn.execute(
                select(t.analysis_snapshots.c.id).where(t.analysis_snapshots.c.id == snapshot_id)
            ).first()
            if found is None:
                raise not_found("Analysis snapshot")
            if deleted.state(conn, "analysis_snapshot", snapshot_id):
                raise MaestroError("SNAPSHOT_DELETED", "This snapshot was deleted", 409)
            self._insert_note(conn, principal, snapshot_id, text)
        return self.get(principal, snapshot_id)

    def update_note(self, principal: Principal, snapshot_id: str, note_id: str, body: dict):
        """Only the author edits a note: it is their conclusion, not the platform's."""
        require_experiment(principal)
        text = _note(body)
        with self.db.write() as conn:
            note = (
                conn.execute(
                    select(t.snapshot_notes).where(
                        t.snapshot_notes.c.id == note_id,
                        t.snapshot_notes.c.snapshot_id == snapshot_id,
                    )
                )
                .mappings()
                .first()
            )
            if note is None:
                raise not_found("Note")
            if note["author_id"] != principal.user_id:
                raise MaestroError("FORBIDDEN", "Only the author edits a note", 403)
            conn.execute(
                update(t.snapshot_notes)
                .where(t.snapshot_notes.c.id == note_id)
                .values(body=text, updated_at=clock.to_text(clock.now()))
            )
        return self.get(principal, snapshot_id)


NOTE_MAX = 20000


def _note(body: dict) -> str:
    text = body.get("body")
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= NOTE_MAX:
        raise _invalid(
            "SNAPSHOT_INVALID", "Invalid note", [FieldError("/body", "LENGTH", f"1-{NOTE_MAX}")]
        )
    return text
