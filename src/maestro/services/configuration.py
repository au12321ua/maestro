"""Configuration resolution (03 sections 3-5, CFG-01..06).

One resolver serves the resolution endpoint and run acceptance, so the web page and
the SDK see the same effective configuration, field sources and blockers. Layers, in
order: author (task files) -> team policy defaults -> experiment plan (later) -> this
request's overrides; then author constraints, team hard limits and type checks. Values
are never clamped: a value over a limit is a blocker, not a silent change.

The resolution describes; it reserves nothing. Cache observations and the restore plan
are reported next to the configuration but stay out of its digest (03 section 4).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from sqlalchemy import Connection, select

from maestro.core.digests import digest_json
from maestro.core.errors import FieldError, MaestroError
from maestro.db import schema as t
from maestro.execution.agents import param_problem
from maestro.execution.materials import DEFAULT_CPUS, DEFAULT_MEMORY_MB, RESERVED_PARAMETER
from maestro.services import deleted, policy
from maestro.services.capacity import never_fits, reservation
from maestro.services.catalog import (
    expected_image,
    load_admissions,
    load_availability,
    task_blockers,
)
from maestro.services.credentials import credential_problem
from maestro.services.model_configs import MODEL_KINDS

EXECUTION_MODES = ("agent", "oracle")
PARAMETER_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
PARAMETER_VALUE_MAX = 4096
# Secret author parameters take generic secrets only: a model API key goes to the
# platform's own model calls, never into a task container (U-28).
PARAMETER_KINDS = ("generic_secret",)


def _validation(errors: list[FieldError]) -> MaestroError:
    return MaestroError(
        "CONFIG_VALIDATION_FAILED",
        "Invalid run configuration",
        422,
        stage="preflight",
        field_errors=errors,
    )


def _number(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, int | float) and math.isfinite(value)


# The fields of a configuration request; an experiment plan holds any of them.
REQUEST_FIELDS = (
    "dataset_version_id",
    "task_selection",
    "execution_mode",
    "model_config_id",
    "model_revision",
    "harness_version_id",
    "harness_params",
    "attachments",
    "repetitions",
    "concurrency",
    "run_deadline_hours",
    "resources",
    "timeouts",
    "parameters",
)
NESTED_FIELDS = ("resources", "timeouts", "parameters", "harness_params")


def check_template(template) -> dict:
    """A partial request (an experiment plan): the same checks as a request, on the
    fields it has; nothing is required."""
    if not isinstance(template, dict):
        raise _validation([FieldError("/template", "TYPE", "an object")])
    unknown = sorted(set(template) - set(REQUEST_FIELDS))
    if unknown:
        raise _validation([FieldError(f"/template/{k}", "UNKNOWN_FIELD") for k in unknown])
    template = {k: v for k, v in template.items() if v is not None}
    agent = any(
        k in template
        for k in (
            "model_config_id",
            "model_revision",
            "harness_version_id",
            "harness_params",
            "attachments",
        )
    )
    probe = {
        "dataset_version_id": "-",
        "execution_mode": "agent" if agent else "oracle",
        **template,
    }
    if probe["execution_mode"] == "agent":
        probe.setdefault("model_config_id", "-")
        probe.setdefault("harness_version_id", "-")
    try:
        checked = check_request(probe)
    except MaestroError as error:
        for found in error.field_errors:
            found.path = "/template" + found.path
        raise
    found = {}
    for key, value in template.items():
        if key in NESTED_FIELDS:
            found[key] = {k: v for k, v in checked[key].items() if k in value}
        else:
            found[key] = checked[key]
    if "parameters" in found:
        # Kept in request form (a string, or a credential reference): a run request
        # merges them in and is checked again, and check_request takes no {"value": ...}.
        found["parameters"] = {k: v.get("value", v) for k, v in found["parameters"].items()}
    return found


def check_request(body: dict) -> dict:
    """Shape and type checks; limits are blockers of the resolution. Absent and null
    both mean "not set here": the field is inherited from the layer below (03 rule 1)."""
    errors: list[FieldError] = []
    version_id = body.get("dataset_version_id")
    if not isinstance(version_id, str):
        errors.append(FieldError("/dataset_version_id", "REQUIRED"))
    selection = body.get("task_selection") or {"mode": "all_eligible"}
    mode = selection.get("mode") if isinstance(selection, dict) else None
    task_ids = selection.get("task_ids") if isinstance(selection, dict) else None
    if mode not in {"all_eligible", "explicit"}:
        errors.append(FieldError("/task_selection/mode", "ENUM"))
    elif (mode == "explicit" or task_ids is not None) and (
        not isinstance(task_ids, list)
        or not task_ids
        or not all(isinstance(x, str) for x in task_ids)
        or len(set(task_ids)) != len(task_ids)
    ):
        errors.append(FieldError("/task_selection/task_ids", "INVALID"))
    if mode != "explicit":
        task_ids = None
    execution_mode = body.get("execution_mode")
    if execution_mode not in EXECUTION_MODES:
        errors.append(FieldError("/execution_mode", "ENUM", "agent or oracle"))
    model_id = body.get("model_config_id")
    model_revision = body.get("model_revision")
    if model_revision is not None and (type(model_revision) is not int or model_revision < 1):
        errors.append(FieldError("/model_revision", "TYPE", "a positive integer"))
    harness_id = body.get("harness_version_id")
    harness_params = body.get("harness_params") or {}
    if execution_mode == "agent":
        if not isinstance(model_id, str):
            errors.append(FieldError("/model_config_id", "REQUIRED"))
        if not isinstance(harness_id, str):
            errors.append(FieldError("/harness_version_id", "REQUIRED"))
    elif execution_mode == "oracle" and (model_id or harness_id or harness_params):
        # Oracle runs the task's reference solution; it never calls a model (15 section 6).
        errors.append(FieldError("/execution_mode", "ORACLE_HAS_NO_MODEL"))
    attachments = body.get("attachments") or []
    if (
        not isinstance(attachments, list)
        or not all(isinstance(x, str) for x in attachments)
        or len(set(attachments)) != len(attachments)
    ):
        errors.append(FieldError("/attachments", "INVALID", "distinct attachment version ids"))
        attachments = []
    elif attachments and execution_mode == "oracle":
        # Attachments are installed by the harness wrapper around a model agent (03 section 7).
        errors.append(FieldError("/attachments", "ORACLE_HAS_NO_ATTACHMENTS"))
    if not isinstance(harness_params, dict):
        errors.append(FieldError("/harness_params", "TYPE"))
        harness_params = {}
    for name in ("repetitions", "concurrency", "run_deadline_hours"):
        value = body.get(name)
        if value is not None and (type(value) is not int or value < 1):
            errors.append(FieldError(f"/{name}", "TYPE", "a positive integer"))
    resources = body.get("resources") or {}
    timeouts = body.get("timeouts") or {}
    if not isinstance(resources, dict) or not isinstance(timeouts, dict):
        errors.append(FieldError("/resources", "TYPE"))
        resources, timeouts = {}, {}
    cpus, memory = resources.get("cpus"), resources.get("memory_mb")
    # Whole CPUs: the Harbor environment takes an integer and nothing is rounded silently.
    if cpus is not None and (type(cpus) is not int or cpus < 1):
        errors.append(FieldError("/resources/cpus", "TYPE", "a positive integer"))
    if memory is not None and (type(memory) is not int or memory < 1):
        errors.append(FieldError("/resources/memory_mb", "TYPE", "a positive integer"))
    for name in ("agent_sec", "verifier_sec", "bootstrap_sec"):
        value = timeouts.get(name)
        if value is not None and (not _number(value) or value <= 0):
            errors.append(FieldError(f"/timeouts/{name}", "TYPE", "a positive number"))
    parameters = body.get("parameters") or {}
    if not isinstance(parameters, dict):
        errors.append(FieldError("/parameters", "TYPE"))
        parameters = {}
    clean_parameters = {}
    for name, value in parameters.items():
        path = f"/parameters/{name}"
        if not PARAMETER_NAME.fullmatch(name):
            errors.append(FieldError(path, "PATTERN"))
        elif isinstance(value, str) and len(value) <= PARAMETER_VALUE_MAX and "\0" not in value:
            clean_parameters[name] = {"value": value}
        elif (
            isinstance(value, dict)
            and set(value) == {"credential_id"}
            and isinstance(value["credential_id"], str)
        ):
            clean_parameters[name] = {"credential_id": value["credential_id"]}
        else:
            errors.append(
                FieldError(path, "TYPE", "a string, or {'credential_id': ...} for secrets")
            )
    if errors:
        raise _validation(errors)
    plan = body.get("plan")
    return {
        # Set by the service when an experiment plan filled fields in (never by a client).
        **({"plan": plan} if isinstance(plan, dict) else {}),
        "dataset_version_id": version_id,
        "task_selection": {"mode": mode, **({"task_ids": sorted(task_ids)} if task_ids else {})},
        "execution_mode": execution_mode,
        "model_config_id": model_id if execution_mode == "agent" else None,
        # Pins an earlier revision (reruns use the source's); unset means the current one.
        "model_revision": model_revision if execution_mode == "agent" else None,
        "harness_version_id": harness_id if execution_mode == "agent" else None,
        "harness_params": dict(sorted(harness_params.items())),
        # In load order: the order is part of the configuration (03 section 7).
        "attachments": attachments,
        "repetitions": body.get("repetitions"),
        "concurrency": body.get("concurrency"),
        "run_deadline_hours": body.get("run_deadline_hours"),
        "resources": {"cpus": cpus, "memory_mb": memory},
        "timeouts": {k: timeouts.get(k) for k in ("agent_sec", "verifier_sec", "bootstrap_sec")},
        "parameters": dict(sorted(clean_parameters.items())),
    }


@dataclass
class Blocker:
    code: str
    path: str
    message: str
    error: str = "CONFIG_VALIDATION_FAILED"
    status: int = 422
    extra: dict = field(default_factory=dict)
    field_errors: list[FieldError] | None = None

    def public(self) -> dict:
        return {"code": self.code, "path": self.path, "message": self.message, **self.extra}

    def errors(self) -> list[FieldError]:
        if self.field_errors is not None:
            return self.field_errors
        return [FieldError(self.path, self.code, self.message, limit=self.extra.get("limit"))]


@dataclass
class Resolution:
    request: dict
    effective: dict | None = None
    field_sources: dict = field(default_factory=dict)
    baseline_differences: list = field(default_factory=list)
    blockers: list[Blocker] = field(default_factory=list)
    warnings: list = field(default_factory=list)
    excluded: list = field(default_factory=list)
    cache_observations: list = field(default_factory=list)
    required_preparation: list = field(default_factory=list)
    references: dict = field(default_factory=dict)
    tasks: list = field(default_factory=list)
    version: dict | None = None
    reservation: dict | None = None
    credential_ids: list = field(default_factory=list)
    labels: dict = field(default_factory=dict)

    @property
    def config_digest(self) -> str | None:
        return digest_json(self.effective) if self.effective is not None else None

    def raise_if_blocked(self) -> None:
        if not self.blockers:
            return
        first = self.blockers[0]
        same = [b for b in self.blockers if b.error == first.error]
        raise MaestroError(
            first.error,
            first.message,
            first.status,
            stage="preflight",
            field_errors=[e for b in same for e in b.errors()],
        )

    def public(self) -> dict:
        return {
            "effective_config": self.effective,
            "field_sources": self.field_sources,
            "baseline_differences": self.baseline_differences,
            "excluded_tasks": self.excluded,
            "required_preparation": self.required_preparation,
            "cache_observations": self.cache_observations,
            "warnings": self.warnings,
            "blockers": [b.public() for b in self.blockers],
            "config_digest": self.config_digest,
            "reference_revisions": self.references,
            "labels": self.labels,
            "planned_trials": (
                len(self.tasks) * self.effective["repetitions"] if self.effective else 0
            ),
        }


def comparison_view(effective: dict) -> dict:
    """What decides whether two runs are comparable (03 section 5): content, preparation
    method, model and harness content, resources, metric and repetitions. Run identity,
    references to rows, credentials, task subsets and scheduling are left out."""
    view = {
        k: v
        for k, v in effective.items()
        if k not in {"tasks", "concurrency", "run_deadline_hours", "parameters"}
    }
    view["dataset"] = {k: v for k, v in effective["dataset"].items() if k != "version_id"}
    if effective.get("model"):
        view["model"] = {
            k: v
            for k, v in effective["model"].items()
            if k not in {"config_id", "revision", "credential_id"}
        }
    if effective.get("harness"):
        view["harness"] = {k: v for k, v in effective["harness"].items() if k != "version_id"}
    view["attachments"] = [
        {k: v for k, v in a.items() if k != "version_id"}
        for a in effective.get("attachments") or []
    ]
    # Parameter values are compared; which credential holds a secret is not.
    view["parameters"] = {
        name: value.get("value", "<secret>")
        for name, value in (effective.get("parameters") or {}).items()
    }
    return view


def differences(old: dict | None, new: dict | None, path: str = "") -> list[dict]:
    """JSON-pointer differences between two effective configurations."""
    if old == new:
        return []
    if isinstance(old, dict) and isinstance(new, dict):
        found = []
        for key in sorted(set(old) | set(new)):
            found.extend(differences(old.get(key), new.get(key), f"{path}/{key}"))
        return found
    if (
        isinstance(old, list)
        and isinstance(new, list)
        and len(old) == len(new)
        and all(isinstance(x, dict) and "task_key" in x for x in old + new)
    ):
        found = []
        for a, b in zip(old, new, strict=True):
            found.extend(differences(a, b, f"{path}/{a['task_key']}"))
        return found
    return [{"path": path or "/", "before": old, "after": new}]


def hide_model_credential(data):
    """A copy without the model's bound credential (U-27): members see that a model has a
    usable key, not which one. Digests are computed before, over the full configuration;
    credential references the member chose for author parameters stay."""
    found: set[str] = set()

    def collect(value) -> None:
        if isinstance(value, list):
            for x in value:
                collect(x)
        elif isinstance(value, dict):
            model = value.get("model")
            if isinstance(model, dict) and "config_id" in model and model.get("credential_id"):
                found.add(model["credential_id"])
            for x in value.values():
                collect(x)

    def scrub(value):
        if isinstance(value, list):
            return [scrub(x) for x in value]
        if not isinstance(value, dict):
            return value
        if value.get("path") == "/model/credential_id" and {"before", "after"} <= set(value):
            return {**value, "before": None, "after": None, "hidden": True}
        copy = {k: scrub(v) for k, v in value.items()}
        model = copy.get("model")
        if isinstance(model, dict) and "config_id" in model:
            copy["model"] = {k: v for k, v in model.items() if k != "credential_id"}
        if isinstance(copy.get("credential_ids"), list):
            copy["credential_ids"] = [x for x in copy["credential_ids"] if x not in found]
        return copy

    collect(data)
    return scrub(data)


def pinned_request(request: dict, effective: dict) -> dict:
    """The request that resolves to ``effective`` again (03 section 5, CFG-06): fields the
    team policy filled in are pinned to the values the run used, and so is the model
    revision, so later team defaults or model revisions change nothing. Author values
    stay inherited: they come from the same immutable dataset version."""
    pinned = {
        **request,
        "resources": dict(request["resources"]),
        "timeouts": dict(request["timeouts"]),
        "parameters": dict(request["parameters"]),
        "harness_params": dict(request["harness_params"]),
        "attachments": list(request.get("attachments") or []),
    }
    for name in ("repetitions", "concurrency", "run_deadline_hours"):
        pinned[name] = effective[name]
    if effective.get("bootstrap_timeout_sec") is not None:
        pinned["timeouts"]["bootstrap_sec"] = effective["bootstrap_timeout_sec"]
    if effective.get("model"):
        pinned["model_revision"] = effective["model"]["revision"]
    return pinned


class Resolver:
    def __init__(
        self,
        capacity=None,
        adapters_of=None,
        executor_id=None,
        network_of=None,
        model_params_of=None,
    ):
        self.capacity = capacity
        # Callable giving the executor's adapters (name -> protocols); None: no executor.
        self.adapters_of = adapters_of or (lambda: None)
        # Callable giving the model parameters each adapter passes on (name -> names);
        # an adapter not listed passes on any.
        self.model_params_of = model_params_of or (dict)
        self.executor_id = executor_id
        # Callable: can this host restrict task networks? True, False, or None (unknown).
        self.network_of = network_of or (lambda: None)

    def resolve(self, conn: Connection, request: dict) -> Resolution:
        res = Resolution(request=request)
        version = self._version(conn, request["dataset_version_id"], res)
        if version is None:
            return res
        team = policy.current(conn)
        res.references["team_policy"] = team.revision
        effective = {
            "dataset": {
                "source_kind": version["source_kind"],
                "namespace": version["namespace"],
                "name": version["name"],
                "version_id": version["id"],
                "resolved_digest": version["resolved_digest"],
            },
            "execution_mode": request["execution_mode"],
        }
        self._run_fields(team, request, effective, res)
        self._agent(conn, request, effective, res)
        self._attachments(conn, team, request, effective, res)
        self._tasks(conn, team, request, version, effective, res)
        if res.tasks:
            self._size_and_capacity(conn, team, effective, res)
        res.effective = effective
        if request.get("plan"):
            self._plan_sources(request["plan"], res)
        return res

    @staticmethod
    def _plan_sources(plan: dict, res: Resolution) -> None:
        """Values the plan filled in come from the plan layer, not this request."""
        res.references["plan"] = {"id": plan["plan_id"], "revision": plan["revision"]}
        source = {"layer": "plan", "source_id": plan["plan_id"], "revision": plan["revision"]}
        renamed = {
            "/model_config_id": "/model",
            "/model_revision": "/model",
            "/harness_version_id": "/harness",
        }
        for pointer in plan["fields"]:
            path = renamed.get(pointer) or pointer.replace("/harness_params/", "/harness/params/")
            if res.field_sources.get(path, {}).get("layer") == "override":
                res.field_sources[path] = dict(source)

    # --- dataset ------------------------------------------------------------------

    def _version(self, conn: Connection, version_id: str, res: Resolution):
        version = (
            conn.execute(
                select(
                    t.dataset_versions,
                    t.datasets.c.namespace,
                    t.datasets.c.name,
                    t.datasets.c.source_kind,
                )
                .join(t.datasets, t.datasets.c.id == t.dataset_versions.c.dataset_id)
                .where(t.dataset_versions.c.id == version_id)
            )
            .mappings()
            .first()
        )
        if version is None:
            res.blockers.append(
                Blocker("NOT_FOUND", "/dataset_version_id", "Dataset version not found")
            )
            return None
        if deleted.version_state(conn, version_id):
            res.blockers.append(
                Blocker(
                    "DELETED",
                    "/dataset_version_id",
                    "This dataset version was deleted",
                    error="DATASET_VERSION_DELETED",
                )
            )
            return None
        res.version = dict(version)
        return version

    # --- run-level fields ---------------------------------------------------------

    def _run_fields(self, team, request, effective, res: Resolution) -> None:
        for name, default, limit in (
            ("repetitions", "repetitions_default", "repetitions_max"),
            ("concurrency", "run_default_concurrency", "run_max_concurrency"),
            ("run_deadline_hours", "run_deadline_hours", "run_deadline_hours_max"),
        ):
            value = request[name]
            if value is None:
                value = getattr(team, default)
                res.field_sources[f"/{name}"] = {
                    "layer": "team",
                    "source_id": "team_policy",
                    "revision": team.revision,
                }
            else:
                res.field_sources[f"/{name}"] = {"layer": "override"}
            maximum = getattr(team, limit)
            if value > maximum:
                res.blockers.append(
                    Blocker(
                        "LIMIT_EXCEEDED",
                        f"/{name}",
                        f"{name} {value} is over the team limit {maximum}",
                        extra={"limit": maximum},
                    )
                )
            effective[name] = value

    # --- model and harness ---------------------------------------------------------

    def _agent(self, conn: Connection, request, effective, res: Resolution) -> None:
        effective["model"] = effective["harness"] = None
        if request["execution_mode"] != "agent":
            return
        if self.network_of() is False:
            # Every model run restricts its task network (U-18); refusing here keeps the
            # quota untouched instead of failing when the Runner starts.
            res.blockers.append(
                Blocker(
                    "NETWORK_POLICY_UNSUPPORTED",
                    "/execution_mode",
                    "This host cannot restrict task networks (kernel nftables support), "
                    "so model runs are refused; Oracle runs still work",
                    error="CONFIG_INCOMPATIBLE",
                )
            )
        model = self._model(conn, request["model_config_id"], request.get("model_revision"), res)
        harness = self._harness(conn, request["harness_version_id"], res)
        if model is None or harness is None:
            return
        res.field_sources["/model"] = {
            "layer": "override",
            "source_id": model["config_id"],
            "revision": model["revision"],
        }
        res.field_sources["/harness"] = {"layer": "override", "source_id": harness["version_id"]}
        if model["protocol"] not in harness["protocols"]:
            res.blockers.append(
                Blocker(
                    "HARNESS_PROTOCOL_UNSUPPORTED",
                    "/harness_version_id",
                    f"{harness['name']} {harness['version']} cannot call a "
                    f"{model['protocol']} model",
                    error="CONFIG_INCOMPATIBLE",
                )
            )
        adapters = self.adapters_of()
        if adapters is not None and model["protocol"] not in adapters.get(harness["agent"], ()):
            res.blockers.append(
                Blocker(
                    "EXECUTOR_UNSUPPORTED",
                    "/harness_version_id",
                    f"The executor cannot run {harness['agent']} with {model['protocol']}",
                    error="CONFIG_INCOMPATIBLE",
                )
            )
        allowed = self.model_params_of().get(harness["agent"])
        dropped = sorted(set(model["params"] or {}) - set(allowed)) if allowed is not None else []
        if dropped:
            res.blockers.append(
                Blocker(
                    "HARNESS_MODEL_PARAMS_UNSUPPORTED",
                    "/model_config_id",
                    f"{harness['agent']} cannot pass on the model parameters "
                    f"{', '.join(dropped)}; it takes {', '.join(sorted(allowed)) or 'none'}",
                    error="CONFIG_INCOMPATIBLE",
                )
            )
        params = {}
        schema = harness["schema"].get("parameters") or {}
        for name, value in request["harness_params"].items():
            spec = schema.get(name)
            if spec is None:
                res.blockers.append(
                    Blocker(
                        "UNKNOWN_PARAMETER", f"/harness_params/{name}", "Not a harness parameter"
                    )
                )
            elif problem := param_problem(spec, value):
                res.blockers.append(Blocker(problem, f"/harness_params/{name}", "Invalid value"))
            else:
                params[name] = value
                res.field_sources[f"/harness/params/{name}"] = {"layer": "override"}
        for name, spec in schema.items():
            if name not in params and "default" in spec:
                params[name] = spec["default"]
                res.field_sources[f"/harness/params/{name}"] = {
                    "layer": "harness",
                    "source_id": harness["version_id"],
                }
        effective["model"] = {
            k: model[k]
            for k in (
                "config_id",
                "revision",
                "provider",
                "protocol",
                "endpoint",
                "model_id",
                "params",
                "credential_id",
            )
        }
        effective["harness"] = {
            "version_id": harness["version_id"],
            "name": harness["name"],
            "version": harness["version"],
            "agent": harness["agent"],
            "content_digest": harness["content_digest"],
            "params": dict(sorted(params.items())),
        }
        if harness["upstream_version"] is not None:
            effective["harness"]["upstream_version"] = harness["upstream_version"]
        res.labels["model"] = model["name"]
        res.labels["harness"] = f"{harness['name']} {harness['version']}"
        res.references["model_config"] = {"id": model["config_id"], "revision": model["revision"]}
        res.references["harness_version"] = harness["version_id"]
        if model["credential_id"]:
            res.credential_ids.append(model["credential_id"])

    def _model(
        self, conn: Connection, config_id: str, revision: int | None, res: Resolution
    ) -> dict | None:
        row = (
            conn.execute(
                select(t.model_configs, t.model_revisions)
                .join(
                    t.model_revisions,
                    (t.model_revisions.c.model_config_id == t.model_configs.c.id)
                    & (
                        t.model_revisions.c.revision
                        == (revision or t.model_configs.c.current_revision)
                    ),
                )
                .where(t.model_configs.c.id == config_id)
            )
            .mappings()
            .first()
        )
        path = "/model_config_id"
        # Before the row: a purged configuration has lost its revisions too.
        if deleted.state(conn, "model_config", config_id):
            res.blockers.append(
                Blocker(
                    "DELETED", path, "Model configuration was deleted", error="CONFIG_UNAVAILABLE"
                )
            )
            return None
        if row is None:
            found = "Model configuration" + (f" revision {revision}" if revision else "")
            res.blockers.append(Blocker("NOT_FOUND", path, f"{found} not found"))
            return None
        if not row["enabled"]:
            res.blockers.append(
                Blocker(
                    "DISABLED", path, "Model configuration is disabled", error="CONFIG_UNAVAILABLE"
                )
            )
        if row["credential_id"]:
            problem = credential_problem(conn, row["credential_id"], MODEL_KINDS)
            if problem:
                res.blockers.append(
                    Blocker(
                        "CREDENTIAL_UNAVAILABLE",
                        path,
                        f"The model's credential cannot be used ({problem.lower()}); "
                        "an administrator must fix the binding",
                        error="CONFIG_UNAVAILABLE",
                        extra={"reason": problem},
                    )
                )
        return {
            "config_id": config_id,
            "name": row["name"],
            "revision": row["revision"],
            "provider": row["provider"],
            "protocol": row["protocol"],
            "endpoint": row["endpoint"],
            "model_id": row["model_id"],
            "params": row["params_json"],
            "credential_id": row["credential_id"],
        }

    def _harness(self, conn: Connection, harness_id: str, res: Resolution) -> dict | None:
        row = (
            conn.execute(select(t.harness_versions).where(t.harness_versions.c.id == harness_id))
            .mappings()
            .first()
        )
        path = "/harness_version_id"
        if row is None:
            res.blockers.append(Blocker("NOT_FOUND", path, "Harness version not found"))
            return None
        if deleted.state(conn, "harness_version", harness_id):
            res.blockers.append(
                Blocker("DELETED", path, "Harness version was deleted", error="CONFIG_UNAVAILABLE")
            )
            return None
        if not row["enabled"]:
            res.blockers.append(
                Blocker("DISABLED", path, "Harness version is disabled", error="CONFIG_UNAVAILABLE")
            )
        return {
            "version_id": harness_id,
            "name": row["name"],
            "version": row["version"],
            "agent": row["agent"],
            "upstream_version": row["upstream_version"],
            "protocols": row["protocols_json"],
            "schema": row["schema_json"],
            "content_digest": row["content_digest"],
        }

    # --- attachments -----------------------------------------------------------------

    def _attachments(self, conn: Connection, team, request, effective, res: Resolution) -> None:
        """Ordered packages, each compatible with the harness; two packages may not claim
        the same name, tool or environment variable (03 section 7: no "last one wins")."""
        ids = request["attachments"]
        effective["attachments"] = []
        effective["bootstrap_timeout_sec"] = None
        if not ids:
            if request["timeouts"]["bootstrap_sec"] is not None:
                res.warnings.append(
                    {
                        "code": "BOOTSTRAP_TIMEOUT_UNUSED",
                        "path": "/timeouts/bootstrap_sec",
                        "message": "No attachment installs anything in this run",
                    }
                )
            return
        if len(ids) > team.run_max_attachments:
            res.blockers.append(
                Blocker(
                    "LIMIT_EXCEEDED",
                    "/attachments",
                    f"{len(ids)} attachments is over the team limit {team.run_max_attachments}",
                    extra={"limit": team.run_max_attachments},
                )
            )
        rows = {
            r["id"]: r
            for r in conn.execute(
                select(t.attachment_versions).where(t.attachment_versions.c.id.in_(ids))
            ).mappings()
        }
        gone = deleted.states(conn, "attachment_version", ids)
        agent = (effective.get("harness") or {}).get("agent")
        claimed: dict[tuple[str, str], str] = {}
        for index, attachment_id in enumerate(ids):
            path = f"/attachments/{index}"
            row = rows.get(attachment_id)
            if row is None:
                res.blockers.append(Blocker("NOT_FOUND", path, "Attachment not found"))
                continue
            label = f"{row['name']} {row['version']}"
            if attachment_id in gone:
                res.blockers.append(
                    Blocker("DELETED", path, f"{label} was deleted", error="CONFIG_UNAVAILABLE")
                )
                continue
            if not row["enabled"]:
                res.blockers.append(
                    Blocker("DISABLED", path, f"{label} is disabled", error="CONFIG_UNAVAILABLE")
                )
            manifest = row["manifest_json"]
            harnesses = manifest["compatible_harnesses"]
            if agent and "*" not in harnesses and agent not in harnesses:
                res.blockers.append(
                    Blocker(
                        "ATTACHMENT_INCOMPATIBLE",
                        path,
                        f"{label} does not support the {agent} harness",
                        error="CONFIG_INCOMPATIBLE",
                    )
                )
            claims = [("name", row["name"])] + [
                (kind, name) for kind in ("tools", "env") for name in manifest["provides"][kind]
            ]
            for claim in claims:
                if claim in claimed:
                    res.blockers.append(
                        Blocker(
                            "ATTACHMENT_CONFLICT",
                            path,
                            f"{label} and {claimed[claim]} both provide {claim[0]} {claim[1]!r}",
                            error="ATTACHMENT_CONFLICT",
                            extra={"kind": claim[0], "name": claim[1]},
                        )
                    )
                else:
                    claimed[claim] = label
            effective["attachments"].append(
                {
                    "version_id": attachment_id,
                    "name": row["name"],
                    "version": row["version"],
                    "content_digest": row["content_digest"],
                    "effects": manifest["effects"],
                    "provides": manifest["provides"],
                    "has_bootstrap": row["has_bootstrap"],
                }
            )
        res.field_sources["/attachments"] = {"layer": "override"}
        res.references["attachments"] = list(ids)
        res.labels["attachments"] = ", ".join(
            f"{a['name']} {a['version']}" for a in effective["attachments"]
        )
        if not any(a["has_bootstrap"] for a in effective["attachments"]):
            return
        value = request["timeouts"]["bootstrap_sec"]
        if value is None:
            value = team.bootstrap_timeout_sec
            res.field_sources["/timeouts/bootstrap_sec"] = {
                "layer": "team",
                "source_id": "team_policy",
                "revision": team.revision,
            }
        else:
            res.field_sources["/timeouts/bootstrap_sec"] = {"layer": "override"}
        if value > team.bootstrap_timeout_sec_max:
            res.blockers.append(
                Blocker(
                    "LIMIT_EXCEEDED",
                    "/timeouts/bootstrap_sec",
                    f"{value} is over the team limit {team.bootstrap_timeout_sec_max}",
                    extra={"limit": team.bootstrap_timeout_sec_max},
                )
            )
        effective["bootstrap_timeout_sec"] = value

    # --- tasks ---------------------------------------------------------------------

    def _tasks(self, conn: Connection, team, request, version, effective, res) -> None:
        rows = (
            conn.execute(
                select(t.tasks)
                .where(t.tasks.c.dataset_version_id == version["id"])
                .order_by(t.tasks.c.task_key)
            )
            .mappings()
            .all()
        )
        admissions = load_admissions(conn, [r["id"] for r in rows])
        availability = load_availability(
            conn,
            [r["content_digest"] for r in rows]
            + [d for a in admissions.values() if (d := expected_image(a))],
        )
        overrides = {
            "cpus": request["resources"]["cpus"],
            "memory_mb": request["resources"]["memory_mb"],
            "agent_timeout_sec": request["timeouts"]["agent_sec"],
            "verifier_timeout_sec": request["timeouts"]["verifier_sec"],
        }
        self._override_limits(team, overrides, res)
        parameters, declared = self._parameters(conn, request, rows, res)
        candidates, blocked = [], {}
        for row in rows:
            admission = admissions.get(row["id"])
            definition = row["definition_json"] or {}
            found = task_blockers(row, admission, availability)
            settings = self._task_settings(definition, overrides)
            found.extend(self._task_limits(team, settings, overrides))
            found.extend(self._task_parameters(definition, parameters))
            blocked[row["id"]] = found
            candidates.append(
                {
                    "task_id": row["id"],
                    "task_key": row["task_key"],
                    "content_digest": row["content_digest"],
                    "spec_digest": row["spec_digest"],
                    "admission_id": admission["id"] if admission else None,
                    "admission_revision": admission["revision"] if admission else None,
                    "preparation_method_digest": (
                        admission["preparation_method_digest"] if admission else None
                    ),
                    "expected_image_id": expected_image(admission),
                    "definition": definition,
                    "settings": settings,
                    "parameters": sorted(p["name"] for p in definition.get("parameters") or []),
                }
            )
        chosen = self._select(request["task_selection"], candidates, blocked, res)
        res.tasks = chosen
        effective["overrides"] = {k: v for k, v in overrides.items() if v is not None}
        effective["tasks"] = [
            {
                "task_key": x["task_key"],
                "content_digest": x["content_digest"],
                "spec_digest": x["spec_digest"],
                **x["settings"],
            }
            for x in chosen
        ]
        used = {name for x in chosen for name in x["parameters"]}
        effective["parameters"] = {k: v for k, v in parameters.items() if k in used}
        for name in effective["parameters"]:
            given = name in request["parameters"]
            res.field_sources[f"/parameters/{name}"] = {"layer": "override" if given else "author"}
        res.credential_ids.extend(
            sorted(
                {
                    v["credential_id"]
                    for v in effective["parameters"].values()
                    if "credential_id" in v
                }
            )
        )
        self._sources_and_baseline(overrides, chosen, effective, res)
        unknown = sorted(set(request["parameters"]) - declared)
        for name in unknown:
            res.blockers.append(
                Blocker(
                    "UNKNOWN_PARAMETER",
                    f"/parameters/{name}",
                    "No task of this version declares this parameter",
                )
            )
        ready = set(
            conn.execute(
                select(t.preparations.c.spec_digest).where(
                    t.preparations.c.spec_digest.in_([x["spec_digest"] for x in chosen]),
                    t.preparations.c.cache_state == "ready",
                )
            ).scalars()
        )
        for x in chosen:
            warm = x["spec_digest"] in ready
            res.cache_observations.append(
                {
                    "task_key": x["task_key"],
                    "cache_state": "ready" if warm else "cold",
                    "action": None if warm else "restore_on_run",
                }
            )
            if not warm:
                res.required_preparation.append(
                    {
                        "task_id": x["task_id"],
                        "task_key": x["task_key"],
                        "action": "restore_cache",
                        "spec_digest": x["spec_digest"],
                    }
                )

    @staticmethod
    def _task_settings(definition: dict, overrides: dict) -> dict:
        resources = definition.get("resources") or {}
        timeouts = definition.get("timeouts") or {}

        def finite(value):
            return value if _number(value) else None

        author = {
            "cpus": resources.get("cpus") or DEFAULT_CPUS,
            "memory_mb": resources.get("memory_mb") or DEFAULT_MEMORY_MB,
            # None: the task gives no limit and the executor's default applies.
            "agent_timeout_sec": finite(timeouts.get("agent_sec")),
            "verifier_timeout_sec": finite(timeouts.get("verifier_sec")),
        }
        return {k: author[k] if overrides[k] is None else overrides[k] for k in author}

    @staticmethod
    def _override_limits(team, overrides: dict, res: Resolution) -> None:
        for name, path, limit in (
            ("cpus", "/resources/cpus", team.trial_cpus_max),
            ("memory_mb", "/resources/memory_mb", team.trial_memory_mb_max),
            ("agent_timeout_sec", "/timeouts/agent_sec", team.agent_timeout_sec_max),
        ):
            if overrides[name] is not None and overrides[name] > limit:
                res.blockers.append(
                    Blocker(
                        "LIMIT_EXCEEDED",
                        path,
                        f"{overrides[name]} is over the team limit {limit}",
                        extra={"limit": limit},
                    )
                )

    @staticmethod
    def _task_limits(team, settings: dict, overrides: dict) -> list[dict]:
        """An author value over a team limit blocks the task: nothing is lowered silently."""
        found = []
        for name, limit in (
            ("cpus", team.trial_cpus_max),
            ("memory_mb", team.trial_memory_mb_max),
            ("agent_timeout_sec", team.agent_timeout_sec_max),
        ):
            value = settings[name]
            if overrides[name] is None and value is not None and value > limit:
                found.append(
                    {"code": "TEAM_LIMIT_EXCEEDED", "field": name, "value": value, "limit": limit}
                )
        return found

    def _parameters(self, conn: Connection, request, rows, res: Resolution):
        """Values for author parameters: this request's, else the author's default."""
        declared: dict[str, dict] = {}
        for row in rows:
            for p in (row["definition_json"] or {}).get("parameters") or []:
                known = declared.setdefault(p["name"], dict(p))
                known["sensitive"] = known["sensitive"] or p["sensitive"]
        values = {}
        for name, given in request["parameters"].items():
            path = f"/parameters/{name}"
            spec = declared.get(name)
            if spec is None:
                continue  # reported as unknown once the declared set is known
            if RESERVED_PARAMETER.fullmatch(name):
                res.blockers.append(
                    Blocker("RESERVED_NAME", path, "This name is reserved by the platform")
                )
                continue
            if "value" in given and spec["sensitive"]:
                res.blockers.append(
                    Blocker(
                        "SENSITIVE_PARAMETER_LITERAL",
                        path,
                        "A secret parameter takes a credential reference, not a value",
                    )
                )
                continue
            if "credential_id" in given:
                problem = credential_problem(conn, given["credential_id"], PARAMETER_KINDS)
                if problem:
                    res.blockers.append(
                        Blocker(
                            "CREDENTIAL_UNAVAILABLE",
                            path,
                            f"The credential cannot be used ({problem.lower()})",
                            error="CONFIG_UNAVAILABLE",
                            extra={"reason": problem},
                        )
                    )
                    continue
            values[name] = given
        for name, spec in declared.items():
            if (
                name not in values
                and name not in request["parameters"]
                and spec["default"] is not None
            ):
                values[name] = {"value": spec["default"]}
        return dict(sorted(values.items())), set(declared)

    @staticmethod
    def _task_parameters(definition: dict, values: dict) -> list[dict]:
        missing = [
            p["name"]
            for p in definition.get("parameters") or []
            if p["name"] not in values and (p["required"] or p["sensitive"])
        ]
        return [{"code": "CONFIG_REQUIRED_MISSING", "parameters": missing}] if missing else []

    @staticmethod
    def _select(selection: dict, candidates: list, blocked: dict, res: Resolution) -> list:
        def first(row):
            found = blocked[row["task_id"]]
            return found[0]["code"] if found else None

        def why(task_id: str, label: str) -> str:
            # A refusal names the exact content it waits for (02 section 8, E2E-DS-05):
            # field errors are all a refused submission carries.
            digest = next(
                (b["expected_digest"] for b in blocked[task_id] if b.get("expected_digest")),
                None,
            )
            return f"{label}: content {digest} is not available" if digest else label

        if selection["mode"] == "all_eligible":
            chosen = [r for r in candidates if first(r) is None]
            res.excluded.extend(
                {
                    "task_id": r["task_id"],
                    "task_key": r["task_key"],
                    "blockers": blocked[r["task_id"]],
                }
                for r in candidates
                if first(r)
            )
        else:
            by_id = {r["task_id"]: r for r in candidates}
            missing = [x for x in selection["task_ids"] if x not in by_id]
            stopped = [x for x in selection["task_ids"] if x in by_id and first(by_id[x])]
            if missing or stopped:
                res.blockers.append(
                    Blocker(
                        "TASKS_NOT_SUBMITTABLE",
                        "/task_selection/task_ids",
                        "Some selected tasks are unknown or blocked",
                        error="TASKS_NOT_SUBMITTABLE",
                        extra={
                            "tasks": [{"task_id": x, "blockers": blocked[x]} for x in stopped]
                            + [{"task_id": x, "blockers": [{"code": "NOT_FOUND"}]} for x in missing]
                        },
                        field_errors=[
                            FieldError("/task_selection/task_ids", "NOT_FOUND", x) for x in missing
                        ]
                        + [
                            FieldError("/task_selection/task_ids", first(by_id[x]), why(x, x))
                            for x in stopped
                        ],
                    )
                )
                return []
            chosen = [by_id[x] for x in selection["task_ids"]]
        if not chosen:
            res.blockers.append(
                Blocker(
                    "NO_SUBMITTABLE_TASKS",
                    "/task_selection",
                    "No submittable task in this version",
                    error="NO_SUBMITTABLE_TASKS",
                    field_errors=[
                        FieldError(
                            "/task_selection",
                            x["blockers"][0]["code"],
                            why(x["task_id"], x["task_key"]),
                        )
                        for x in res.excluded
                    ],
                )
            )
        return sorted(chosen, key=lambda r: r["task_key"])

    @staticmethod
    def _sources_and_baseline(overrides: dict, chosen: list, effective: dict, res) -> None:
        paths = {
            "cpus": "/resources/cpus",
            "memory_mb": "/resources/memory_mb",
            "agent_timeout_sec": "/timeouts/agent_sec",
            "verifier_timeout_sec": "/timeouts/verifier_sec",
        }
        deviates = False
        for name, path in paths.items():
            value = overrides[name]
            if value is None:
                res.field_sources[path] = {"layer": "author", "source_id": "tasks"}
                continue
            res.field_sources[path] = {"layer": "override"}
            authors = [
                Resolver._task_settings(x["definition"], dict.fromkeys(overrides))[name]
                for x in chosen
            ]
            lower = sum(1 for a in authors if a is not None and value < a)
            higher = sum(1 for a in authors if a is None or value > a)
            if lower or higher:
                deviates = True
                res.baseline_differences.append(
                    {"path": path, "value": value, "tasks_lower": lower, "tasks_higher": higher}
                )
            if lower and name in {"cpus", "memory_mb"}:
                # CPU and memory never go below what the author asked for (U-22).
                res.blockers.append(
                    Blocker(
                        "BELOW_AUTHOR_RESOURCES",
                        path,
                        f"{lower} task(s) ask for more; resources cannot be lowered below "
                        "the author's",
                    )
                )
            elif lower:
                res.warnings.append(
                    {
                        "code": "SHORTER_THAN_AUTHOR",
                        "path": path,
                        "message": f"Shorter than the author's limit for {lower} task(s)",
                    }
                )
        effective["deviates_from_author_baseline"] = deviates

    def _size_and_capacity(self, conn, team, effective, res: Resolution) -> None:
        trials = len(res.tasks) * effective["repetitions"]
        if len(res.tasks) > team.run_max_tasks or trials > team.run_max_trials:
            res.blockers.append(
                Blocker(
                    "RUN_TOO_LARGE",
                    "/task_selection",
                    "Run exceeds task or trial limits",
                    error="RUN_TOO_LARGE",
                    status=413,
                    extra={"task_count": len(res.tasks), "trial_count": trials},
                )
            )
        provider = (effective.get("model") or {}).get("provider")
        res.reservation = reservation(
            effective["concurrency"],
            [{"resources": x} for x in effective["tasks"]],
            provider,
            trials,
        )
        if self.capacity:
            reason = never_fits(conn, self.capacity, res.reservation)
            if reason:
                res.blockers.append(
                    Blocker(
                        reason,
                        "/concurrency",
                        "This run can never fit the configured execution capacity",
                        error="RUN_EXCEEDS_CAPACITY",
                    )
                )

    # --- acceptance -------------------------------------------------------------------

    @staticmethod
    def recheck(conn: Connection, res: Resolution) -> list[str]:
        """Inside the acceptance transaction: what changed since resolving (F-26)."""
        changed = []
        if policy.current(conn).revision != res.references.get("team_policy"):
            changed.append("/team_policy")
        model = (res.effective or {}).get("model")
        if model:
            row = conn.execute(
                select(t.model_configs.c.enabled, t.model_configs.c.current_revision).where(
                    t.model_configs.c.id == model["config_id"]
                )
            ).first()
            pinned = res.request.get("model_revision") is not None
            if (
                row is None
                or not row.enabled
                or (not pinned and row.current_revision != model["revision"])
                or deleted.state(conn, "model_config", model["config_id"])
            ):
                changed.append("/model")
        harness = (res.effective or {}).get("harness")
        if harness:
            enabled = conn.execute(
                select(t.harness_versions.c.enabled).where(
                    t.harness_versions.c.id == harness["version_id"]
                )
            ).scalar()
            if not enabled or deleted.state(conn, "harness_version", harness["version_id"]):
                changed.append("/harness")
        attachments = [a["version_id"] for a in (res.effective or {}).get("attachments") or []]
        if attachments:
            enabled = dict(
                conn.execute(
                    select(t.attachment_versions.c.id, t.attachment_versions.c.enabled).where(
                        t.attachment_versions.c.id.in_(attachments)
                    )
                ).all()
            )
            gone = deleted.states(conn, "attachment_version", attachments)
            for index, attachment_id in enumerate(attachments):
                if not enabled.get(attachment_id) or attachment_id in gone:
                    changed.append(f"/attachments/{index}")
        # Kinds never change; resolution already checked each reference's kind.
        for credential_id in res.credential_ids:
            if credential_problem(conn, credential_id, PARAMETER_KINDS + MODEL_KINDS):
                changed.append(f"/credentials/{credential_id}")
        return changed
