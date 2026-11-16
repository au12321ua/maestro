"""Capacity accounting: slots, CPU / memory, provider limits, cleanup-blocked capacity."""

import asyncio
import uuid

import pytest
from sqlalchemy import select, update
from support import Api, running, setup_admin
from support.runs import dispatch_until

from maestro.db import schema as t
from maestro.services import scheduling
from maestro.services.capacity import HostCapacity, in_use


def accept(service, owner, version_id, **body):
    _, data, _ = service.runs.submit(
        owner,
        uuid.uuid4().hex,
        {"name": "r", "dataset_version_id": version_id, "execution_mode": "oracle", **body},
    )
    return data


def use(service, slots, cpus=64.0, memory_mb=1 << 20):
    host = HostCapacity(slots=slots, cpus=cpus, memory_mb=memory_mb)
    service.worker.capacity = host
    service.runs.capacity = host
    service.capacity.host = host


def set_provider(service, run_id, provider):
    with service.db.write() as conn:
        effective = conn.execute(
            select(t.run_snapshots.c.effective_json).where(t.run_snapshots.c.run_id == run_id)
        ).scalar()
        conn.execute(
            update(t.run_snapshots)
            .where(t.run_snapshots.c.run_id == run_id)
            .values(
                effective_json={
                    **effective,
                    "execution_mode": "agent",
                    "model": {"provider": provider},
                }
            )
        )


def one(service, owner, version_id, **body):
    with service.db.read() as conn:
        task_id = conn.execute(select(t.tasks.c.id).where(t.tasks.c.task_key == "add-01")).scalar()
    return accept(
        service,
        owner,
        version_id,
        task_selection={"mode": "explicit", "task_ids": [task_id]},
        **body,
    )


def holds(service):
    with service.db.read() as conn:
        return in_use(conn)


def test_trials_take_available_capacity_without_reserving_run_width(
    service, owner, version_id, executor
):
    executor.script = {key: ["hang"] for key in ("add-01", "add-02", "mul-01")}
    use(service, slots=4)
    first = accept(service, owner, version_id, concurrency=3)
    second = accept(service, owner, version_id, concurrency=2)

    async def scenario():
        service.worker.start_epoch()
        await dispatch_until(service, lambda: holds(service)["slots"] == 4)
        assert sum(t["state"] == "executing" for t in service.runs.trials(first["run_id"])) == 3
        assert sum(t["state"] == "executing" for t in service.runs.trials(second["run_id"])) == 1
        assert service.runs.get(owner, second["run_id"])["queue_reason"] == "CAPACITY_SLOTS"
        for run in (first, second):
            service.runs.cancel(owner, run["run_id"])
        await service.worker.drain()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("cpus", "memory", "reason"), [(3, 8192, "CAPACITY_CPU"), (8, 6144, "CAPACITY_MEMORY")]
)
def test_cpu_and_memory_count_individual_attempts(
    service, owner, version_id, executor, cpus, memory, reason
):
    executor.script = {key: ["hang"] for key in ("add-01", "add-02", "mul-01")}
    use(service, slots=10, cpus=cpus, memory_mb=memory)
    first = accept(service, owner, version_id, concurrency=2)
    second = accept(service, owner, version_id, concurrency=2)

    async def scenario():
        service.worker.start_epoch()
        await dispatch_until(service, lambda: holds(service)["slots"] == 3)
        assert holds(service)["cpus"] == 3 and holds(service)["memory_mb"] == 6144
        assert service.runs.get(owner, second["run_id"])["queue_reason"] == reason
        for run in (first, second):
            service.runs.cancel(owner, run["run_id"])
        await service.worker.drain()

    asyncio.run(scenario())


def test_provider_limit_counts_model_phases_across_runs(service, owner, version_id, executor):
    executor.script = {key: ["hang"] for key in ("add-01", "add-02", "mul-01")}
    use(service, slots=10)
    runs = [accept(service, owner, version_id, concurrency=2) for _ in range(3)]
    for run, provider in zip(runs, ("acme", "acme", "zeta"), strict=True):
        set_provider(service, run["run_id"], provider)
    with service.db.write() as conn:
        conn.execute(
            t.provider_limits.insert().values(
                provider="acme", max_concurrent_trials=3, updated_by="usr_owner", updated_at="t"
            )
        )

    async def scenario():
        service.worker.start_epoch()
        await dispatch_until(service, lambda: holds(service)["slots"] == 5)
        assert holds(service)["providers"] == {"acme": 3, "zeta": 2}
        for run in runs:
            service.runs.cancel(owner, run["run_id"])
        await service.worker.drain()

    asyncio.run(scenario())


