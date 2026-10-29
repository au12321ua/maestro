"""Comparing runs (06 section 8): dimensions, failure groups, alignment and statistics,
as pure functions over snapshot views and trial records."""

import pytest

from maestro.domain.comparison import (
    DEFAULT_EXCLUDED,
    ComparedRun,
    ComparedTrial,
    compare,
    dimensions_of,
    failure_group,
    nonconforming,
)
from maestro.domain.scoring import DEFAULT_CONTRACT
from maestro.execution.policy import HARBOR_EXCEPTIONS, harbor_outcome

DATASET = {"source_kind": "hub", "namespace": "demo", "name": "tb", "resolved_digest": "sha256:a"}


def view(model="gpt", harness="codex", **extra):
    return {
        "dataset": DATASET,
        "execution_mode": "agent",
        "repetitions": 1,
        "model": model
        if isinstance(model, dict)
        else {"provider": "p", "endpoint": "https://x", "model_id": model, "params": {}},
        "harness": {"name": harness, "version": "1", "agent": harness, "params": {}},
        "attachments": [],
        "bootstrap_timeout_sec": None,
        "overrides": {},
        "parameters": {},
        "deviates_from_author_baseline": False,
        **extra,
    }


def trial(task, score=None, state="scored", rep=1, cause=None, exception=None, **kw):
    return ComparedTrial(
        trial_id=f"{task}-{rep}",
        task_key=task,
        repetition=rep,
        state=state,
        raw_score=score,
        failure_cause=cause,
        exception_type=exception,
        **kw,
    )


def run(run_id, trials, terminal=True, contract=DEFAULT_CONTRACT, **view_args):
    return ComparedRun(run_id, view(**view_args), contract, terminal, tuple(trials))


def error(task, cause, exception=None, rep=1):
    return trial(task, state="execution_error", cause=cause, exception=exception, rep=rep)


def by_id(table):
    return {r["run_id"]: r for r in table["runs"]}


def test_dimensions_detect_what_differs_and_name_rows_by_it():
    a = run("a", [trial("t1", 1.0)], model="gpt", harness="codex")
    b = run("b", [trial("t1", 0.0)], model="haiku", harness="codex")
    table = compare([a, b])
    assert table["varying"] == ["model"]
    assert table["comparable"] and table["warnings"] == []
    rows = by_id(table)
    assert rows["a"]["title"] == "gpt" and rows["b"]["title"] == "haiku"
    assert rows["b"]["difference"] == {"mean": -1.0, "pass_rate": -1.0}
    assert rows["a"]["difference"] is None
    # Resources are not what an ad-hoc comparison expects to vary: compared, but warned.
    c = run("c", [trial("t1", 1.0)], overrides={"cpus": 2})
    table = compare([a, c])
    assert {"code": "UNEXPECTED_DIFFERENCE", "dimension": "resources"} in table["warnings"]
    assert table["comparable"]


def test_different_dataset_content_or_scoring_gives_raw_summaries_only():
    a = run("a", [trial("t1", 1.0)])
    other = {**DATASET, "resolved_digest": "sha256:b"}
    b = run("b", [trial("t1", 0.0)], dataset=other)
    table = compare([a, b])
    assert not table["comparable"] and table["blockers"][0]["code"] == "DATASET_DIFFERS"
    assert table["tasks"] == []
    assert all(r["difference"] is None for r in table["runs"])
    assert by_id(table)["a"]["mean"] == 1.0
    scored_by_cost = {**DEFAULT_CONTRACT, "metric_id": "cost"}
    table = compare([a, run("c", [trial("t1", 1.0)], contract=scored_by_cost)])
    assert table["blockers"][0]["code"] == "SCORING_DIFFERS"


