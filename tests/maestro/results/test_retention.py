"""Artifact retention: expiry from execution end, reclaiming with read pins, bounded
reads, index/file reconciliation and storage watermarks."""

import asyncio
import logging
import os
import uuid

import pytest
from sqlalchemy import insert, select, update
from support.datasets import import_version
from support.results import backdate, make_owner, run

from maestro.application import Maestro
from maestro.boundaries.storage import LocalArtifactStore
from maestro.core import clock
from maestro.core.errors import MaestroError
from maestro.db import migrate
from maestro.db import schema as t
from maestro.services.disk import GIB, DiskWatch
from maestro.testing.fakes import FakeExecutor


@pytest.fixture
def local(settings, source, environment):
    """A service writing real files, for races that depend on POSIX unlink."""
    built = Maestro.build(
        settings,
        executor=FakeExecutor(),
        store=LocalArtifactStore(settings.artifacts_dir),
        environment=environment,
        sources={"fake": source},
    )
    migrate.upgrade(built.db)
    built.keys.initialize()
    version = import_version(built)["dataset_version_id"]
    owner = make_owner(built)
    built.worker.start_epoch()
    yield built, owner, version
    built.db.dispose()


def test_expiry_counts_from_execution_end_not_upload(local):
    service, owner, version = local
    run_id = run(service, owner, version)
    with service.db.read() as conn:
        rows = conn.execute(
            select(t.artifacts.c.expires_at, t.trials.c.execution_terminal_at).join(
                t.trials, t.trials.c.id == t.artifacts.c.trial_id
            )
        ).all()
        finished = conn.execute(select(t.runs.c.finished_at).where(t.runs.c.id == run_id)).scalar()
    assert rows
    for expires, ended in rows:
        assert clock.from_text(expires) - clock.from_text(ended) == clock.timedelta(days=7)
    run_level = [a for a in service.artifacts.for_run(run_id) if a["trial_id"] is None]
    for artifact in run_level:
        assert clock.from_text(artifact["expires_at"]) - clock.from_text(finished) == (
            clock.timedelta(days=7)
        )


def test_reclaim_keeps_index_scores_and_reports_expired(local):
    service, owner, version = local
    run_id = run(service, owner, version)
    before = service.runs.summary(run_id)
    trial = service.runs.trials(run_id)[0]
    for artifact in service.artifacts.for_run(run_id, trial["id"]):
        backdate(service, artifact["id"])
    assert service.artifacts.sweep()["reclaimed"] == 2
    listed = service.artifacts.for_run(run_id, trial["id"])
    assert {a["status"] for a in listed} == {"expired"}
    assert all(a["digest"] and a["status_reason"] == "RETENTION_EXPIRED" for a in listed)
    with pytest.raises(MaestroError) as error:
        service.artifacts.read(listed[0]["id"])
    assert error.value.status == 410 and error.value.code == "ARTIFACT_EXPIRED"
    again = next(x for x in service.runs.trials(run_id) if x["id"] == trial["id"])
    assert again["artifact_state"] == "expired"
    assert again["artifact_counts"] == {"available": 0, "failed": 0, "expired": 2}
    assert again["result"]["score"] == trial["result"]["score"]
    assert service.runs.summary(run_id) == before


def test_pins_hold_files_for_a_bounded_time_and_open_readers_survive(local):
    service, owner, version = local
    run_id = run(service, owner, version)
    artifact = next(a for a in service.artifacts.for_run(run_id) if a["kind"] == "output")
    meta, reader, count, total = service.artifacts.open(artifact["id"])
    backdate(service, artifact["id"])
    pin_until = clock.now() + clock.timedelta(minutes=5)
    with service.db.write() as conn:
        conn.execute(
            insert(t.artifact_pins).values(
                id="pin_a",
                artifact_id=artifact["id"],
                holder_kind="export",
                holder_id="exp_x",
                expires_at=clock.to_text(pin_until),
            )
        )
    assert service.artifacts.sweep()["reclaimed"] == 0
    with service.db.write() as conn:
        conn.execute(update(t.artifact_pins).values(expires_at=clock.to_text(clock.now())))
    assert service.artifacts.sweep()["reclaimed"] == 1
    with service.db.read() as conn:
        assert conn.execute(select(t.artifact_pins)).first() is None  # released
    # A download that opened the file before reclaiming still reads all of it.
    with reader:
        assert len(reader.read(count)) == total == meta["size_bytes"]


