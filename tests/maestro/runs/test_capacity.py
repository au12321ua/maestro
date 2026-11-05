"""Capacity accounting: slots, CPU / memory, provider limits, cleanup-blocked capacity."""

import asyncio
import uuid

import pytest
from sqlalchemy import select, update
from support import Api, running, setup_admin
from support.datasets import import_version

from maestro.db import schema as t
from maestro.services.capacity import HostCapacity


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
            .values(effective_json={**effective, "model": {"provider": provider}})
        )


def test_runs_wait_for_slots_and_small_runs_pass_large_ones(
    service, owner, version_id, executor, source
):
    executor.script = {"add-01": ["hang"], "add-02": ["hang"], "mul-01": ["hang"]}
    use(service, slots=4)
    first = accept(service, owner, version_id, concurrency=3)  # 3 trials, width 3
    large = accept(service, owner, version_id, concurrency=2)  # width 2: no room
    service.worker.start_epoch()
    claimed = service.worker._claim()
    assert claimed[0] == first["run_id"]
    assert service.worker._claim() is None
    assert service.runs.get(owner, large["run_id"])["queue_reason"] == "CAPACITY_SLOTS"
    source.add("demo", "one", "v1", ["solo"])
    one = import_version(service, "demo", "one", "v1")["dataset_version_id"]
    passing = accept(service, owner, one, concurrency=1)
    assert service.worker._claim()[0] == passing["run_id"]
    assert service.runs.get(owner, large["run_id"])["state"] == "queued"


def test_cpu_and_memory_reservations(service, owner, version_id):
    use(service, slots=10, cpus=3, memory_mb=8192)
    first = accept(service, owner, version_id, concurrency=2)  # 2 x (1 CPU, 2 GiB)
    second = accept(service, owner, version_id, concurrency=2)
    service.worker.start_epoch()
    assert service.worker._claim()[0] == first["run_id"]
    assert service.worker._claim() is None
    assert service.runs.get(owner, second["run_id"])["queue_reason"] == "CAPACITY_CPU"
    use(service, slots=10, cpus=8, memory_mb=6144)
    assert service.worker._claim() is None
    assert service.runs.get(owner, second["run_id"])["queue_reason"] == "CAPACITY_MEMORY"


def test_provider_limit_counts_trials_across_runs(service, owner, version_id):
    use(service, slots=10)
    first = accept(service, owner, version_id, concurrency=2)
    second = accept(service, owner, version_id, concurrency=2)
    other = accept(service, owner, version_id, concurrency=2)
    set_provider(service, first["run_id"], "acme")
    set_provider(service, second["run_id"], "acme")
    set_provider(service, other["run_id"], "zeta")
    with service.db.write() as conn:
        conn.execute(
            t.provider_limits.insert().values(
                provider="acme", max_concurrent_trials=3, updated_by="usr_owner", updated_at="t"
            )
        )
    service.worker.start_epoch()
    assert service.worker._claim()[0] == first["run_id"]
    assert service.worker._claim()[0] == other["run_id"]  # another provider passes
    assert service.runs.get(owner, second["run_id"])["queue_reason"] == "PROVIDER_LIMIT"


def test_run_that_can_never_fit_is_refused_at_acceptance(service, owner, version_id):
    use(service, slots=2)
    with pytest.raises(Exception) as caught:
        accept(service, owner, version_id, concurrency=3)
    assert caught.value.code == "RUN_EXCEEDS_CAPACITY"
    assert accept(service, owner, version_id, concurrency=2)["state"] == "queued"


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
