"""The confirmed scoring contract's metric selects the score (06 section 2).

Before, the executor adapter always scored the ``reward`` key (or a lone value), so a
contract naming ``accuracy`` labelled the summary accuracy while scoring something else.
"""

import asyncio
import stat
import sys

import pytest
from sqlalchemy import select
from support import invite_member, running, setup_admin, submit, wait_operation
from test_harbor_executor import Cancel, RecordingDocker, RecordingSink, finished

from maestro.boundaries.executor import RunSpec, TrialSpec
from maestro.db import schema as t
from maestro.domain.scoring import DEFAULT_CONTRACT
from maestro.execution.harbor import HarborExecutor
from maestro.execution.materials import TaskMaterials, tree_digest
from maestro.execution.policy import harbor_outcome, primary_score
from maestro.services.worker import trial_outcome
from maestro.testing.fakes import FakeExecutor

REWARDS = {"accuracy": 0.5, "f1": 0.7}


def test_the_contract_metric_selects_the_score():
    outcome = harbor_outcome(None, None, REWARDS, True, metric_id="accuracy")
    assert outcome.raw_score == 0.5 and outcome.reward_valid
    assert trial_outcome(outcome) == ("scored", None)
    assert outcome.metrics == REWARDS  # the other rewards stay as evidence
    assert harbor_outcome(None, None, REWARDS, True, metric_id="f1").raw_score == 0.7
    # The chosen metric wins over a ``reward`` key of the same verifier output.
    both = harbor_outcome(None, None, {"reward": 1.0, "accuracy": 0.3}, True, metric_id="accuracy")
    assert both.raw_score == 0.3 and both.reward_valid


@pytest.mark.parametrize(
    "rewards",
    [
        {"reward": 1.0},  # a lone value is not the metric the contract named
        {"reward": 1.0, "f1": 0.7},
        {"accuracy": float("nan"), "reward": 1.0},
        {"accuracy": None},
        {"accuracy": True},
        {},
        None,
    ],
)
def test_a_missing_or_non_finite_chosen_metric_is_no_usable_reward(rewards):
    outcome = harbor_outcome(None, None, rewards, True, metric_id="accuracy")
    assert outcome.raw_score is None and not outcome.reward_valid
    assert trial_outcome(outcome) == ("execution_error", "verifier_failed")
    assert outcome.metrics == (rewards or {})  # every reported reward stays as evidence


def test_a_chosen_metric_does_not_rescue_an_invalid_reward():
    outcome = harbor_outcome("ApiRateLimitError", "x", REWARDS, True, metric_id="accuracy")
    assert outcome.raw_score == 0.5 and not outcome.reward_valid
    timeout = harbor_outcome("AgentTimeoutError", "x", {"reward": 1.0}, True, metric_id="accuracy")
    assert timeout.raw_score is None
    assert trial_outcome(timeout) == ("execution_error", "agent_exhausted_budget")
    ok = harbor_outcome("AgentTimeoutError", "x", {"accuracy": 0.0}, True, metric_id="accuracy")
    assert trial_outcome(ok) == ("scored", "agent_exhausted_budget") and ok.raw_score == 0.0


def test_the_default_metric_is_unchanged():
    assert DEFAULT_CONTRACT["metric_id"] == "reward"
    assert RunSpec.__dataclass_fields__["metric_id"].default == "reward"
    assert primary_score({"reward": 0.25}) == 0.25
    assert primary_score({"reward": 0.25, "other": 9}) == 0.25
    assert primary_score({"only": 0.4}) == 0.4  # the lone-value rule belongs to ``reward``
    assert primary_score({"a": 1, "b": 2}) is None
    assert harbor_outcome(None, None, {"only": 0.4}, True).raw_score == 0.4
    assert harbor_outcome(None, None, {"only": 0.4}, True, metric_id="reward").raw_score == 0.4
    named = harbor_outcome(None, None, {"only": 0.4}, True, metric_id="accuracy")
    assert named.raw_score is None and not named.reward_valid


# --- the adapter hands the run's metric to the policy ----------------------------------

