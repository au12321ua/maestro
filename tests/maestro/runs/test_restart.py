"""Restart reconciliation and Runner process identity."""

import asyncio
import signal
import subprocess
import sys
import uuid

from maestro.execution import process


def accept(service, owner, version_id, **body):
    _, data, _ = service.runs.submit(
        owner,
        uuid.uuid4().hex,
        {"name": "r", "dataset_version_id": version_id, "execution_mode": "oracle", **body},
    )
    return data


def crash_while_running(service, run_id):
    async def first_process():
        service.worker.start_epoch()
        await service.worker.tick()
        for _ in range(500):
            states = {x["state"] for x in service.runs.trials(run_id)}
            if "executing" in states:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("never executing")

    asyncio.run(first_process())  # the loop closes: the process "dies"


def test_restart_stops_leftovers_and_records_cleanup(service, owner, version_id, executor):
    executor.script = {"mul-01": ["hang"]}
    data = accept(service, owner, version_id)
    crash_while_running(service, data["run_id"])
    report = service.worker.start_epoch()
    assert report["interrupted_runs"] == [data["run_id"]]
    assert service.runs.get(owner, data["run_id"])["cleanup_state"] == "pending"
    result = asyncio.run(service.worker.recover())
    assert result == {
        "runs": {data["run_id"]: True},
        "sweep_released": True,
        "preparation": {"temporary_images_removed": []},
    }
    assert executor.reconciled
    assert all(item.attempt_id for item in executor.reconciled)
    leftover = next(item for item in executor.reconciled if item.runner)
    assert leftover.labels["io.maestro.run_id"] == data["run_id"].lower()
    assert leftover.runner["runner_id"] == "fake"
    instance = service.worker.instance_id
    assert executor.cleaned[-1] == {"io.maestro.instance_id": instance}  # instance sweep
    run = service.runs.get(owner, data["run_id"])
    assert run["state"] == "interrupted" and run["cleanup_state"] == "complete"
    cleanup = {x["cleanup_state"] for x in service.runs.trials(data["run_id"])}
    assert cleanup == {"complete"}


def test_unconfirmed_cleanup_is_recorded_as_failed(service, owner, version_id, executor):
    executor.script = {"mul-01": ["hang"]}
    data = accept(service, owner, version_id)
    crash_while_running(service, data["run_id"])
    service.worker.start_epoch()
    executor.release = False
    asyncio.run(service.worker.recover())
    assert service.runs.get(owner, data["run_id"])["cleanup_state"] == "failed"


def test_restart_reconciles_each_attempt_of_a_terminal_run_before_releasing_capacity(
    service, owner, version_id, executor
):
    from maestro.boundaries.environment import CleanupReport
    from maestro.services.capacity import in_use

    data = accept(service, owner, version_id, concurrency=3)
    executor.release = False
    service.worker.start_epoch()
    asyncio.run(service.worker.drain())
    assert service.runs.get(owner, data["run_id"])["state"] == "finished"
    service.worker.start_epoch()
    leftovers = list(service.worker.leftovers)
    assert len(leftovers) == 3 and all(item.attempt_id and item.runner for item in leftovers)
    failed_attempt = leftovers[0].attempt_id

    async def reconcile(item):
        executor.reconciled.append(item)
        return CleanupReport(item.attempt_id != failed_attempt)

    executor.reconcile = reconcile
    executor.release = True
    result = asyncio.run(service.worker.recover())
    assert result["runs"] == {data["run_id"]: False}
    assert len(executor.reconciled) == 3
    with service.db.read() as conn:
        assert in_use(conn)["slots"] == 1
    assert service.runs.get(owner, data["run_id"])["counts"]["scored"] == 3

    async def retry():
        nonlocal failed_attempt
        failed_attempt = None
        service.worker.cleanup_retry_seconds = 0
        await service.worker.tick()

    asyncio.run(retry())
    with service.db.read() as conn:
        assert in_use(conn)["slots"] == 0
    assert service.runs.get(owner, data["run_id"])["cleanup_state"] == "complete"


def test_claimed_run_that_never_started_is_requeued(service, owner, version_id):
    data = accept(service, owner, version_id)
    service.worker.start_epoch()
    assert service.worker._claim()[0] == data["run_id"]
    report = service.worker.start_epoch()  # restart before any attempt began
    assert report["requeued_runs"] == [data["run_id"]]
    run = service.runs.get(owner, data["run_id"])
    assert run["state"] == "queued" and run["stop_reason"] is None
    asyncio.run(service.worker.recover())
    assert service.worker._claim()[0] == data["run_id"]


def test_identity_matches_only_the_same_live_process():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    try:
        recorded = process.identity(child.pid)
        assert process.matches(recorded)
        assert not process.matches({**recorded, "start_ticks": "1"})
        assert not process.matches({**recorded, "boot_id": "another-boot"})
        assert process.kill_recorded(recorded)
        assert child.wait(5) == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
    assert not process.matches(recorded)
    assert not process.kill_recorded(recorded)
    assert not process.kill_recorded(None)
