"""Comparing runs side by side (06 section 8): dimensions, failure groups, alignment.

Pure functions over what a run snapshot froze (its comparison view and scoring contract)
and its trial records. Web pages, the API, the SDK and saved snapshots all call
``compare``, so the same runs with the same settings give the same table.

A comparison never changes a run: excluding a failure group, counting the remaining
failures as failed and dropping tasks only shape this table. A rerun is one more run,
never merged into its source.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass

from maestro.core.digests import digest_json
from maestro.domain.scoring import mean, passed, sample_stddev
from maestro.domain.states import TRIAL_ACTIVE, TrialState

# Snapshots keep the tables they computed: a change to the table's shape bumps this, and
# reading a snapshot must then still understand the older shape.
COMPARISON_VERSION = 1
GROUPING_VERSION = 1

# --- dimensions -------------------------------------------------------------------------

DIMENSIONS = (
    "dataset",
    "scoring",
    "model",
    "harness",
    "attachments",
    "resources",
    "repetitions",
    "parameters",
    "other",
)
# A collection may let these vary; the dataset content, the scoring contract and anything
# not named here always stay fixed, so a collection never spans two benchmarks.
VARIABLE_DIMENSIONS = (
    "model",
    "harness",
    "attachments",
    "resources",
    "repetitions",
    "parameters",
)
# What an ad-hoc comparison is expected to vary: differences elsewhere are warned about.
SUBJECT_DIMENSIONS = ("model", "harness", "attachments")
# Differences here make an aligned table meaningless: only raw summaries side by side.
BLOCKING_DIMENSIONS = ("dataset", "scoring")

# Keys of ``comparison_view`` and the dimension each belongs to; any other key is "other".
_KEY_DIMENSION = {
    "dataset": "dataset",
    "model": "model",
    "harness": "harness",
    "attachments": "attachments",
    "bootstrap_timeout_sec": "attachments",
    "overrides": "resources",
    "repetitions": "repetitions",
    "parameters": "parameters",
}

# Worked out from other keys (the overrides against the same task content): comparing
# it again would only repeat a difference already shown.
_DERIVED = {"deviates_from_author_baseline"}

SCENARIOS = {
    "models": ("model",),
    "harnesses": ("harness",),
    "model_harness": ("model", "harness"),
    "attachments": ("attachments",),
    "repeats": (),
    "custom": None,  # chosen by the creator
}


def dimensions_of(view: dict, contract: dict) -> dict[str, dict]:
    """A run's comparison view split by dimension; the scoring contract is its own
    dimension (where it came from is not part of what it means)."""
    found: dict[str, dict] = {d: {} for d in DIMENSIONS}
    for key, value in view.items():
        if key in _DERIVED:
            continue
        found[_KEY_DIMENSION.get(key, "other")][key] = value
    found["scoring"] = {k: v for k, v in (contract or {}).items() if k != "source"}
    return found


def pointers(old, new, path: str = "") -> list[str]:
    """JSON pointers where two values differ (lists compared item by item)."""
    if old == new:
        return []
    if isinstance(old, dict) and isinstance(new, dict):
        found = []
        for key in sorted(set(old) | set(new)):
            found += pointers(old.get(key), new.get(key), f"{path}/{key}")
        return found
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        found = []
        for index, (a, b) in enumerate(zip(old, new, strict=True)):
            found += pointers(a, b, f"{path}/{index}")
        return found
    return [path or "/"]


def label(dimension: str, value: dict) -> str:
    """A short name for a dimension's value, used for row and column titles."""
    if dimension == "model":
        model = value.get("model") or {}
        return model.get("model_id") or "-"
    if dimension == "harness":
        harness = value.get("harness") or {}
        return f"{harness.get('name', '-')} {harness.get('version', '')}".strip()
    if dimension == "attachments":
        names = [f"{a.get('name')} {a.get('version')}" for a in value.get("attachments") or []]
        return " + ".join(names) or "none"
    if dimension == "resources":
        overrides = value.get("overrides") or {}
        return " ".join(f"{k}={overrides[k]}" for k in sorted(overrides)) or "author"
    if dimension == "repetitions":
        return str(value.get("repetitions"))
    if dimension == "parameters":
        parameters = value.get("parameters") or {}
        return ", ".join(f"{k}={parameters[k]}" for k in sorted(parameters)) or "author"
    if dimension == "dataset":
        dataset = value.get("dataset") or {}
        return f"{dataset.get('namespace')}/{dataset.get('name')}"
    if dimension == "scoring":
        return str(value.get("metric_id"))
    if all(isinstance(v, str | int | float | bool) for v in value.values()):
        return ", ".join(f"{k}={value[k]}" for k in sorted(value))
    return digest_json(value)[7:15]


