"""Scoring contracts: contracts the maths can honour, exact
statistics, started-only usage counts and the planned = S + F + E + U identity."""

import io
import json
import math
import zipfile

import pytest
from support import invite_member, running, setup_admin, submit, wait_operation

from maestro.core.errors import MaestroError
from maestro.domain.scoring import (
    BENCH_AGGREGATION,
    DEFAULT_CONTRACT,
    TASK_AGGREGATION,
    TrialRecord,
    compact,
    contract_errors,
    contract_for,
    mean,
    sample_stddev,
    summarize,
)
from maestro.domain.states import Role, TrialState
from maestro.services.principal import Channel, Principal
from maestro.testing.fakes import failed


def rec(task, state, score=None, cause=None, attempts=1, usage=None):
    return TrialRecord(task, state, score, cause, attempts, usage)


def declared(**fields):
    return {"scoring_contract": {"direction": "higher_is_better", "passed": None, **fields}}


# --- 2. only rules the maths implements -------------------------------------------------


def test_the_supported_rules_are_the_ones_the_summary_computes():
    assert DEFAULT_CONTRACT["task_aggregation"] == TASK_AGGREGATION
    assert DEFAULT_CONTRACT["bench_aggregation"] == BENCH_AGGREGATION
    assert contract_errors(DEFAULT_CONTRACT) == []
    explicit = contract_for(
        declared(
            task_aggregation=TASK_AGGREGATION,
            bench_aggregation=BENCH_AGGREGATION,
            count_as_failure=[],
            metric_id="accuracy",
        )
    )
    assert explicit["source"] == "dataset_version" and explicit["metric_id"] == "accuracy"


@pytest.mark.parametrize(
    "field",
    [
        {"bench_aggregation": "micro_mean"},
        {"task_aggregation": "max"},
        {"count_as_failure": ["verifier_failed"]},
        {"count_as_failure": None},
        {"contract_version": 2},
        {"metric_id": ""},
        {"metric_id": {"x": 1}},
        {"name": 5},
        {"unit": 5},
        {"direction": ["higher_is_better"]},
        {"passed": {"rule": "threshold", "threshold": True}},
        {"passed": {"rule": "threshold", "threshold": float("nan")}},
        {"passed": {"rule": "above_zero"}},
    ],
)
def test_a_stored_contract_the_maths_cannot_honour_falls_back_to_the_default(field):
    # Echoing "micro_mean" next to a macro mean would mislabel the number.
    assert contract_for(declared(**field)) == DEFAULT_CONTRACT
    summary = summarize(
        [rec("a", "scored", 1.0), rec("a", "scored", 0.0)], contract_for(declared(**field))
    )
    assert summary["contract"]["source"] == "platform_default"


@pytest.fixture
def admin_who(service, version_id):
    with running(service) as client:
        admin = setup_admin(service, client)
        me = admin.get("/api/v1/me").json()["data"]
    return Principal(me["id"], "admin", Role.ADMIN, Channel.WEB_SESSION, session_id="s")


def stored_contract(service, version_id):
    detail = service.catalog.get_version(version_id)
    return detail["scoring_contract"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("bench_aggregation", "micro_mean"),
        ("task_aggregation", "max"),
        ("count_as_failure", ["verifier_failed"]),
    ],
)
def test_the_handler_rejects_rules_the_maths_does_not_apply(
    service, version_id, admin_who, field, value
):
    body = {"direction": "higher_is_better", "pass_threshold": 0.5, field: value}
    with pytest.raises(MaestroError) as raised:
        service.datasets.confirm_scoring_contract(admin_who, version_id, body)
    error = raised.value
    assert error.status == 422 and error.code == "CONFIG_VALIDATION_FAILED"
    assert [f.path for f in error.field_errors] == [f"/{field}"]
    assert stored_contract(service, version_id)["source"] == "platform_default"  # nothing written


