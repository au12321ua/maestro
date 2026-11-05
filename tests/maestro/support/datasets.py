"""Shared datasets helpers for tests."""

import asyncio
import time

from sqlalchemy import select

from maestro.db import schema as t
from support import setup_admin, wait_operation


def import_dataset(admin, namespace="demo", name="arith", ref="v1", **extra) -> dict:
    body = {"source_kind": "fake", "namespace": namespace, "name": name, "ref": ref, **extra}
    op = admin.post("/api/v1/admin/dataset-imports", json=body, expect=202).json()["data"]
    return wait_operation(admin, op["id"])


def import_and_prepare(admin, *args, **kw) -> tuple[str, dict | None]:
    done = import_dataset(admin, *args, **kw)
    assert done["status"] == "succeeded", done
    prepared = None
    if done["result"].get("preparation_operation_id"):
        prepared = wait_operation(admin, done["result"]["preparation_operation_id"])
    return done["result"]["dataset_version_id"], prepared


def tasks_by_key(api, version_id) -> dict:
    tasks = api.get(f"/api/v1/dataset-versions/{version_id}/tasks", expect=200).json()["data"]
    return {x["task_key"]: x for x in tasks}


def wait_run(api, run_id) -> dict:
    run = api.get(f"/api/v1/runs/{run_id}", expect=200).json()["data"]
    wait_operation(api, run["operation_id"])
    return api.get(f"/api/v1/runs/{run_id}", expect=200).json()["data"]


def evict(admin, task) -> None:
    preparation = task["local_preparation"]["id"]
    op = admin.post(f"/api/v1/admin/preparations/{preparation}/evict", expect=202).json()["data"]
    assert wait_operation(admin, op["id"])["status"] == "succeeded"


def until(condition, timeout=10.0, message="condition"):
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {message}")
        time.sleep(0.02)


def on_loop(service, callback):
    service.worker._loop.call_soon_threadsafe(callback)


def solo(service, source, client, name="solo"):
    source.add("demo", name, "v1", [name])
    admin = setup_admin(service, client)
    version_id, prepared = import_and_prepare(admin, name=name)
    assert prepared["status"] == "succeeded"
    (task,) = tasks_by_key(admin, version_id).values()
    return admin, version_id, task


def trial_attempts(service, run_id):
    with service.db.read() as conn:
        return conn.execute(
            select(t.execution_attempts.c.attempt_index, t.execution_attempts.c.error_json)
            .join(t.trials, t.trials.c.id == t.execution_attempts.c.trial_id)
            .where(t.trials.c.run_id == run_id)
            .order_by(t.execution_attempts.c.attempt_index)
        ).all()


def import_version(service, namespace="demo", name="arith", ref="v1", **request) -> dict:
    """Import and first-prepare through the real Operation code, outside the worker."""
    return asyncio.run(
        service.preparer.import_and_prepare(
            {"source_kind": "fake", "namespace": namespace, "name": name, "ref": ref, **request},
            None,
        )
    )
