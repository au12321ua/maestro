"""Run-level execution with the fake executor: results, retries, cancel, restart."""

import asyncio

import pytest
from sqlalchemy import select, update
from support.runs import _drain, accept, trials_by_key

from maestro.boundaries.executor import AttemptOutcome
from maestro.db import schema as t
from maestro.services.capacity import HostCapacity
from maestro.services.worker import RunSink, StaleLease
from maestro.testing.fakes import failed, scored


def test_zero_missing_and_error_are_distinct(service, owner, version_id, executor):
    executor.script = {
        "add-01": [scored(0.0)],
        "add-02": [AttemptOutcome(raw_score=None, reward_valid=True, verifier_status="missing")],
        "mul-01": [
            AttemptOutcome(raw_score=1.0, reward_valid=False, failure_cause="model_provider_failed")
        ],
    }
    data = accept(service, owner, version_id)
    asyncio.run(_drain(service))
    trials = trials_by_key(service, data["run_id"])
    zero = trials["add-01"][0]
    assert zero["state"] == "scored" and zero["result"]["score"] == 0.0
    missing = trials["add-02"][0]
    assert missing["state"] == "execution_error" and missing["result"]["score"] is None
    assert missing["result"]["raw_score"] is None and missing["reason_code"] == "VERIFIER_FAILED"
    invalid = trials["mul-01"][0]
    assert invalid["state"] == "execution_error"
    assert invalid["result"]["raw_score"] == 1.0 and invalid["result"]["score"] is None
    run = service.runs.get(owner, data["run_id"])
    assert run["state"] == "finished" and run["result_completeness"] == "partial"
    assert run["counts"]["scored"] == 1 and run["counts"]["errors"] == 2
    op = service.operations.get(data["operation_id"])
    assert op["status"] == "succeeded" and op["progress"]["terminal"] == 3


def test_all_errors_fail_the_operation_but_finish_the_run(service, owner, version_id, executor):
    executor.default = failed("sandbox_failed")
    data = accept(service, owner, version_id)
    asyncio.run(_drain(service))
    run = service.runs.get(owner, data["run_id"])
    assert run["state"] == "finished" and run["result_completeness"] == "none"
    op = service.operations.get(data["operation_id"])
    assert op["status"] == "failed" and op["error"]["code"] == "ALL_TRIALS_FAILED"


def test_retries_add_attempts_not_trials(service, owner, version_id, executor):
    executor.script = {"add-01": [failed("sandbox_failed", retry=True), scored(1.0)]}
    data = accept(service, owner, version_id, repetitions=2)
    asyncio.run(_drain(service))
    trials = trials_by_key(service, data["run_id"])
    assert len(trials["add-01"]) == 2
    for trial in trials["add-01"]:
        assert trial["state"] == "scored"
        assert [a["attempt_index"] for a in trial["attempts"]] == [1, 2]
        assert trial["attempts"][0]["state"] == "failed"
        assert trial["attempts"][0]["error"]["failure_cause"] == "sandbox_failed"
        assert trial["result"]["attempt_id"] == trial["attempts"][1]["id"]
    assert service.runs.get(owner, data["run_id"])["counts"]["planned"] == 6


def test_artifacts_and_logs_are_indexed(service, owner, version_id, store):
    data = accept(service, owner, version_id)
    asyncio.run(_drain(service))
    artifacts = service.artifacts.for_run(data["run_id"])
    kinds = sorted({a["kind"] for a in artifacts})
    assert kinds == ["log", "output"]
    assert all(a["status"] == "available" and a["digest"].startswith("sha256:") for a in artifacts)
    _, content = service.artifacts.read(next(a["id"] for a in artifacts if a["kind"] == "log"))
    assert content.startswith(b"attempt 1")
    assert all(
        "/" not in a["relative_path"] or not a["relative_path"].startswith("/") for a in artifacts
    )


def test_storage_failure_keeps_score(service, owner, version_id, store):
    store.fail_put = True
    data = accept(service, owner, version_id)
    asyncio.run(_drain(service))
    run = service.runs.get(owner, data["run_id"])
    assert run["counts"]["scored"] == 3 and run["artifact_state"] == "failed"