def test_bounded_reads_for_log_paging(local, settings):
    service, owner, version = local
    run_id = run(service, owner, version)
    log = next(a for a in service.artifacts.for_run(run_id) if a["kind"] == "log")
    _, reader, count, total = service.artifacts.open(log["id"], offset=3, length=4)
    with reader:
        assert count == 4 and reader.read(count) == b"empt" and total == log["size_bytes"]
    _, reader, count, _ = service.artifacts.open(log["id"], offset=10_000)
    reader.close()
    assert count == 0
    with pytest.raises(MaestroError) as error:
        service.artifacts.open(log["id"], length=10**9)
    assert error.value.code == "INVALID_RANGE"


def test_reconcile_marks_missing_and_removes_orphans(local, settings):
    """After a crash no index row claims a missing file, no file lacks a row."""
    service, owner, version = local
    run_id = run(service, owner, version)
    artifact = service.artifacts.for_run(run_id)[0]
    with service.db.read() as conn:
        key = conn.execute(
            select(t.artifacts.c.storage_key).where(t.artifacts.c.id == artifact["id"])
        ).scalar()
    (settings.artifacts_dir / key).unlink()
    orphan = settings.artifacts_dir / "runs" / "orphan" / "file"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"published but never indexed")
    partial = settings.artifacts_dir / "runs" / "orphan" / ".file.123.tmp"
    partial.write_bytes(b"half")
    report = service.artifacts.reconcile()
    assert report == {"missing": 1, "orphans": 1, "partial": 1, "readopted": 0}
    assert not orphan.exists() and not partial.exists()
    row = next(a for a in service.artifacts.for_run(run_id) if a["id"] == artifact["id"])
    assert row["status"] == "unavailable" and row["status_reason"] == "STORAGE_CONTENT_MISSING"
    with pytest.raises(MaestroError) as error:
        service.artifacts.read(artifact["id"])
    assert error.value.code == "ARTIFACT_UNAVAILABLE"


def statuses(service, run_id) -> dict[str, tuple[str, str | None]]:
    return {a["id"]: (a["status"], a["status_reason"]) for a in service.artifacts.for_run(run_id)}


def test_reconcile_readopts_files_once_the_volume_is_back(local, settings, caplog):
    """A start while the artifact volume was absent must not condemn intact files for good."""
    service, owner, version = local
    run_id = run(service, owner, version)
    root = settings.artifacts_dir
    total = len(statuses(service, run_id))
    away = root.with_name("artifacts.unmounted")
    os.rename(root, away)
    with caplog.at_level(logging.ERROR, logger="maestro.services.artifacts"):
        assert service.artifacts.reconcile()["missing"] == total
    # Most of the index vanished at once: that is a storage incident, said loudly.
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    assert {s for s, _ in statuses(service, run_id).values()} == {"unavailable"}

    os.rename(away, root)
    report = service.artifacts.reconcile()
    assert report["readopted"] == total and report["missing"] == 0
    assert {v for v in statuses(service, run_id).values()} == {("available", None)}
    artifact = service.artifacts.for_run(run_id)[0]
    assert service.artifacts.read(artifact["id"])[1]
    assert service.artifacts.reconcile()["readopted"] == 0  # nothing more to do


def test_reconcile_does_not_readopt_files_that_changed_or_other_failures(local, settings):
    service, owner, version = local
    run_id = run(service, owner, version)
    rows = service.artifacts.for_run(run_id)
    with service.db.read() as conn:
        keys = {
            r.id: r.storage_key
            for r in conn.execute(select(t.artifacts.c.id, t.artifacts.c.storage_key))
        }
    short, swapped, healthy = rows[0], rows[1], rows[2]
    root = settings.artifacts_dir
    away = root.with_name("artifacts.unmounted")
    os.rename(root, away)
    service.artifacts.reconcile()
    os.rename(away, root)
    (root / keys[short["id"]]).write_bytes(b"x")  # size no longer matches the index
    original = (root / keys[swapped["id"]]).read_bytes()
    (root / keys[swapped["id"]]).write_bytes(b"?" * len(original))  # same size, other content
    # A row that went unavailable for another reason stays as it is.
    with service.db.write() as conn:
        conn.execute(
            update(t.artifacts)
            .where(t.artifacts.c.id == healthy["id"])
            .values(status_reason="STORAGE_WRITE_FAILED")
        )
    report = service.artifacts.reconcile()
    now = statuses(service, run_id)
    assert now[short["id"]] == ("unavailable", "STORAGE_CONTENT_MISSING")
    assert now[swapped["id"]] == ("unavailable", "STORAGE_CONTENT_MISSING")
    assert now[healthy["id"]] == ("unavailable", "STORAGE_WRITE_FAILED")
    assert report["readopted"] == len(rows) - 3