def test_admission_checks_one_environment_group_instead_of_concurrency(service, owner, version_id):
    use(service, slots=2, cpus=2, memory_mb=4096)
    assert accept(service, owner, version_id, concurrency=3)["state"] == "queued"
    with pytest.raises(Exception) as caught:
        accept(service, owner, version_id, resources={"cpus": 3})
    assert caught.value.code == "RUN_EXCEEDS_CAPACITY"


def test_packing_combines_complementary_cpu_and_memory_requests():
    candidates = [
        {"need": {"cpus": cpu, "memory_mb": mem}} for cpu, mem in ((6, 2), (2, 6), (6, 6))
    ]
    assert scheduling.pack(candidates, 2, 8, 8, 8, 8) == candidates[:2]


def test_finite_backfill_drains_capacity_for_an_older_request(
    service, owner, version_id, executor, monkeypatch
):
    executor.script = {"add-01": ["hang"]}
    use(service, slots=10, cpus=4, memory_mb=16384)
    blocker = one(service, owner, version_id, resources={"cpus": 2}, concurrency=1)
    large = one(service, owner, version_id, resources={"cpus": 3}, concurrency=1)
    small = one(service, owner, version_id, repetitions=3, concurrency=3)
    monkeypatch.setattr(scheduling, "PROTECT_AFTER_BYPASSES", 2)

    async def scenario():
        service.worker.start_epoch()
        await dispatch_until(service, lambda: holds(service)["cpus"] == 4)
        assert service.runs.trials(large["run_id"])[0]["attempts"] == []
        assert any(
            t["attempts"] and t["attempts"][0]["backfilled"]
            for t in service.runs.trials(small["run_id"])
        )
        service.runs.cancel(owner, small["run_id"])
        await dispatch_until(
            service, lambda: service.runs.get(owner, small["run_id"])["state"] == "cancelled"
        )
        later = one(service, owner, version_id)
        await dispatch_until(
            service,
            lambda: service.runs.trials(later["run_id"])[0]["queue_reason"] == "WAITING_PRIORITY",
        )
        assert holds(service)["cpus"] == 2
        service.runs.cancel(owner, blocker["run_id"])
        await dispatch_until(
            service, lambda: service.runs.trials(large["run_id"])[0]["state"] == "executing"
        )
        for run in (large, later):
            service.runs.cancel(owner, run["run_id"])
        await service.worker.drain()

    asyncio.run(scenario())


def test_unreleased_resources_keep_capacity_until_cleanup_succeeds(
    service, owner, version_id, executor
):
    use(service, slots=3)
    executor.release = False
    first = accept(service, owner, version_id, concurrency=3)
    second = accept(service, owner, version_id, concurrency=3)

    async def scenario():
        worker = service.worker
        worker.start_epoch()
        worker._loop = asyncio.get_running_loop()
        await worker.tick()
        while service.runs.get(owner, first["run_id"])["state"] != "finished":
            await asyncio.sleep(0.01)
        await worker.tick()
        assert service.runs.get(owner, first["run_id"])["cleanup_state"] == "failed"
        assert service.runs.get(owner, second["run_id"])["queue_reason"] == "CAPACITY_SLOTS"
        executor.release = True
        worker.cleanup_retry_seconds = 0
        await worker.drain()

    asyncio.run(scenario())
    assert service.runs.get(owner, first["run_id"])["cleanup_state"] == "complete"
    assert service.runs.get(owner, second["run_id"])["state"] == "finished"


