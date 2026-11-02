"""Scoring contract and run summaries.

Pure functions over trial records: web pages, the API and exports all call
``summarize`` so they report the same counts. Only trials in state ``scored``
(a legal number with reward_valid) enter the statistics; a real 0 is a sample,
an invalid reward is never one even when a number exists.
"""

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass

from maestro.domain.states import TRIAL_ACTIVE, TrialState

CONTRACT_VERSION = 1

# The only aggregation rules ``summarize`` computes, and the only ones a contract may name:
# a summary echoes its contract, so it must not claim a rule the maths does not apply.
TASK_AGGREGATION = "mean_of_scored_repetitions"
BENCH_AGGREGATION = "macro_mean_of_task_means"

# Used when the dataset version declares no confirmed contract. ``passed`` needs an
# explicit threshold (never "score > 0"); Harbor verifiers write 1 for a solved task.
DEFAULT_CONTRACT = {
    "contract_version": CONTRACT_VERSION,
    "source": "platform_default",
    "metric_id": "reward",
    "name": "Reward",
    "unit": None,
    "direction": "higher_is_better",
    "passed": {"rule": "threshold", "threshold": 1.0},
    "task_aggregation": TASK_AGGREGATION,
    "bench_aggregation": BENCH_AGGREGATION,
    "count_as_failure": [],  # The extension is not implemented: only [] is accepted.
}

_DIRECTIONS = {"higher_is_better", "lower_is_better"}


def contract_errors(contract: dict) -> list[str]:
    """Names of the contract fields ``summarize`` cannot honour, empty when it can."""
    passed, unit, direction = (
        contract.get("passed"),
        contract.get("unit"),
        contract.get("direction"),
    )
    checks = {
        "contract_version": contract.get("contract_version") == CONTRACT_VERSION,
        "metric_id": _text(contract.get("metric_id")),
        "name": _text(contract.get("name")),
        "unit": unit is None or isinstance(unit, str),
        "direction": isinstance(direction, str) and direction in _DIRECTIONS,
        "passed": passed is None
        or (
            isinstance(passed, dict)
            and passed.get("rule") == "threshold"
            and _number(passed.get("threshold"))
        ),
        "task_aggregation": contract.get("task_aggregation") == TASK_AGGREGATION,
        "bench_aggregation": contract.get("bench_aggregation") == BENCH_AGGREGATION,
        "count_as_failure": contract.get("count_as_failure") == [],
    }
    return [name for name, ok in checks.items() if not ok]


def contract_for(manifest: dict | None) -> dict:
    """The dataset version's declared contract when well formed, else the default."""
    declared = (manifest or {}).get("scoring_contract")
    if not isinstance(declared, dict):
        return dict(DEFAULT_CONTRACT)
    contract = {**DEFAULT_CONTRACT, **declared, "source": "dataset_version"}
    return dict(DEFAULT_CONTRACT) if contract_errors(contract) else contract


def snapshot_contract(policy: dict | None) -> dict:
    """The contract a run snapshot froze at acceptance (written by ``contract_for``)."""
    return (policy or {}).get("scoring") or DEFAULT_CONTRACT