def _labels(dimension: str, values: dict[str, dict]) -> dict[str, str]:
    """Labels per run; two different values that read the same get a short digest."""
    named = {run_id: label(dimension, value) for run_id, value in values.items()}
    by_label: dict[str, set[str]] = defaultdict(set)
    for run_id, text in named.items():
        by_label[text].add(digest_json(values[run_id]))
    return {
        run_id: text if len(by_label[text]) == 1 else f"{text} #{digest_json(values[run_id])[7:13]}"
        for run_id, text in named.items()
    }


def nonconforming(fixed: dict[str, dict], dims: dict[str, dict]) -> list[dict]:
    """Where a run differs from a collection's fixed dimensions (empty: it conforms)."""
    found = []
    for dimension in DIMENSIONS:
        if dimension not in fixed:
            continue
        for pointer in pointers(fixed[dimension], dims[dimension]):
            found.append({"dimension": dimension, "path": pointer})
    return found


# --- failure groups ---------------------------------------------------------------------

EXTERNAL = "external"
SUBJECT = "subject"

# Why a trial has no valid score, finer than its cause: whether the thing under test
# could have caused it decides the default. Versioned: snapshots record the version.
GROUPS = {
    "provider_unavailable": EXTERNAL,  # rate limits, dropped connections, overload
    "provider_access": EXTERNAL,  # authentication, unknown model, quota or budget
    "environment_setup": EXTERNAL,  # image build, start, health check, harness install
    "preparation": EXTERNAL,  # cache restore, attachment content missing or not copied
    "executor_unsupported": EXTERNAL,
    "platform_stop": EXTERNAL,  # not started, cancelled, interrupted, restarts, deadline
    "out_of_memory": SUBJECT,
    "verifier": SUBJECT,  # the verifier produced no score
    "attachment": SUBJECT,  # an attachment failed to install
    "provider_request": SUBJECT,  # other permanent model errors: request or harness setup
    "unclassified": SUBJECT,
}
DEFAULT_EXCLUDED = tuple(g for g, side in GROUPS.items() if side == EXTERNAL)

_BY_EXCEPTION = {
    "SandboxBuildFailedError": "environment_setup",
    "EnvironmentStartTimeoutError": "environment_setup",
    "HealthcheckError": "environment_setup",
    "AgentSetupTimeoutError": "environment_setup",
    "MemoryLimitExceededError": "out_of_memory",
    "SandboxLikelyOutOfMemoryError": "out_of_memory",
    "AttachmentTransferFailed": "preparation",
    "AttachmentContentMissing": "preparation",
    "AttachmentInstallFailed": "attachment",
    "AttachmentInstallTimeout": "attachment",
    "ApiUsageLimitError": "provider_access",
    "PaymentRequiredError": "provider_access",
    "BudgetExceededError": "provider_access",
}
_BY_CAUSE = {
    "provider_transient": "provider_unavailable",
    "provider_auth": "provider_access",
    "provider_permanent": "provider_request",
    "model_provider_failed": "provider_request",
    "sandbox_failed": "environment_setup",
    "harbor_failed": "executor_unsupported",
    "verifier_failed": "verifier",
    "attachment_failed": "attachment",
    "cancelled": "platform_stop",
    "preparation_restore_failed": "preparation",
    "resource_content_unavailable": "preparation",
    "resource_content_mismatch": "preparation",
    "credential_unavailable": "provider_access",
}
# Platform reasons of trials that ended without an executor verdict.
_PLATFORM_REASONS = {
    "user_cancelled",
    "admin_cancelled",
    "run_deadline_exceeded",
    "run_interrupted",
    "service_restarted",
    "runner_unresponsive",
    "runner_protocol_error",
    "runner_failed",
    "runner_exited",
    "executor_failed",
    "executor_no_result",
    "storage_write_failed",
    "stale_execution_lease",
}
_UNSTARTED = {TrialState.NOT_STARTED, TrialState.CANCELLED, TrialState.INTERRUPTED}


def failure_group(trial: ComparedTrial) -> str:
    if trial.exception_type in _BY_EXCEPTION:
        return _BY_EXCEPTION[trial.exception_type]
    cause = (trial.failure_cause or "").lower()
    if cause in _BY_CAUSE:
        return _BY_CAUSE[cause]
    if cause in _PLATFORM_REASONS or trial.state in _UNSTARTED:
        return "platform_stop"
    return "unclassified"


# --- inputs -------------------------------------------------------------------------------


@dataclass(frozen=True)
class ComparedTrial:
    trial_id: str
    task_key: str
    repetition: int
    state: str
    raw_score: float | None = None
    failure_cause: str | None = None
    exception_type: str | None = None
    usage: dict | None = None
    actual_model: dict | None = None


@dataclass(frozen=True)
class ComparedRun:
    run_id: str
    view: dict  # the snapshot's comparison view
    contract: dict  # the snapshot's scoring contract
    terminal: bool
    trials: tuple[ComparedTrial, ...]


