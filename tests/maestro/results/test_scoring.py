"""Counts, coverage and statistics from trial records."""

import math

import pytest

from maestro.domain.scoring import DEFAULT_CONTRACT, TrialRecord, contract_for, summarize


def rec(task, state, score=None, cause=None, attempts=1, usage=None):
    return TrialRecord(task, state, score, cause, attempts, usage)


def test_worked_example_of_section_6():
    trials = [
        rec("t1", "scored", 1.0),
        rec("t2", "scored", 0.0),
        rec("t3", "scored", 1.0),
        rec("t4", "scored", 0.0, cause="agent_exhausted_budget"),  # timeout, valid 0
        rec("t5", "execution_error", None, cause="sandbox_failed"),
    ]
    s = summarize(trials, DEFAULT_CONTRACT, bench_task_count=10)
    assert (s["planned_trials"], s["scored_trials"], s["count_as_failure_trials"]) == (5, 4, 0)
    assert (s["excluded_error_trials"], s["unfinished_trials"], s["passed_trials"]) == (1, 0, 2)
    assert s["mean"] == 0.5 and s["pass_rate"] == 0.5
    assert s["scored_coverage"] == 0.8 == s["evaluation_coverage"]
    assert s["bench_task_coverage"] == 0.5
    assert s["excluded_by_cause"] == {"sandbox_failed": 1}
    assert s["result_completeness"] == "partial"
    terminal = (
        s["scored_trials"]
        + s["count_as_failure_trials"]
        + s["excluded_error_trials"]
        + s["unfinished_trials"]
    )
    assert terminal == s["planned_trials"]


def test_invalid_reward_never_counts_even_with_a_number():
    # A provider error after which the verifier still wrote 1: diagnostic only.
    s = summarize(
        [rec("t1", "execution_error", 1.0, "model_provider_failed"), rec("t2", "scored", 0.0)],
        DEFAULT_CONTRACT,
    )
    assert s["scored_trials"] == 1 and s["mean"] == 0.0 and s["passed_trials"] == 0


def test_all_excluded_gives_nulls_not_zero_division():
    s = summarize(
        [rec("t1", "execution_error", cause="verifier_failed"), rec("t2", "cancelled")],
        DEFAULT_CONTRACT,
    )
    assert s["mean"] is None and s["stddev"] is None and s["pass_rate"] is None
    assert s["scored_coverage"] == 0 and s["bench_score"] is None
    assert s["result_completeness"] == "none" and s["unfinished_trials"] == 1
    empty = summarize([], DEFAULT_CONTRACT)
    assert empty["scored_coverage"] is None and empty["mean"] is None


def test_one_sample_has_no_stddev_and_low_coverage_is_visible():
    trials = [rec("t1", "scored", 1.0)] + [rec(f"t{i}", "not_started") for i in range(2, 6)]
    s = summarize(trials, DEFAULT_CONTRACT)
    assert s["stddev"] is None and s["mean"] == 1.0
    assert s["scored_coverage"] == pytest.approx(0.2)


def test_sample_stddev_uses_n_minus_one():
    s = summarize([rec("t", "scored", v) for v in (1.0, 2.0, 3.0, 4.0)], DEFAULT_CONTRACT)
    assert s["stddev"] == pytest.approx(math.sqrt(5 / 3))


def test_task_means_partial_flag_and_macro_bench_score():
    trials = [
        rec("a", "scored", 1.0),
        rec("a", "scored", 0.0),
        rec("b", "scored", 1.0),
        rec("b", "execution_error", cause="sandbox_failed"),
        rec("c", "execution_error", cause="sandbox_failed"),
    ]
    s = summarize(trials, DEFAULT_CONTRACT)
    tasks = {x["task_key"]: x for x in s["tasks"]}
    assert tasks["a"]["mean"] == 0.5 and not tasks["a"]["partial"]
    assert tasks["b"]["mean"] == 1.0 and tasks["b"]["partial"]
    assert tasks["c"]["mean"] is None and tasks["c"]["failure_causes"] == ["sandbox_failed"]
    # Macro mean over tasks with a score: (0.5 + 1.0) / 2, not the trial mean 2/3.
    assert s["bench_score"] == 0.75 and s["bench_scored_tasks"] == 2
    assert s["mean"] == pytest.approx(2 / 3)


def test_retries_count_attempts_not_samples():
    s = summarize([rec("t", "scored", 1.0, attempts=3)], DEFAULT_CONTRACT)
    assert s["planned_trials"] == 1 and s["retry_count"] == 2 and s["retried_trials"] == 1


def test_running_runs_keep_active_and_queued_apart():
    s = summarize(
        [rec("a", "scored", 1.0), rec("b", "executing"), rec("c", "queued")], DEFAULT_CONTRACT
    )
    assert (s["active_trials"], s["queued_trials"], s["unfinished_trials"]) == (1, 1, 0)


def test_usage_reports_known_total_and_unknown_count():
    s = summarize(
        [
            rec("a", "scored", 1.0, usage={"input_tokens": 10, "cost_usd": 0.5}),
            rec("b", "scored", 1.0),
            rec("c", "not_started"),
        ],
        DEFAULT_CONTRACT,
    )
    assert s["usage"] == {
        "known_trials": 1,
        "unknown_trials": 1,
        "known_totals": {"input_tokens": 10, "cost_usd": 0.5},
    }


def test_contracts_come_from_the_version_or_the_default():
    assert contract_for(None)["source"] == "platform_default"
    declared = contract_for(
        {
            "scoring_contract": {
                "metric_id": "errors",
                "direction": "lower_is_better",
                "passed": {"rule": "threshold", "threshold": 0},
            }
        }
    )
    assert declared["source"] == "dataset_version" and declared["metric_id"] == "errors"
    s = summarize([rec("t", "scored", 0.0), rec("u", "scored", 2.0)], declared)
    assert s["passed_trials"] == 1 and s["pass_rate"] == 0.5
    # Malformed contracts fall back rather than inventing a pass rule.
    bad = contract_for({"scoring_contract": {"passed": {"rule": "above_zero"}}})
    assert bad == DEFAULT_CONTRACT
    no_pass = contract_for({"scoring_contract": {"passed": None}})
    assert summarize([rec("t", "scored", 1.0)], no_pass)["pass_rate"] is None