def test_cancel_running_keeps_scores(service, owner, version_id, executor):
    executor.script = {"mul-01": ["hang"]}
    data = accept(service, owner, version_id, concurrency=3)

    async def scenario():
        worker = service.worker
        worker.start_epoch()
        await worker.tick()
        for _ in range(500):
            states = {x["task_key"]: x["state"] for x in service.runs.trials(data["run_id"])}
            if (
                states["add-01"] == "scored"
                and states["add-02"] == "scored"
                and states["mul-01"] == "executing"
            ):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError(states)
        stopping = service.runs.cancel(owner, data["run_id"])
        # Stop requested but not yet finished: cancel is no longer offered.
        assert stopping["stop_requested"] and "cancel" not in stopping["allowed_actions"]
        await worker.drain()

    asyncio.run(scenario())
    run = service.runs.get(owner, data["run_id"])
    assert run["state"] == "cancelled" and run["stop_reason"] == "USER_CANCELLED"
    states = {
        x["task_key"]: (x["state"], x["reason_code"]) for x in service.runs.trials(data["run_id"])
    }
    assert states == {
        "add-01": ("scored", None),
        "add-02": ("scored", None),
        "mul-01": ("cancelled", "USER_CANCELLED"),
    }
    assert service.operations.get(data["operation_id"])["status"] == "cancelled"


def test_restart_interrupts_claimed_runs_and_keeps_queued(service, owner, version_id, executor):
    executor.script = {"mul-01": ["hang"]}
    claimed = accept(service, owner, version_id, concurrency=3)
    queued = accept(service, owner, version_id)

    async def first_process():
        worker = service.worker
        # Room for the first run's three trials only, so the second one stays queued.
        worker.capacity = HostCapacity(slots=3, cpus=64, memory_mb=1 << 20)
        worker.start_epoch()
        await worker.tick()
        for _ in range(500):
            states = {x["state"] for x in service.runs.trials(claimed["run_id"])}
            if "executing" in states and "scored" in states:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("never executing")

    asyncio.run(first_process())  # loop closes: simulates the process dying
    report = service.worker.start_epoch()
    assert report["interrupted_runs"] == [claimed["run_id"]]
    run = service.runs.get(owner, claimed["run_id"])
    assert run["state"] == "interrupted" and run["stop_reason"] == "SERVICE_RESTARTED"
    states = {
        x["task_key"]: (x["state"], x["reason_code"])
        for x in service.runs.trials(claimed["run_id"])
    }
    assert states["mul-01"] == ("interrupted", "SERVICE_RESTARTED")
    assert states["add-01"][0] == "scored"
    assert service.operations.get(claimed["operation_id"])["status"] == "interrupted"
    assert service.runs.get(owner, queued["run_id"])["state"] == "queued"


def test_stale_lease_events_are_rejected(service, owner, version_id):
    data = accept(service, owner, version_id)
    claimed = service.worker._claim() if service.worker.epoch else None
    if claimed is None:
        service.worker.start_epoch()
        claimed = service.worker._claim()
    run_id, lease, spec = claimed
    sink = RunSink(service.worker, run_id, "not-the-lease")
    with pytest.raises(StaleLease):
        asyncio.run(sink.attempt_started(spec.trials[0].trial_id, 1))
    good = RunSink(service.worker, run_id, lease)
    asyncio.run(good.attempt_started(spec.trials[0].trial_id, 1))
    asyncio.run(good.attempt_finished(spec.trials[0].trial_id, 1, scored(0.5), final=True))
    # A different final result for the same trial is ignored once terminal.
    asyncio.run(good.attempt_finished(spec.trials[0].trial_id, 1, scored(0.9), final=True))
    trial = next(x for x in service.runs.trials(run_id) if x["id"] == spec.trials[0].trial_id)
    assert trial["result"]["score"] == 0.5
    assert data["run_id"] == run_id


def test_deadline_stops_queued_run(service, owner, version_id):
    data = accept(service, owner, version_id)
    with service.db.write() as conn:
        conn.execute(update(t.runs).values(deadline_at="2000-01-01T00:00:00.000000Z"))
    service.worker.start_epoch()
    asyncio.run(service.worker.tick())
    run = service.runs.get(owner, data["run_id"])
    assert run["state"] == "finished" and run["stop_reason"] == "RUN_DEADLINE_EXCEEDED"
    with service.db.read() as conn:
        reasons = set(conn.execute(select(t.trials.c.reason_code)).scalars())
    assert reasons == {"RUN_DEADLINE_EXCEEDED"}