def _legal(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _outcome(trial: ComparedTrial, excluded: set[str]) -> tuple[str, str | None]:
    """'valid' (a score), 'failed' (counted as failed), 'excluded', or 'pending'."""
    if trial.state == TrialState.SCORED and _legal(trial.raw_score):
        return "valid", None
    if trial.state == TrialState.QUEUED or trial.state in TRIAL_ACTIVE:
        return "pending", None
    group = failure_group(trial)
    return ("excluded" if group in excluded else "failed"), group


# --- the comparison ------------------------------------------------------------------------


def _cell(trials: list[tuple[ComparedTrial, str, str | None]], contract: dict) -> dict:
    """One run's trials of one task. Failures count as 0 when higher is better; with a
    lower-is-better metric 0 would be the best score, so they only count as not passed."""
    counts_as_zero = contract.get("direction") != "lower_is_better"
    values = [x.raw_score for x, kind, _ in trials if kind == "valid"]
    failed = [g for _, kind, g in trials if kind == "failed"]
    rule = bool(contract.get("passed"))
    passed_count = sum(1 for v in values if passed(contract, v)) if rule else None
    sample = values + [0.0] * len(failed) if counts_as_zero else values
    excluded: dict[str, int] = defaultdict(int)
    for _, kind, group in trials:
        if kind == "excluded":
            excluded[group] += 1
    failed_groups: dict[str, int] = defaultdict(int)
    for group in failed:
        failed_groups[group] += 1
    ordered = sorted(trials, key=lambda x: x[0].repetition)
    return {
        "planned_trials": len(trials),
        "scored_trials": len(values),
        "failed_trials": len(failed),
        "excluded_trials": sum(excluded.values()),
        "pending_trials": sum(1 for _, kind, _ in trials if kind == "pending"),
        "valid_trials": len(values) + len(failed),
        "mean": mean(sample),
        "passed_trials": passed_count,
        "excluded_by_group": dict(sorted(excluded.items())),
        "failed_by_group": dict(sorted(failed_groups.items())),
        "trial_ids": [x.trial_id for x, _, _ in ordered],
        "scores": [x.raw_score if kind == "valid" else None for x, kind, _ in ordered],
    }


def _usage(trials) -> dict:
    totals: dict[str, float] = defaultdict(float)
    known = 0
    for trial in trials:
        values = {k: v for k, v in (trial.usage or {}).items() if _legal(v)}
        if values:
            known += 1
            for key, value in values.items():
                totals[key] += value
    return {"known_trials": known, "known_totals": dict(sorted(totals.items()))}


def compare(
    runs: list[ComparedRun],
    *,
    exclude: list[str] | tuple[str, ...] = DEFAULT_EXCLUDED,
    scope: str = "common",
    baseline: str | None = None,
    expected_variable: tuple[str, ...] | list[str] = SUBJECT_DIMENSIONS,
) -> dict:
    """The comparison table. ``scope`` is 'common' (tasks every run has) or 'all'.
    Differences to the baseline row are given only when the runs share dataset content
    and scoring contract and only common tasks are compared."""
    excluded = set(exclude)
    ids = [r.run_id for r in runs]
    dims = {r.run_id: dimensions_of(r.view, r.contract) for r in runs}

    dimensions = []
    for dimension in DIMENSIONS:
        values = {run_id: dims[run_id][dimension] for run_id in ids}
        first = values[ids[0]] if ids else {}
        paths = sorted({p for v in values.values() for p in pointers(first, v)})
        dimensions.append(
            {
                "key": dimension,
                "varies": bool(paths),
                "paths": paths,
                "labels": _labels(dimension, values),
            }
        )
    varying = [d["key"] for d in dimensions if d["varies"]]
    blockers = [
        {"code": f"{d.upper()}_DIFFERS", "dimension": d}
        for d in BLOCKING_DIMENSIONS
        if d in varying
    ]
    comparable = not blockers
    warnings = [
        {"code": "UNEXPECTED_DIFFERENCE", "dimension": d}
        for d in varying
        if d not in BLOCKING_DIMENSIONS and d not in expected_variable
    ]
    if not comparable:
        scope = "all"

    outcomes: dict[str, dict[str, list]] = {}
    for run in runs:
        by_task: dict[str, list] = defaultdict(list)
        for trial in run.trials:
            kind, group = _outcome(trial, excluded)
            by_task[trial.task_key].append((trial, kind, group))
        outcomes[run.run_id] = by_task
    task_sets = [set(outcomes[run_id]) for run_id in ids]
    union = sorted(set().union(*task_sets)) if task_sets else []
    common = sorted(set.intersection(*task_sets)) if task_sets else []
    contracts = {r.run_id: r.contract for r in runs}
    cells = {
        run_id: {task: _cell(rows, contracts[run_id]) for task, rows in outcomes[run_id].items()}
        for run_id in ids
    }

    tasks = []
    compared: set[str] = set()
    dropped = 0
    if comparable:
        for task in union:
            row = {run_id: cells[run_id].get(task) for run_id in ids}
            if task not in common:
                status = "not_common"
            elif all(c["valid_trials"] for c in row.values()):
                status = "compared"
            else:
                status = "no_valid_trials"
                dropped += 1
            if status == "compared" or scope == "all":
                compared.add(task)
            tasks.append({"task_key": task, "status": status, "cells": row})

    rows = []
    for run in runs:
        own = cells[run.run_id]
        chosen = [own[x] for x in sorted(own) if (x in compared or not comparable)]
        chosen = [c for c in chosen if c["valid_trials"]]
        task_means = [c["mean"] for c in chosen if c["mean"] is not None]
        rule = bool(run.contract.get("passed"))
        valid = sum(c["valid_trials"] for c in chosen)
        passed_count = sum(c["passed_trials"] or 0 for c in chosen) if rule else None
        every = list(own.values())
        excluded_groups: dict[str, int] = defaultdict(int)
        failed_groups: dict[str, int] = defaultdict(int)
        for c in every:
            for g, n in c["excluded_by_group"].items():
                excluded_groups[g] += n
            for g, n in c["failed_by_group"].items():
                failed_groups[g] += n
        observed = {
            json.dumps(x.actual_model, sort_keys=True) for x in run.trials if x.actual_model
        }
        rows.append(
            {
                "run_id": run.run_id,
                "terminal": run.terminal,
                "title": " · ".join(
                    next(d for d in dimensions if d["key"] == k)["labels"][run.run_id]
                    for k in varying
                    if k not in BLOCKING_DIMENSIONS
                ),
                "task_count": len(chosen),
                "compared_trials": sum(c["planned_trials"] for c in chosen),
                "valid_trials": valid,
                "mean": mean(task_means),
                "stddev": sample_stddev(task_means),
                "passed_trials": passed_count,
                "pass_rate": (passed_count / valid) if rule and valid else None,
                "planned_trials": sum(c["planned_trials"] for c in every),
                "scored_trials": sum(c["scored_trials"] for c in every),
                "failed_trials": sum(c["failed_trials"] for c in every),
                "excluded_trials": sum(c["excluded_trials"] for c in every),
                "pending_trials": sum(c["pending_trials"] for c in every),
                "excluded_by_group": dict(sorted(excluded_groups.items())),
                "failed_by_group": dict(sorted(failed_groups.items())),
                "usage": _usage(run.trials),
                "actual_models": sorted(observed),
                "difference": None,
            }
        )

    by_model: dict[str, set] = defaultdict(set)
    for row in rows:
        model_value = json.dumps(dims[row["run_id"]]["model"], sort_keys=True)
        by_model[model_value].add(tuple(row["actual_models"]))
    if any(len(observed) > 1 for observed in by_model.values()):
        warnings.append({"code": "ACTUAL_MODEL_DIFFERS"})
    if any(not r.terminal for r in runs):
        warnings.append(
            {"code": "RUN_IN_PROGRESS", "run_ids": [r.run_id for r in runs if not r.terminal]}
        )
    if comparable and len(common) < len(union):
        warnings.append({"code": "TASK_SETS_DIFFER", "common": len(common), "all": len(union)})
    if any(r.contract.get("direction") == "lower_is_better" for r in runs):
        warnings.append({"code": "FAILURES_NOT_IN_MEAN"})

    if baseline not in ids:
        baseline = ids[0] if ids else None
    if comparable and scope == "common" and baseline:
        base = next(r for r in rows if r["run_id"] == baseline)
        for row in rows:
            if row["run_id"] == baseline:
                continue
            row["difference"] = {
                "mean": _minus(row["mean"], base["mean"]),
                "pass_rate": _minus(row["pass_rate"], base["pass_rate"]),
            }

    groups = []
    for group, side in GROUPS.items():
        count = sum(
            r["excluded_by_group"].get(group, 0) + r["failed_by_group"].get(group, 0) for r in rows
        )
        groups.append({"key": group, "side": side, "excluded": group in excluded, "trials": count})
    return {
        "comparison_version": COMPARISON_VERSION,
        "grouping_version": GROUPING_VERSION,
        "settings": {"exclude": sorted(excluded), "scope": scope, "baseline_run_id": baseline},
        "comparable": comparable,
        "blockers": blockers,
        "warnings": warnings,
        "dimensions": dimensions,
        "varying": varying,
        "groups": groups,
        "runs": rows,
        "tasks": tasks,
        "common_tasks": len(common),
        "all_tasks": len(union),
        "dropped_tasks": dropped,
    }


def _minus(a, b):
    return a - b if a is not None and b is not None else None