def test_the_handler_rejects_bad_fields_with_their_request_paths(service, version_id, admin_who):
    body = {"direction": "sideways", "pass_threshold": float("inf"), "metric_id": "", "name": ""}
    with pytest.raises(MaestroError) as raised:
        service.datasets.confirm_scoring_contract(admin_who, version_id, body)
    assert {f.path for f in raised.value.field_errors} == {
        "/direction",
        "/pass_threshold",
        "/metric_id",
        "/name",
    }
    assert stored_contract(service, version_id)["source"] == "platform_default"


def test_the_handler_accepts_what_the_maths_implements(service, version_id, admin_who):
    body = {
        "metric_id": "accuracy",
        "name": "Accuracy",
        "direction": "higher_is_better",
        "pass_threshold": 0.5,
        "task_aggregation": TASK_AGGREGATION,
        "bench_aggregation": BENCH_AGGREGATION,
        "count_as_failure": [],
    }
    confirmed = service.datasets.confirm_scoring_contract(admin_who, version_id, body)
    assert confirmed["source"] == "dataset_version" and confirmed["metric_id"] == "accuracy"
    assert stored_contract(service, version_id) == confirmed


def test_over_http_unsupported_rules_and_an_empty_metric_are_422(service, version_id):
    with running(service) as client:
        admin = setup_admin(service, client)
        url = f"/api/v1/admin/dataset-versions/{version_id}/scoring-contract"
        ok = {"direction": "higher_is_better", "pass_threshold": 0.5}
        extra = admin.put(url, json={**ok, "bench_aggregation": "micro_mean"})
        assert extra.status_code == 422
        empty = admin.put(url, json={**ok, "metric_id": ""})
        assert empty.status_code == 422
        assert [f["path"] for f in empty.json()["error"]["field_errors"]] == ["/metric_id"]
        assert admin.put(url, json={**ok, "metric_id": "accuracy"}).status_code == 200
        member = invite_member(service, admin, "member")
        detail = member.get(f"/api/v1/dataset-versions/{version_id}", expect=200).json()["data"]
        assert detail["scoring_contract"]["metric_id"] == "accuracy"


# --- 3. exact statistics and started-only usage ----------------------------------------


def test_identical_scores_average_to_themselves_with_an_exact_zero_spread():
    for score, n in ((0.1, 3), (0.7, 10), (1 / 3, 7)):
        trials = [rec("t", "scored", score) for _ in range(n)]
        summary = summarize(trials, DEFAULT_CONTRACT)
        assert summary["mean"] == score and summary["stddev"] == 0.0
        task = summary["tasks"][0]
        assert task["mean"] == score and task["stddev"] == 0.0
        assert summary["bench_score"] == score
    macro = summarize([rec(f"t{i}", "scored", 0.1) for i in range(3)], DEFAULT_CONTRACT)
    assert macro["bench_score"] == 0.1 and macro["mean"] == 0.1


def test_statistics_keep_the_n_minus_one_rule_and_null_for_one_sample():
    assert sample_stddev([0.5]) is None and sample_stddev([]) is None
    assert mean([]) is None
    assert sample_stddev([1.0, 2.0, 3.0, 4.0]) == pytest.approx(math.sqrt(5 / 3), abs=1e-15)
    assert sample_stddev([0, 1]) == pytest.approx(math.sqrt(0.5))
    assert mean([1, 0, 1]) == pytest.approx(2 / 3) and isinstance(mean([1, 2, 3]), float)


def test_usage_totals_are_exact_sums():
    trials = [rec(f"t{i}", "scored", 1.0, usage={"cost_usd": 0.1}) for i in range(10)]
    assert summarize(trials, DEFAULT_CONTRACT)["usage"]["known_totals"] == {"cost_usd": 1.0}


def test_usage_counts_only_trials_that_have_started():
    s = summarize(
        [
            rec("a", "scored", 1.0, usage={"cost_usd": 1.0}),
            rec("b", "queued"),
            rec("c", "queued"),
            rec("d", "preparing"),
            rec("e", "not_started"),
            rec("f", "executing"),
            rec("g", "verifying"),
            rec("h", "scored", 0.0),
            rec("i", "execution_error", cause="sandbox_failed"),
            rec("j", "cancelled"),
        ],
        DEFAULT_CONTRACT,
    )
    assert s["usage"] == {"known_trials": 1, "unknown_trials": 5, "known_totals": {"cost_usd": 1.0}}
    waiting = summarize([rec("a", "queued"), rec("b", "preparing")], DEFAULT_CONTRACT)
    assert waiting["usage"] == {"known_trials": 0, "unknown_trials": 0, "known_totals": {}}