def test_storage_watermarks_pause_acceptance_then_dispatch(local, settings):
    """Low space refuses new runs, then holds queued ones; queries and cancel work."""
    service, owner, version = local
    free = {"bytes": 100 * GIB}
    watch = DiskWatch(
        settings.data_dir,
        type(settings)(
            **{
                **settings.__dict__,
                "storage_accept_min_free_gb": 20,
                "storage_dispatch_min_free_gb": 10,
                "storage_accept_min_free_percent": 0,
                "storage_dispatch_min_free_percent": 0,
            }
        ),
        usage=lambda _: (1000 * GIB, 0, free["bytes"]),
    )
    service.runs.disk = watch
    service.worker.disk = watch
    _, queued, _ = service.runs.submit(
        owner,
        uuid.uuid4().hex,
        {"name": "r", "dataset_version_id": version, "execution_mode": "oracle"},
    )
    free["bytes"] = 15 * GIB
    with pytest.raises(MaestroError) as error:
        service.runs.submit(
            owner,
            uuid.uuid4().hex,
            {"name": "r", "dataset_version_id": version, "execution_mode": "oracle"},
        )
    assert error.value.code == "STORAGE_LOW" and error.value.status == 503
    free["bytes"] = 5 * GIB
    asyncio.run(service.worker.tick())
    held = service.runs.get(owner, queued["run_id"])
    assert held["state"] == "queued" and held["queue_reason"] == "STORAGE_LOW"
    assert service.runs.cancel(owner, queued["run_id"])["state"] == "cancelled"
    free["bytes"] = 100 * GIB
    assert watch.accepting() and watch.dispatching()


def test_manual_retention_survives_expiry_and_restart_on_original_links(local, monkeypatch):
    from maestro.db.engine import Database
    from maestro.services.artifacts import Artifacts

    service, owner, version = local
    kept = run(service, owner, version)
    ordinary = run(service, owner, version)
    original = service.artifacts.for_run(kept)
    scores = {run_id: service.runs.summary(run_id) for run_id in (kept, ordinary)}
    view = service.artifacts.retain(owner, kept)
    assert view["retained_bytes"] == sum(a["size_bytes"] for a in original) > 0
    assert view["additional_bytes"] == 0
    assert {"log", "output"} <= {a["kind"] for a in original}
    future = clock.now() + clock.timedelta(days=8)
    monkeypatch.setattr(clock, "now", lambda: future)

    db = Database.open(service.artifacts.db.engine.url.database)
    try:
        reopened = Artifacts(db, service.artifacts.store)
        reopened.reconcile()
        assert reopened.sweep()["reclaimed"] == len(service.artifacts.for_run(ordinary))
        assert {status for status, _ in statuses(service, ordinary).values()} == {"expired"}
        for artifact in original:
            metadata, data = reopened.read(artifact["id"])
            assert len(data) == artifact["size_bytes"] and metadata == artifact
        view = reopened.retention(kept)
        assert all(file["past_expiry"] and file["retained"] for file in view["files"])
        assert reopened.release(owner, kept)["used_bytes"] == 0
        assert reopened.release(owner, kept)["used_bytes"] == 0
        assert reopened.sweep()["reclaimed"] == len(original)
        assert {status for status, _ in statuses(service, kept).values()} == {"expired"}
        assert {run_id: service.runs.summary(run_id) for run_id in scores} == scores
    finally:
        db.dispose()