RUNNER = """\
import json, sys
spec = json.loads(sys.stdin.read())
n = 0
def emit(kind, **payload):
    global n
    n += 1
    print(json.dumps({"protocol_version": 2, "run_id": spec["run_id"], "sequence": n,
                      "type": kind, "at": "2026-10-23T00:00:00+00:00", "payload": payload}),
          flush=True)
emit("started", harbor_version="0.24.0", runner_version="fake", n_trials=1)
for task in spec["tasks"]:
    for trial_id in task["trial_ids"]:
        emit("attempt_started", trial_id=trial_id, attempt=1)
        emit("attempt_finished", trial_id=trial_id, attempt=1, exception_type=None,
             exception_message=None, rewards={"accuracy": 0.5, "f1": 0.7}, verifier_ran=True,
             will_retry=False, archive=None, files=[], timing={})
emit("completed", status="completed")
"""


def adapter_outcome(tmp_path, **spec_fields):
    runner = tmp_path / "runner.py"
    runner.write_text(RUNNER)
    python = tmp_path / "runner-python"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{runner}"\n')
    python.chmod(python.stat().st_mode | stat.S_IXUSR)
    materials = TaskMaterials(tmp_path / "cache")
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.toml").write_text('schema_version = "1.4"\n')
    digest = tree_digest(task)
    spec = RunSpec(
        run_id="run_metric",
        trials=(TrialSpec("trl_1", "metric", {}, 1, digest, materials.install(task, digest)),),
        concurrency=1,
        effective_config={"execution_mode": "oracle"},
        deadline_at="2099-01-01T00:00:00Z",
        labels={"io.maestro.run_id": "metric"},
        **spec_fields,
    )
    executor = HarborExecutor(python, tmp_path / "work", materials, RecordingDocker())
    sink = RecordingSink()
    asyncio.run(executor.execute(spec, sink, Cancel()))
    return finished(sink)["trl_1"][3]


def test_the_adapter_scores_the_metric_of_the_run_spec(tmp_path):
    outcome = adapter_outcome(tmp_path, metric_id="accuracy")
    assert outcome.raw_score == 0.5 and outcome.reward_valid


def test_the_adapter_default_metric_finds_no_reward_in_accuracy_output(tmp_path):
    outcome = adapter_outcome(tmp_path)
    assert outcome.raw_score is None and outcome.failure_cause == "verifier_failed"
    assert outcome.metrics == REWARDS


# --- from the confirmed contract to the executor and the summary ------------------------


@pytest.fixture
def executor():
    return FakeExecutor()  # records the specs it was given


def finished_run(api, version_id) -> str:
    accepted = submit(api, version_id)
    assert accepted.status_code == 202, accepted.text
    wait_operation(api, accepted.json()["data"]["operation_id"])
    return accepted.json()["data"]["run_id"]


def test_a_confirmed_metric_reaches_the_executor_and_scores_the_run(service, version_id, executor):
    executor.script = {key: [REWARDS] for key in ("add-01", "add-02", "mul-01")}
    with running(service) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "member")
        url = f"/api/v1/admin/dataset-versions/{version_id}/scoring-contract"
        before = finished_run(member, version_id)  # platform default: metric ``reward``
        admin.put(
            url,
            json={"metric_id": "accuracy", "direction": "higher_is_better", "pass_threshold": 0.5},
            expect=200,
        )
        after = finished_run(member, version_id)

        default_run = member.get(f"/api/v1/runs/{before}/summary", expect=200).json()["data"]
        assert default_run["contract"]["metric_id"] == "reward"
        assert default_run["scored_trials"] == 0 and default_run["excluded_by_cause"] == {
            "verifier_failed": 3
        }

        summary = member.get(f"/api/v1/runs/{after}/summary", expect=200).json()["data"]
        assert summary["contract"]["metric_id"] == "accuracy"
        assert (summary["scored_trials"], summary["excluded_error_trials"]) == (3, 0)
        assert summary["mean"] == 0.5 and summary["passed_trials"] == 3 and summary["stddev"] == 0.0
        trials = member.get(f"/api/v1/runs/{after}/trials", expect=200).json()["data"]
        assert {x["result"]["score"] for x in trials} == {0.5}
        listed = member.get("/api/v1/runs", expect=200).json()["data"]
        assert {x["id"]: x["scoring"]["metric_id"] for x in listed} == {
            before: "reward",
            after: "accuracy",
        }
    # The run snapshot fixed the metric before execution: both specs say what they scored by.
    assert [spec.metric_id for spec in executor.specs] == ["reward", "accuracy"]
    with service.db.read() as conn:
        stored = conn.execute(select(t.trial_results.c.metrics_json)).scalars().all()
    assert {"accuracy": 0.5, "f1": 0.7} in stored  # every reward is kept as evidence