def test_external_failures_are_excluded_and_the_rest_count_as_zero():
    a = run("a", [trial("t1", 1.0), trial("t2", 1.0), trial("t3", 1.0)])
    b = run(
        "b",
        [
            trial("t1", 1.0),
            error("t2", "provider_transient", "ApiRateLimitError"),  # excluded: drops t2
            error("t3", "sandbox_failed", "MemoryLimitExceededError"),  # kept: counts as 0
        ],
        model="haiku",
    )
    table = compare([a, b])
    rows = by_id(table)
    assert table["dropped_tasks"] == 1
    statuses = {x["task_key"]: x["status"] for x in table["tasks"]}
    assert statuses == {"t1": "compared", "t2": "no_valid_trials", "t3": "compared"}
    assert rows["b"]["task_count"] == 2 and rows["b"]["mean"] == 0.5
    assert rows["b"]["excluded_by_group"] == {"provider_unavailable": 1}
    assert rows["b"]["failed_by_group"] == {"out_of_memory": 1}
    assert rows["b"]["pass_rate"] == 0.5
    # The same run is dropped from every column, so A is compared on the same two tasks.
    assert rows["a"]["task_count"] == 2
    # Excluding nothing makes the rate-limited trial a failure too.
    rows = by_id(compare([a, b], exclude=[]))
    assert rows["b"]["task_count"] == 3 and rows["b"]["failed_trials"] == 2


def test_a_partly_excluded_task_keeps_its_other_repetitions():
    a = run("a", [trial("t1", 1.0, rep=1), trial("t1", 0.0, rep=2)])
    b = run(
        "b",
        [trial("t1", 1.0, rep=1), error("t1", "provider_transient", rep=2)],
        model="haiku",
    )
    table = compare([a, b])
    cell = table["tasks"][0]["cells"]["b"]
    assert cell["valid_trials"] == 1 and cell["excluded_trials"] == 1 and cell["mean"] == 1.0
    assert table["dropped_tasks"] == 0


def test_task_scope_common_or_all():
    a = run("a", [trial("t1", 1.0), trial("t2", 1.0)])
    b = run("b", [trial("t1", 0.0)], model="haiku")
    table = compare([a, b])
    assert table["common_tasks"] == 1 and table["all_tasks"] == 2
    assert {"code": "TASK_SETS_DIFFER", "common": 1, "all": 2} in table["warnings"]
    assert by_id(table)["a"]["task_count"] == 1
    everything = compare([a, b], scope="all")
    assert by_id(everything)["a"]["task_count"] == 2
    assert all(r["difference"] is None for r in everything["runs"])
    assert everything["tasks"][1]["cells"]["b"] is None


def test_a_rerun_is_its_own_row():
    a = run("a", [trial("t1", 1.0)])
    again = run("a2", [trial("t1", 0.0)])
    table = compare([a, again], expected_variable=())
    assert table["varying"] == []
    assert [r["mean"] for r in table["runs"]] == [1.0, 0.0]


def test_failure_groups():
    assert failure_group(error("t", "sandbox_failed", "SandboxBuildFailedError")) == (
        "environment_setup"
    )
    assert failure_group(error("t", "provider_permanent", "ApiUsageLimitError")) == (
        "provider_access"
    )
    assert failure_group(error("t", "provider_permanent", "BadRequestError")) == (
        "provider_request"
    )
    assert failure_group(trial("t", state="not_started", cause="run_deadline_exceeded")) == (
        "platform_stop"
    )
    assert failure_group(trial("t", state="cancelled")) == "platform_stop"
    assert failure_group(error("t", "preparation_restore_failed")) == "preparation"
    assert failure_group(error("t", "unclassified", "Weird")) == "unclassified"
    assert "out_of_memory" not in DEFAULT_EXCLUDED and "platform_stop" in DEFAULT_EXCLUDED


def test_lower_is_better_failures_only_count_as_not_passed():
    contract = {**DEFAULT_CONTRACT, "direction": "lower_is_better", "passed": None}
    a = run("a", [trial("t1", 3.0), error("t1", "verifier_failed", rep=2)], contract=contract)
    table = compare([a])
    assert by_id(table)["a"]["mean"] == 3.0
    assert {"code": "FAILURES_NOT_IN_MEAN"} in table["warnings"]