def test_manual_retention_ignores_missing_and_files_claimed_by_sweep(local, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    service, owner, version = local
    run_id = run(service, owner, version)
    expired, missing, reclaiming, *_ = service.artifacts.for_run(run_id)
    backdate(service, expired["id"])
    assert service.artifacts.sweep()["reclaimed"] == 1
    with service.db.read() as conn:
        missing_key = conn.scalar(
            select(t.artifacts.c.storage_key).where(t.artifacts.c.id == missing["id"])
        )
    store = service.artifacts.store
    remove = store.delete
    remove(missing_key)
    backdate(service, reclaiming["id"])
    deleting, finish = Event(), Event()

    def wait_to_remove(key):
        deleting.set()
        assert finish.wait(5)
        remove(key)

    monkeypatch.setattr(store, "delete", wait_to_remove)
    with ThreadPoolExecutor() as pool:
        sweeping = pool.submit(service.artifacts.sweep)
        try:
            assert deleting.wait(5)
            view = service.artifacts.retain(owner, run_id)
        finally:
            finish.set()
        assert sweeping.result()["reclaimed"] == 1
    files = {file["artifact_id"]: file for file in view["files"]}
    for artifact, status in [
        (expired, "expired"),
        (missing, "unavailable"),
        (reclaiming, "deleting"),
    ]:
        assert files[artifact["id"]]["status"] == status
        assert not files[artifact["id"]]["retained"]
    assert files[missing["id"]]["status_reason"] == "STORAGE_CONTENT_MISSING"
    assert view["used_bytes"] == view["retained_bytes"] > 0
    assert view["available_bytes"] == view["retained_bytes"]


def test_manual_retention_serializes_same_run_and_competing_quota(local, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    service, owner, version = local
    runs = [run(service, owner, version) for _ in range(3)]
    sizes = [service.artifacts.retention(run_id)["additional_bytes"] for run_id in runs]
    quota = sum(sizes) - min(sizes)
    service.policies.update(owner, {"artifact_retention_quota_bytes": quota}, 0)
    probe = service.artifacts._check_retention_files

    def compete(*run_ids):
        barrier = Barrier(2)

        def check(rows):
            result = probe(rows)
            barrier.wait(timeout=5)
            return result

        def retain(run_id):
            try:
                return service.artifacts.retain(owner, run_id)
            except MaestroError as error:
                return error

        with monkeypatch.context() as patch, ThreadPoolExecutor() as pool:
            patch.setattr(service.artifacts, "_check_retention_files", check)
            return list(pool.map(retain, run_ids))

    first, repeated = compete(runs[0], runs[0])
    assert first == repeated and first["used_bytes"] == sizes[0]
    outcomes = compete(*runs[1:])
    winners = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    refused = [outcome for outcome in outcomes if isinstance(outcome, MaestroError)]
    assert len(winners) == len(refused) == 1
    assert refused[0].code == "ARTIFACT_RETENTION_QUOTA"
    assert refused[0].suggested_actions[0]["shortfall_bytes"] == sum(sizes) - quota
    assert service.artifacts.retention(runs[0])["retained_bytes"] == sizes[0]
    assert service.artifacts.retention(runs[0])["used_bytes"] == winners[0]["used_bytes"]


def test_purge_releases_only_removed_artifact_bytes_and_retries_leftovers(local, monkeypatch):
    service, owner, version = local
    run_id = run(service, owner, version)
    original = service.artifacts.retain(owner, run_id)["used_bytes"]
    a, b, *_ = service.artifacts.for_run(run_id)
    assert a["size_bytes"] > 0 and b["size_bytes"] > 0
    service.recycle.purge(owner, service.recycle.delete(owner, "artifact", a["id"])["id"])
    assert service.artifacts.retention(run_id)["used_bytes"] == original - a["size_bytes"]

    with service.db.read() as conn:
        key = conn.scalar(select(t.artifacts.c.storage_key).where(t.artifacts.c.id == b["id"]))
    store = service.artifacts.store
    remove = store.delete

    def failed_unlink(candidate):
        if candidate == key:
            raise OSError("temporarily unavailable")
        remove(candidate)

    with monkeypatch.context() as patch:
        patch.setattr(store, "delete", failed_unlink)
        entry = service.recycle.delete(owner, "run", run_id)
        service.recycle.purge(owner, entry["id"])
        other = run(service, owner, version)
        assert service.artifacts.retention(other)["used_bytes"] == b["size_bytes"]
        assert store.exists(key)
    assert service.artifacts.sweep_unindexed()["removed"] == 1
    assert service.artifacts.retention(other)["used_bytes"] == 0