def test_admin_sets_provider_limits_on_the_web_only(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        view = admin.get("/api/v1/admin/capacity", expect=200).json()["data"]
        assert view["host"]["slots"] == 10 and view["providers"] == []
        body = {"max_concurrent_trials": 4}
        view = admin.put("/api/v1/admin/capacity/providers/acme", json=body, expect=200).json()
        assert view["data"]["providers"] == [
            {"provider": "acme", "max_concurrent_trials": 4, "in_use": 0}
        ]
        bad = {"max_concurrent_trials": 0}
        admin.put("/api/v1/admin/capacity/providers/acme", json=bad, expect=422)
        token = admin.post(
            "/api/v1/me/pats", json={"name": "p", "permissions": ["experiment"]}, expect=201
        ).json()["data"]["token"]
        pat = Api(client, token=token)
        pat.get("/api/v1/admin/capacity", expect=403)
        cleared = admin.put(
            "/api/v1/admin/capacity/providers/acme",
            json={"max_concurrent_trials": None},
            expect=200,
        ).json()["data"]
        assert cleared["providers"] == []


def stages_for(service, run_id, execution, verification=None):
    """Build a frozen legacy-style total with explicit per-container baselines."""
    with service.db.write() as conn:
        effective = conn.execute(
            select(t.run_snapshots.c.effective_json).where(t.run_snapshots.c.run_id == run_id)
        ).scalar()
        entries = [
            {
                "role": "environment/main",
                "cpus": execution[0],
                "memory_mb": execution[1],
                "default": False,
            }
        ]
        if verification:
            entries.append(
                {
                    "role": "verifier",
                    "cpus": verification[0],
                    "memory_mb": verification[1],
                    "default": False,
                }
            )
        settings = effective["tasks"][0]
        settings.update(
            environments=entries,
            cpus=sum(e["cpus"] for e in entries),
            memory_mb=sum(e["memory_mb"] for e in entries),
        )
        conn.execute(
            update(t.run_snapshots)
            .where(t.run_snapshots.c.run_id == run_id)
            .values(effective_json=effective)
        )


@pytest.mark.parametrize("separate", [False, True])
def test_stage_handoff_returns_only_stopped_environments(
    service, owner, version_id, executor, separate
):
    from maestro.boundaries.executor import ExecutionReport
    from maestro.testing.fakes import scored

    use(service, slots=2, cpus=4, memory_mb=8192)
    first = one(service, owner, version_id)
    second = one(service, owner, version_id, resources={"cpus": 3, "memory_mb": 6144})
    stages_for(service, first["run_id"], (4, 8192), (1, 2048) if separate else None)
    transition, finish = asyncio.Event(), asyncio.Event()
    seen = {}

    async def execute_stage(spec, sink, cancel):
        await sink.attempt_started(spec.trials[0].trial_id, 1)
        permit = await sink.resource_requested("execution")
        assert permit == await sink.resource_requested("execution")
        seen[spec.run_id] = "execution"
        if spec.run_id == first["run_id"]:
            await transition.wait()
            await sink.model_finished()
            if separate:
                await sink.resource_released("execution", permit)
                await sink.resource_released("execution", permit)
            if separate:
                assert holds(service)["cpus"] == 1
                await sink.resource_requested("verification")
            seen[spec.run_id] = "verification"
        await finish.wait()
        await sink.attempt_finished(spec.trials[0].trial_id, 1, scored(1), True)
        return ExecutionReport(cleanup_complete=True)

    executor.execute = execute_stage

    async def scenario():
        service.worker.start_epoch()
        await dispatch_until(service, lambda: seen.get(first["run_id"]) == "execution")
        assert holds(service)["cpus"] == 4
        transition.set()
        await dispatch_until(service, lambda: seen.get(first["run_id"]) == "verification")
        if separate:
            await dispatch_until(service, lambda: seen.get(second["run_id"]) == "execution")
            assert holds(service) == {"slots": 2, "cpus": 4.0, "memory_mb": 8192, "providers": {}}
        else:
            assert second["run_id"] not in seen
            assert holds(service)["cpus"] == 4
        finish.set()
        await service.worker.drain()
        assert holds(service)["slots"] == holds(service)["cpus"] == 0

    asyncio.run(scenario())


def test_larger_verifier_waits_without_holding_execution_resources(
    service, owner, version_id, executor
):
    from maestro.boundaries.executor import ExecutionReport
    from maestro.testing.fakes import scored

    use(service, slots=2, cpus=4, memory_mb=8192)
    first = one(service, owner, version_id)
    one(service, owner, version_id, resources={"cpus": 3, "memory_mb": 6144})
    stages_for(service, first["run_id"], (1, 2048), (4, 8192))
    transition, finish_other, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    seen = {}

    async def execute_stage(spec, sink, cancel):
        await sink.attempt_started(spec.trials[0].trial_id, 1)
        permit = await sink.resource_requested("execution")
        seen[spec.run_id] = "execution"
        if spec.run_id == first["run_id"]:
            await transition.wait()
            await sink.resource_released("execution", permit)
            seen[spec.run_id] = "waiting"
            await sink.resource_requested("verification")
            seen[spec.run_id] = "verification"
            await finish.wait()
        else:
            await finish_other.wait()
        await sink.attempt_finished(spec.trials[0].trial_id, 1, scored(1), True)
        return ExecutionReport(cleanup_complete=True)

    executor.execute = execute_stage

    async def scenario():
        service.worker.start_epoch()
        await dispatch_until(service, lambda: len(seen) == 2)
        transition.set()
        await dispatch_until(service, lambda: seen[first["run_id"]] == "waiting")
        assert holds(service)["cpus"] == 3 and holds(service)["slots"] == 2
        finish_other.set()
        await dispatch_until(service, lambda: seen[first["run_id"]] == "verification")
        assert holds(service)["cpus"] == 4
        finish.set()
        await service.worker.drain()

    asyncio.run(scenario())


def test_aged_verifier_drains_capacity_before_new_backfill(
    service, owner, version_id, executor, monkeypatch
):
    from maestro.boundaries.executor import ExecutionReport
    from maestro.testing.fakes import scored

    use(service, slots=3, cpus=4, memory_mb=8192)
    first = one(service, owner, version_id)
    other = one(service, owner, version_id, resources={"cpus": 2})
    stages_for(service, first["run_id"], (1, 1024), (4, 4096))
    transition, finish_other, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    seen = {}

    async def execute_stage(spec, sink, cancel):
        await sink.attempt_started(spec.trials[0].trial_id, 1)
        permit = await sink.resource_requested("execution")
        seen[spec.run_id] = "execution"
        if spec.run_id == first["run_id"]:
            await transition.wait()
            await sink.resource_released("execution", permit)
            seen[spec.run_id] = "waiting"
            await sink.resource_requested("verification")
            seen[spec.run_id] = "verification"
            await finish.wait()
        elif spec.run_id == other["run_id"]:
            await finish_other.wait()
        await sink.attempt_finished(spec.trials[0].trial_id, 1, scored(1), True)
        return ExecutionReport(cleanup_complete=True)

    executor.execute = execute_stage

    async def scenario():
        service.worker.start_epoch()
        await dispatch_until(service, lambda: len(seen) == 2)
        transition.set()
        await dispatch_until(service, lambda: seen[first["run_id"]] == "waiting")
        monkeypatch.setattr(scheduling, "PROTECT_AFTER_SECONDS", 0)
        later = one(service, owner, version_id)
        trial_id = service.runs.trials(later["run_id"])[0]["id"]
        await dispatch_until(service, lambda: trial_id in service.worker.ready)
        assert service.runs.trials(later["run_id"])[0]["queue_reason"] == "WAITING_PRIORITY"
        assert later["run_id"] not in seen
        assert holds(service)["cpus"] == 2
        finish_other.set()
        await dispatch_until(service, lambda: seen[first["run_id"]] == "verification")
        assert holds(service)["cpus"] == 4
        assert later["run_id"] not in seen
        finish.set()
        await service.worker.drain()
        assert seen[later["run_id"]] == "execution"

    asyncio.run(scenario())


def test_stage_budgets_reconstruct_legacy_overrides_and_verifier_fallback():
    from maestro.execution.resources import stage_resources

    baseline = [
        {"role": "environment/main", "cpus": 2, "memory_mb": 2048},
        {"role": "environment/db", "cpus": 1, "memory_mb": 1024},
        {"role": "verifier", "cpus": 1, "memory_mb": 1024},
        {"role": "tests/api", "cpus": 2, "memory_mb": 512},
    ]
    settings = {"cpus": 10, "memory_mb": 8192, "environments": baseline}
    own = stage_resources(settings, {"preparation": {"spec": {"images": [{"role": "tests/main"}]}}})
    fallback = stage_resources(settings, {})
    assert own == {
        "execution": {"cpus": 7, "memory_mb": 6656},
        "verification": {"cpus": 3, "memory_mb": 1536},
    }
    assert fallback["verification"] == {"cpus": 2, "memory_mb": 2048}
    assert baseline[0]["cpus"] == 2  # Frozen container inputs are never rewritten.


def test_earlier_run_uses_fitting_trials_beyond_the_packing_window(
    service, owner, version_id, executor, monkeypatch
):
    executor.script = {key: ["hang"] for key in ("add-01", "add-02", "mul-01")}
    use(service, slots=10, cpus=2)
    blocker = one(service, owner, version_id)
    monkeypatch.setattr(scheduling, "CANDIDATES", 2)

    async def scenario():
        worker = service.worker
        worker.start_epoch()
        await dispatch_until(service, lambda: holds(service)["cpus"] == 1)
        first = accept(service, owner, version_id, concurrency=3, repetitions=3)
        later = one(service, owner, version_id)
        with service.db.write() as conn:
            snapshot = conn.execute(
                select(t.run_snapshots.c.effective_json).where(
                    t.run_snapshots.c.run_id == first["run_id"]
                )
            ).scalar()
            for task in snapshot["tasks"]:
                cpu = 1 if task["task_key"] == "add-02" else 2
                task.update(
                    cpus=cpu,
                    environments=[{"role": "environment/main", "cpus": cpu, "memory_mb": 2048}],
                )
            conn.execute(
                update(t.run_snapshots)
                .where(t.run_snapshots.c.run_id == first["run_id"])
                .values(effective_json=snapshot)
            )
        dispatch = worker._dispatch
        monkeypatch.setattr(worker, "_dispatch", lambda: None)
        await dispatch_until(service, lambda: len(worker.ready) == 11)
        monkeypatch.setattr(worker, "_dispatch", dispatch)
        await dispatch_until(service, lambda: holds(service)["cpus"] == 2)
        started = [t for t in service.runs.trials(first["run_id"]) if t["attempts"]]
        assert len(started) == 1 and started[0]["task_key"] == "add-02"
        assert service.runs.trials(later["run_id"])[0]["attempts"] == []
        for run in (blocker, first, later):
            service.runs.cancel(owner, run["run_id"])
        await worker.drain()

    asyncio.run(scenario())


def test_model_quota_returns_during_verification_while_runner_slot_stays_held(
    service, owner, version_id, executor
):
    from maestro.boundaries.executor import ExecutionReport
    from maestro.testing.fakes import scored

    use(service, slots=2, cpus=2)
    first, second = (one(service, owner, version_id) for _ in range(2))
    for run in (first, second):
        set_provider(service, run["run_id"], "acme")
    with service.db.write() as conn:
        conn.execute(
            t.provider_limits.insert().values(
                provider="acme", max_concurrent_trials=1, updated_by="usr_owner", updated_at="t"
            )
        )
    release_model, finish = asyncio.Event(), asyncio.Event()
    seen = set()

    async def execute_phase(spec, sink, cancel):
        trial_id = spec.trials[0].trial_id
        await sink.attempt_started(trial_id, 1)
        await sink.phase_changed(trial_id, 1, "agent")
        seen.add(spec.run_id)
        if spec.run_id == first["run_id"]:
            await release_model.wait()
            await sink.model_finished()
            await sink.model_finished()
            await sink.phase_changed(trial_id, 1, "verifying")
        await finish.wait()
        await sink.attempt_finished(trial_id, 1, scored(1), True)
        return ExecutionReport(cleanup_complete=True)

    executor.execute = execute_phase

    async def scenario():
        service.worker.start_epoch()
        await dispatch_until(service, lambda: len(seen) == 1)
        assert seen == {first["run_id"]} and holds(service)["providers"] == {"acme": 1}
        release_model.set()
        await dispatch_until(service, lambda: len(seen) == 2)
        assert service.runs.trials(first["run_id"])[0]["state"] == "verifying"
        assert holds(service)["slots"] == 2 and holds(service)["providers"] == {"acme": 1}
        finish.set()
        await service.worker.drain()

    asyncio.run(scenario())


def test_equal_acceptance_times_preserve_batch_and_single_run_order(
    service, owner, version_id, executor
):
    executor.script = {"add-01": ["hang"]}
    use(service, slots=1)
    first = one(service, owner, version_id)
    task_id = service.runs.trials(first["run_id"])[0]["task_id"]
    body = {
        "dataset_version_id": version_id,
        "execution_mode": "oracle",
        "task_selection": {"mode": "explicit", "task_ids": [task_id]},
    }
    _, batch, _ = service.runs.submit_batch(
        owner,
        uuid.uuid4().hex,
        {"name": "batch", "runs": [{"name": "a", **body}, {"name": "b", **body}]},
    )
    last = one(service, owner, version_id)
    order = [first, *batch["runs"], last]
    with service.db.write() as conn:
        conn.execute(update(t.runs).values(accepted_at="2000-01-01T00:00:00.000000Z"))

    async def scenario():
        service.worker.start_epoch()
        for run in order:
            await dispatch_until(
                service, lambda run=run: bool(service.runs.trials(run["run_id"])[0]["attempts"])
            )
            assert holds(service)["slots"] == 1
            service.runs.cancel(owner, run["run_id"])
            await dispatch_until(
                service,
                lambda run=run: service.runs.get(owner, run["run_id"])["state"] == "cancelled",
            )
        await service.worker.drain()

    asyncio.run(scenario())
