"""The maintenance loop must keep retention going whatever the export builds do."""

import asyncio
import logging

import pytest
from sqlalchemy import select, update
from support.results import backdate, make_owner, run

from maestro.core import clock
from maestro.db import schema as t
from maestro.services import maintenance
from maestro.services.exports import Exports
from maestro.services.maintenance import Maintenance


@pytest.fixture
def instance(service, version_id):
    owner = make_owner(service)
    service.worker.start_epoch()
    return service, owner, version_id


def expired_artifact(service, owner, version_id) -> tuple[str, str]:
    """A finished run whose artifact is past its expiry; returns (run id, artifact id)."""
    run_id = run(service, owner, version_id)
    artifact = service.artifacts.for_run(run_id)[0]
    backdate(service, artifact["id"])
    return run_id, artifact["id"]


def artifact_status(service, artifact_id) -> str:
    with service.db.read() as conn:
        return conn.execute(
            select(t.artifacts.c.status).where(t.artifacts.c.id == artifact_id)
        ).scalar()


def export_row(service, export_id):
    with service.db.read() as conn:
        return conn.execute(select(t.exports).where(t.exports.c.id == export_id)).mappings().one()


def make_failing(monkeypatch, calls: list):
    def boom(self, conn, request):
        calls.append(1)
        raise RuntimeError("selecting failed for sk-abcdefghijklmnopqrstuv")

    monkeypatch.setattr(Exports, "_select_artifacts", boom)


def test_a_build_that_raises_fails_that_export_once_and_retention_still_runs(
    instance, monkeypatch, caplog
):
    service, owner, version_id = instance
    run_id, artifact_id = expired_artifact(service, owner, version_id)
    calls: list = []
    make_failing(monkeypatch, calls)
    created = service.exports.create(owner, {"run_ids": [run_id]})
    with caplog.at_level(logging.ERROR):
        report = service.maintenance.run_once()
    assert report["exports_built"] == 1 and calls == [1]
    row = export_row(service, created["id"])
    assert row["state"] == "failed" and row["error_json"]["code"] == "EXPORT_BUILD_FAILED"
    assert "sk-" not in str(row["error_json"])  # the failure text never reaches the record
    operation = service.operations.get(created["operation_id"])
    assert operation["status"] == "failed" and operation["error"]["code"] == "EXPORT_BUILD_FAILED"
    assert artifact_status(service, artifact_id) == "expired"  # retention ran in the same pass
    with service.db.read() as conn:
        assert conn.execute(select(t.artifact_pins)).first() is None

    assert service.maintenance.run_once()["exports_built"] == 0  # never retried
    assert calls == [1]


def test_the_loop_reaches_retention_while_a_broken_export_is_queued(instance, monkeypatch):
    service, owner, version_id = instance
    run_id, artifact_id = expired_artifact(service, owner, version_id)
    calls: list = []
    make_failing(monkeypatch, calls)
    created = service.exports.create(owner, {"run_ids": [run_id]})
    loop = Maintenance(service.artifacts, service.exports, poll_seconds=0.02, sweep_seconds=0.02)

    async def main():
        task = asyncio.create_task(loop.run_forever())
        for _ in range(100):
            await asyncio.sleep(0.05)
            if artifact_status(service, artifact_id) == "expired":
                break
        await loop.stop()
        await task

    asyncio.run(main())
    assert artifact_status(service, artifact_id) == "expired"
    assert export_row(service, created["id"])["state"] == "failed" and calls == [1]


def test_a_stream_of_exports_cannot_starve_retention(instance, monkeypatch):
    service, owner, version_id = instance
    run_id, artifact_id = expired_artifact(service, owner, version_id)
    built = []

    def endless(self, export_id):
        built.append(export_id)
        # Every build leaves another export behind, like a steady stream of requests.
        if len(built) < 50:
            self.create(owner, {"run_ids": [run_id], "include_artifacts": False})
        with self.db.write() as conn:
            conn.execute(
                update(t.exports).where(t.exports.c.id == export_id).values(state="failed")
            )

    monkeypatch.setattr(Exports, "build", endless)
    service.exports.create(owner, {"run_ids": [run_id], "include_artifacts": False})
    report = service.maintenance.run_once()
    assert report["exports_built"] == maintenance.BUILDS_PER_CYCLE == len(built)
    assert artifact_status(service, artifact_id) == "expired"


def test_one_failing_step_does_not_stop_the_other(instance, monkeypatch, caplog):
    service, owner, version_id = instance
    run_id, _ = expired_artifact(service, owner, version_id)
    package = service.exports.create(owner, {"run_ids": [run_id]})
    service.exports.build(package["id"])
    with service.db.write() as conn:
        conn.execute(
            update(t.exports)
            .where(t.exports.c.id == package["id"])
            .values(expires_at=clock.to_text(clock.now() - clock.timedelta(minutes=1)))
        )

    def broken():
        raise OSError("disk went away")

    monkeypatch.setattr(service.artifacts, "sweep", broken)
    with caplog.at_level(logging.ERROR):
        report = service.maintenance.run_once()
    assert report["exports_expired"] == 1
    assert export_row(service, package["id"])["state"] == "expired"
    assert any("step artifacts failed" in r.getMessage() for r in caplog.records)