# --- 4. accounting identities for terminal and active runs -----------------------------------------------

STATES = [state.value for state in TrialState]


def identity(s):
    return (
        s["scored_trials"]
        + s["count_as_failure_trials"]
        + s["excluded_error_trials"]
        + s["unfinished_trials"]
    )


def test_every_trial_is_counted_once_at_a_terminal_run():
    terminal = ["scored", "execution_error", "not_started", "cancelled", "interrupted"]
    trials = [
        rec(f"t{i}", state, 1.0 if state == "scored" else None, cause="sandbox_failed")
        for i, state in enumerate(terminal * 3)
    ]
    s = summarize(trials, DEFAULT_CONTRACT)
    assert identity(s) == s["planned_trials"] == 15
    assert (s["scored_trials"], s["excluded_error_trials"], s["unfinished_trials"]) == (3, 3, 9)
    assert s["active_trials"] == s["queued_trials"] == 0


def test_a_running_run_balances_with_active_and_queued_trials():
    trials = [
        rec(f"t{i}", state, 1.0 if state == "scored" else None)
        for i, state in enumerate(STATES * 2)
    ]
    s = summarize(trials, DEFAULT_CONTRACT)
    assert identity(s) + s["active_trials"] + s["queued_trials"] == s["planned_trials"]
    assert (s["active_trials"], s["queued_trials"]) == (6, 2)


def test_a_scored_record_without_a_number_is_excluded_not_dropped():
    s = summarize(
        [rec("a", "scored", None), rec("b", "scored", float("nan")), rec("c", "scored", 1.0)],
        DEFAULT_CONTRACT,
    )
    assert (s["scored_trials"], s["excluded_error_trials"], s["mean"]) == (1, 2, 1.0)
    assert identity(s) == s["planned_trials"]
    assert s["excluded_by_cause"] == {"unclassified": 2}
    task = {x["task_key"]: x for x in s["tasks"]}["a"]
    assert task["excluded_trials"] == 1 and task["failure_causes"] == ["unclassified"]
    assert s["result_completeness"] == "partial"


def test_list_detail_summary_and_export_report_the_same_numbers(service, version_id, executor):
    executor.script = {
        "add-01": [{"reward": 1.0}],
        "add-02": [{"reward": 0.0}],
        "mul-01": [failed("sandbox_failed")],
    }
    with running(service) as client:
        admin = setup_admin(service, client)
        accepted = submit(admin, version_id).json()["data"]
        wait_operation(admin, accepted["operation_id"])
        run_id = accepted["run_id"]
        summary = admin.get(f"/api/v1/runs/{run_id}/summary", expect=200).json()["data"]
        assert identity(summary) == summary["planned_trials"] == 3
        assert (summary["scored_trials"], summary["excluded_error_trials"]) == (2, 1)
        assert summary["mean"] == 0.5 and summary["stddev"] == pytest.approx(math.sqrt(0.5))
        detail = admin.get(f"/api/v1/runs/{run_id}", expect=200).json()["data"]
        (listed,) = admin.get("/api/v1/runs", expect=200).json()["data"]
        shown = compact({k: v for k, v in summary.items() if k != "run_id"})
        assert detail["scoring"] == listed["scoring"] == shown
        package = admin.post("/api/v1/exports", json={"run_ids": [run_id]}, expect=202).json()[
            "data"
        ]
        wait_operation(admin, package["operation_id"])
        data = admin.get(f"/api/v1/exports/{package['id']}/download", expect=200).content
    archive = zipfile.ZipFile(io.BytesIO(data))
    (name,) = [n for n in archive.namelist() if n.endswith("/summary.json")]
    assert json.loads(archive.read(name)) == summary