def test_running_runs_are_marked():
    a = run("a", [trial("t1", 1.0), trial("t2", state="executing")], terminal=False)
    table = compare([a])
    assert table["warnings"][0]["code"] == "RUN_IN_PROGRESS"
    assert by_id(table)["a"]["pending_trials"] == 1


def test_a_run_conforms_to_fixed_dimensions_or_not():
    base = dimensions_of(view(), DEFAULT_CONTRACT)
    fixed = {k: v for k, v in base.items() if k != "model"}
    assert nonconforming(fixed, dimensions_of(view(model="haiku"), DEFAULT_CONTRACT)) == []
    found = nonconforming(fixed, dimensions_of(view(overrides={"cpus": 2}), DEFAULT_CONTRACT))
    assert found == [{"dimension": "resources", "path": "/overrides/cpus"}]


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (name, "provider_unavailable" if rule == "provider_transient" else "provider_access")
        for name, rule in HARBOR_EXCEPTIONS.items()
        if rule in {"provider_transient", "provider_auth"}
    ],
)
def test_real_provider_outcomes_keep_their_failure_group(exception, expected):
    outcome = harbor_outcome(exception, "provider error", None, False)
    assert outcome.failure_cause == "model_provider_failed"
    assert failure_group(error("t", outcome.failure_cause, outcome.exception_type)) == expected


@pytest.mark.parametrize(
    ("before", "after", "pointer"),
    [
        ({}, {"temperature": None}, "/model/params/temperature"),
        ({"enabled": False}, {"enabled": 0}, "/model/params/enabled"),
        ({"values": [True]}, {"values": [1]}, "/model/params/values/0"),
        ({"a/b~c": None}, {}, "/model/params/a~1b~0c"),
    ],
)
def test_fixed_conditions_distinguish_missing_null_and_json_types(before, after, pointer):
    model = view()["model"]
    original = dimensions_of(view(model={**model, "params": before}), DEFAULT_CONTRACT)
    changed = dimensions_of(view(model={**model, "params": after}), DEFAULT_CONTRACT)
    assert nonconforming(original, changed) == [{"dimension": "model", "path": pointer}]
    table = compare(
        [
            run("a", [trial("t", 1)], model={**model, "params": before}),
            run("b", [trial("t", 1)], model={**model, "params": after}),
        ]
    )
    dimension = next(d for d in table["dimensions"] if d["key"] == "model")
    assert dimension["varies"] and dimension["paths"] == [pointer]
    assert dimension["values"]["a"] == original["model"]
    assert dimension["values"]["b"] == changed["model"]


@pytest.mark.parametrize("exclude", [[], list(DEFAULT_EXCLUDED)])
def test_incomparable_runs_keep_raw_trial_statistics_and_never_count_failures_as_zero(exclude):
    a = run(
        "a",
        [
            trial("t1", 1, rep=1),
            trial("t1", 1, rep=2),
            trial("t2", 0),
            error("t2", "sandbox_failed", "MemoryLimitExceededError", rep=2),
            error("t3", "model_provider_failed", "ApiRateLimitError"),
        ],
    )
    b = run("b", [trial("t1", 0)], dataset={**DATASET, "resolved_digest": "sha256:b"})
    table = compare([a, b], exclude=exclude)
    row = by_id(table)["a"]
    assert not table["comparable"] and not table["tasks"]
    assert row["mean"] == pytest.approx(2 / 3)  # raw trial mean, not task macro mean
    assert row["stddev"] == pytest.approx(1 / 3**0.5)
    assert row["pass_rate"] == pytest.approx(2 / 3)
    assert row["task_count"] == 3 and row["compared_trials"] == 5
    assert row["valid_trials"] == 3 and row["failed_trials"] == 0
    assert row["excluded_trials"] == 2 and row["difference"] is None