def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _number(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


@dataclass(frozen=True)
class TrialRecord:
    task_key: str
    state: str
    raw_score: float | None = None
    failure_cause: str | None = None
    attempts: int = 0
    usage: dict | None = None


def passed(contract: dict, score: float) -> bool | None:
    rule = contract.get("passed")
    if not rule:
        return None
    if contract["direction"] == "lower_is_better":
        return score <= rule["threshold"]
    return score >= rule["threshold"]


def mean(values: list[float]) -> float | None:
    """Correctly rounded: ``statistics`` works in exact fractions, so three 0.1 average
    to 0.1 and not to 0.10000000000000002."""
    return float(statistics.mean(values)) if values else None


def sample_stddev(values: list[float]) -> float | None:
    """n - 1 denominator, exact like ``mean`` (equal values spread exactly 0.0); fewer
    than two samples have no spread (null, not 0)."""
    return float(statistics.stdev(values)) if len(values) >= 2 else None


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


# A trial that has not started cannot have used anything yet: waiting in the queue,
# preparing its environment, or never started because the run stopped first.
_NOT_STARTED = {TrialState.QUEUED, TrialState.PREPARING, TrialState.NOT_STARTED}


def _usage(trials: list[TrialRecord]) -> dict:
    """Known totals plus how many started trials reported nothing."""
    totals: dict[str, list[float]] = defaultdict(list)
    known = started = 0
    for trial in trials:
        if trial.state in _NOT_STARTED:
            continue
        started += 1
        values = {k: v for k, v in (trial.usage or {}).items() if _number(v)}
        if values:
            known += 1
            for key, value in values.items():
                totals[key].append(value)
    return {
        "known_trials": known,
        "unknown_trials": started - known,
        "known_totals": {key: math.fsum(values) for key, values in totals.items()},
    }


def _bucket(trial: TrialRecord) -> str:
    """Every trial lands in exactly one bucket, so planned = scored + excluded + unfinished
    + active + queued holds by construction. A trial recorded as scored
    without a legal number is not a sample: it is excluded, never dropped."""
    state = trial.state
    if state == TrialState.SCORED:
        return "scored" if _number(trial.raw_score) else "excluded"
    if state == TrialState.EXECUTION_ERROR:
        return "excluded"
    if state == TrialState.QUEUED:
        return "queued"
    if state in TRIAL_ACTIVE:
        return "active"
    return "unfinished"  # not started, cancelled, interrupted


def summarize(
    trials: list[TrialRecord], contract: dict, bench_task_count: int | None = None
) -> dict:
    """At a terminal run planned = scored + count_as_failure + excluded +
    unfinished; while running, active and queued trials make up the rest."""
    buckets: dict[str, list[TrialRecord]] = defaultdict(list)
    for trial in trials:
        buckets[_bucket(trial)].append(trial)
    scored, excluded = buckets["scored"], buckets["excluded"]
    values = [x.raw_score for x in scored]
    has_pass_rule = bool(contract.get("passed"))
    passed_count = sum(1 for x in scored if passed(contract, x.raw_score)) if has_pass_rule else 0
    planned = len(trials)
    count_as_failure = 0  # Non-default extension; not offered yet.

    by_task: dict[str, list[TrialRecord]] = defaultdict(list)
    for trial in trials:
        by_task[trial.task_key].append(trial)
    tasks = []
    for task_key in sorted(by_task):
        group = by_task[task_key]
        task_values = [x.raw_score for x in group if _bucket(x) == "scored"]
        task_excluded = [x for x in group if _bucket(x) == "excluded"]
        tasks.append(
            {
                "task_key": task_key,
                "planned_trials": len(group),
                "scored_trials": len(task_values),
                "mean": mean(task_values),
                "stddev": sample_stddev(task_values),
                "passed_trials": sum(1 for v in task_values if passed(contract, v))
                if has_pass_rule
                else None,
                # Some planned repetitions have no score: the mean covers fewer samples.
                "partial": 0 < len(task_values) < len(group),
                "excluded_trials": len(task_excluded),
                "failure_causes": sorted(
                    {x.failure_cause or "unclassified" for x in task_excluded}
                ),
            }
        )
    task_means = [x["mean"] for x in tasks if x["mean"] is not None]
    causes: dict[str, int] = defaultdict(int)
    for trial in excluded:
        causes[trial.failure_cause or "unclassified"] += 1
    attempts = sum(x.attempts for x in trials)
    retried = [x for x in trials if x.attempts > 1]
    if scored and len(scored) == planned:
        completeness = "complete"
    elif scored:
        completeness = "partial"
    else:
        completeness = "none"
    return {
        "contract": contract,
        "planned_trials": planned,
        "scored_trials": len(scored),
        "count_as_failure_trials": count_as_failure,
        "excluded_error_trials": len(excluded),
        "unfinished_trials": len(buckets["unfinished"]),
        "active_trials": len(buckets["active"]),
        "queued_trials": len(buckets["queued"]),
        "passed_trials": passed_count if has_pass_rule else None,
        "mean": mean(values),
        "stddev": sample_stddev(values),
        "pass_rate": _ratio(passed_count, len(scored) + count_as_failure)
        if has_pass_rule
        else None,
        "scored_coverage": _ratio(len(scored), planned),
        "evaluation_coverage": _ratio(len(scored) + count_as_failure, planned),
        "bench_task_coverage": _ratio(len(by_task), bench_task_count) if bench_task_count else None,
        "bench_score": mean(task_means),
        "bench_scored_tasks": len(task_means),
        "task_count": len(by_task),
        "result_completeness": completeness,
        "excluded_by_cause": dict(sorted(causes.items())),
        "attempts": attempts,
        "retried_trials": len(retried),
        "retry_count": sum(x.attempts - 1 for x in retried),
        "usage": _usage(trials),
        "tasks": tasks,
    }


def compact(summary: dict) -> dict:
    """The run-list subset: enough to read a mean next to its coverage."""
    keys = (
        "planned_trials",
        "scored_trials",
        "excluded_error_trials",
        "unfinished_trials",
        "passed_trials",
        "mean",
        "stddev",
        "pass_rate",
        "scored_coverage",
        "bench_score",
    )
    return {"metric_id": summary["contract"]["metric_id"], **{k: summary[k] for k in keys}}
