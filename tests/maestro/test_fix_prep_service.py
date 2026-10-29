"""Review fixes for the preparation coordinator: failures that escaped classification,
leaked pins and waiters, tasks without a spec, unavailable images that never recover,
and operations that reported the wrong outcome."""

import asyncio
import shutil
import uuid

import pytest
from sqlalchemy import delete, select, update
from support import running, setup_admin, submit, wait_operation
from test_datasets import (
    evict,
    import_and_prepare,
    import_dataset,
    tasks_by_key,
    wait_run,
)
from test_preparation import on_loop, solo, trial_attempts, until

from maestro.boundaries.environment import TRANSIENT, PreparationError
from maestro.db import schema as t
from maestro.domain.states import Role
from maestro.services.principal import Channel, Principal


def run_leftovers(service, run_id):
    """Pins held by, and waiter rows still open for, one run."""
    with service.db.read() as conn:
        pins = (
            conn.execute(
                select(t.preparation_pins.c.id).where(
                    t.preparation_pins.c.holder_kind == "run",
                    t.preparation_pins.c.holder_id == run_id,
                )
            )
            .scalars()
            .all()
        )
        waiting = (
            conn.execute(
                select(t.preparation_waiters.c.id).where(
                    t.preparation_waiters.c.run_id == run_id,
                    t.preparation_waiters.c.state == "waiting",
                )
            )
            .scalars()
            .all()
        )
    return pins, waiting


# --- the Docker check fails -----------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        lambda: PreparationError(TRANSIENT, "docker image inspect timed out after 60s"),
        lambda: FileNotFoundError("docker"),
    ],
    ids=["preparation-error", "os-error"],
)
def test_a_failing_cache_check_is_a_retryable_restore_failure(service, source, environment, error):
    """A check that cannot answer is classified like a failed restore: an attempt of the
    trial, retried with back-off, never a bare executor failure."""
    with running(service) as client:
        admin, version_id, _ = solo(service, source, client)
        real, calls = environment.check, {"n": 0}

        async def flaky(expected):
            calls["n"] += 1
            if calls["n"] == 1:
                raise error()
            return await real(expected)

        environment.check = flaky
        run = submit(admin, version_id).json()["data"]
        done = wait_run(admin, run["run_id"])
        assert done["counts"]["scored"] == 1
        first, second = trial_attempts(service, run["run_id"])
        assert (first.attempt_index, second.attempt_index) == (1, 2)
        assert first.error_json["code"] == "PREPARATION_RESTORE_FAILED"
        assert first.error_json["detail"]["kind"] == "transient"
        assert second.error_json is None
        assert run_leftovers(service, run["run_id"]) == ([], [])


def test_a_failing_task_cancels_its_siblings_and_leaves_no_pin_or_waiter(
    service, source, environment
):
    """When one task of a run blows up unexpectedly, the sibling that is waiting for a
    shared restore is cancelled with it: it must not join the restore after the run is
    closed and be handed a pin that nothing releases."""
    source.add("demo", "duo", "v1", ["a", "b"])
    with running(service) as client:
        admin = setup_admin(service, client)
        version_id, _ = import_and_prepare(admin, name="duo")
        tasks = tasks_by_key(admin, version_id)
        evict(admin, tasks["b"])  # b is cold; its restore is held at the gate
        environment.gate = asyncio.Event()
        environment.started.clear()
        real = service.preparer.acquire

        async def acquire(ctx, *args, **kwargs):
            if ctx.task_key == "a":
                while not environment.started.is_set():  # b has joined the restore
                    await asyncio.sleep(0.01)
                raise RuntimeError("unexpected failure while preparing a")
            return await real(ctx, *args, **kwargs)

        service.preparer.acquire = acquire
        run = submit(admin, version_id).json()["data"]
        wait_run(admin, run["run_id"])  # the run is closed because task a blew up
        on_loop(service, environment.gate.set)  # b's restore may now finish, if it still runs
        until(lambda: not service.preparer.active, message="restore ended")
        assert run_leftovers(service, run["run_id"]) == ([], [])
        with service.db.read() as conn:
            assert conn.execute(select(t.preparation_pins.c.id)).all() == []


# --- tasks without a preparation spec ---------------------------------------------------


def test_a_task_recorded_as_unsupported_fails_alone_in_first_preparation(
    service, source, environment
):
    source.add("demo", "mix", "v1", ["ok-a", "bad-b", "ok-c"])
    (bad,) = [x for x in source.versions[("demo", "mix", "v1")].tasks if x.task_key == "bad-b"]
    source.fail_fetch[bad.content_digest] = ["unsupported"]
    with running(service) as client:
        admin = setup_admin(service, client)
        done = import_dataset(admin, name="mix")
        assert done["status"] == "succeeded"
        version_id = done["result"]["dataset_version_id"]
        first = wait_operation(admin, done["result"]["preparation_operation_id"])
        # The Operation completes normally, reporting the one task that is not admitted.
        assert first["status"] == "failed"
        assert first["error"]["code"] == "FIRST_PREPARATION_INCOMPLETE"
        tasks = tasks_by_key(admin, version_id)
        assert [tasks[k]["admission"]["state"] for k in ("ok-a", "bad-b", "ok-c")] == [
            "admitted",
            "first_failed",
            "admitted",
        ]
        (blocker,) = tasks["bad-b"]["blockers"]
        assert blocker["code"] == "TASK_NOT_ADMITTED"
        assert blocker["first_preparation_error"]["code"] == "PREPARATION_UNSUPPORTED"
        assert "fake unsupported failure" in blocker["first_preparation_error"]["message"]
        with service.db.read() as conn:
            evidence = conn.execute(
                select(t.task_admissions.c.evidence_json).where(
                    t.task_admissions.c.task_id == tasks["bad-b"]["id"]
                )
            ).scalar()
        assert evidence["error"]["evidence"]["reason"]  # says why
        assert tasks["ok-a"]["can_submit"] and tasks["ok-c"]["can_submit"]
        assert not service.preparer.jobs and not service.preparer.active
        # The healthy tasks run; the bad one stays out of "all eligible".
        run = submit(admin, version_id).json()["data"]
        assert run["planned_trials"] == 2
        assert wait_run(admin, run["run_id"])["counts"]["scored"] == 2


# --- an environment image recorded as unavailable ---------------------------------------


def test_reimport_brings_an_unavailable_environment_image_back(service, source, environment):
    source.add("demo", "solo", "v1", ["solo"])
    with running(service) as client:
        admin = setup_admin(service, client)
        version_id, _ = import_and_prepare(admin, name="solo")
        (task,) = tasks_by_key(admin, version_id).values()
        spec = task["spec_digest"]
        lost = {i.digest for i in environment.outputs[spec].images}
        evict(admin, task)
        environment.nondeterministic.add("solo")
        environment.lose(spec)
        run = submit(admin, version_id).json()["data"]
        wait_run(admin, run["run_id"])
        blocked = tasks_by_key(admin, version_id)["solo"]
        assert blocked["blockers"][0]["resource_kind"] == "environment_image"
        assert submit(admin, version_id).status_code == 422  # no restore is even attempted

        # While the cause persists, a re-import changes nothing.
        import_dataset(admin, name="solo")
        assert not tasks_by_key(admin, version_id)["solo"]["can_submit"]

        # The registry has it again (restored from a backup): offering the same version
        # again re-evaluates the image and clears the block.
        environment.nondeterministic.discard("solo")
        environment.registry |= lost
        again = import_dataset(admin, name="solo")
        assert again["result"]["existing"] is True
        recovered = tasks_by_key(admin, version_id)["solo"]
        assert recovered["can_submit"] and not recovered["blockers"]
        assert recovered["local_preparation"]["cache_state"] == "ready"
        assert recovered["admission"] == blocked["admission"]  # admission was never touched
        rerun = submit(admin, version_id).json()["data"]
        assert wait_run(admin, rerun["run_id"])["counts"]["scored"] == 1
        with service.db.read() as conn:
            assert not conn.execute(select(t.preparation_pins.c.id)).all()


# --- credentials ---------------------------------------------------------------------


def test_a_deleted_dataset_credential_is_an_auth_failure_not_a_retry(service, source, environment):
    source.add("demo", "solo", "v1", ["solo"])
    with running(service) as client:
        admin = setup_admin(service, client)
        credential = admin.post(
            "/api/v1/admin/credentials",
            json={"name": "team-hub", "kind": "hub_token", "provider": "hub", "value": "S3CRET"},
            expect=201,
        ).json()["data"]
        version_id, _ = import_and_prepare(admin, name="solo", credential_id=credential["id"])
        (task,) = tasks_by_key(admin, version_id).values()
        evict(admin, task)
        # The material is gone locally, so the restore has to download with the credential.
        shutil.rmtree(service.settings.cache_dir / "tasks" / task["content"]["digest"][7:])
        assert admin.delete(f"/api/v1/admin/credentials/{credential['id']}").status_code in (
            200,
            202,
        )
        run = submit(admin, version_id).json()["data"]
        wait_run(admin, run["run_id"])
        (attempt,) = trial_attempts(service, run["run_id"])  # not retried
        assert attempt.error_json["detail"]["kind"] == "auth"
        assert attempt.error_json["detail"]["suggested_actions"] == [
            {"type": "check_dataset_credential"}
        ]


# --- eviction and superseded restores --------------------------------------------------


def test_an_eviction_that_raises_unexpectedly_is_reported_as_failed(service, source, environment):
    source.add("demo", "solo", "v1", ["solo"])
    with running(service) as client:
        admin = setup_admin(service, client)
        version_id, _ = import_and_prepare(admin, name="solo")
        (task,) = tasks_by_key(admin, version_id).values()

        async def broken(expected):
            raise FileNotFoundError("docker")

        environment.evict = broken
        prep = task["local_preparation"]["id"]
        op = admin.post(f"/api/v1/admin/preparations/{prep}/evict", expect=202).json()["data"]
        done = wait_operation(admin, op["id"])
        assert done["status"] == "failed"
        assert done["error"]["code"] == "EVICTION_FAILED"
        with service.db.read() as conn:  # the claim is released either way
            row = conn.execute(
                select(t.preparations.c.eviction_operation_id).where(t.preparations.c.id == prep)
            ).scalar()
        assert row is None


def test_the_waiters_of_a_superseded_restore_are_closed(service, source, environment):
    with running(service) as client:
        admin, version_id, task = solo(service, source, client)
        evict(admin, task)
        environment.gate = asyncio.Event()
        run = submit(admin, version_id).json()["data"]
        preparation = f"/api/v1/preparations/{task['local_preparation']['id']}"
        old = until(
            lambda: (d := admin.get(preparation).json()["data"])["operation_id"] and d,
            message="restore",
        )["operation_id"]
        with service.db.write() as conn:  # another generation took over the key
            conn.execute(
                update(t.preparations)
                .where(t.preparations.c.id == task["local_preparation"]["id"])
                .values(generation=t.preparations.c.generation + 1)
            )
        on_loop(service, environment.gate.set)
        assert wait_operation(admin, old)["error"]["code"] == "STALE_PREPARATION_LEASE"
        assert wait_run(admin, run["run_id"])["counts"]["scored"] == 1
        with service.db.read() as conn:
            states = (
                conn.execute(
                    select(t.preparation_waiters.c.state).where(
                        t.preparation_waiters.c.operation_id == old
                    )
                )
                .scalars()
                .all()
            )
        assert states and "waiting" not in states


# --- accepted runs keep their snapshot ---------------------------------------------------


@pytest.fixture
def admin(service):
    with service.db.write() as conn:
        conn.execute(
            t.users.insert().values(
                id="usr_admin",
                username="admin",
                display_name="a",
                role="admin",
                status="active",
                password_hash="x",
                revision=1,
                created_at="t",
                updated_at="t",
            )
        )
    return Principal("usr_admin", "admin", Role.ADMIN, Channel.WEB_SESSION, session_id="s")


def test_an_accepted_run_restores_after_its_admission_was_revoked(
    service, version_id, environment, admin
):
    """Revoking admission stops new work; a run accepted earlier still brings the
    recorded image back, also where this location has no cache record yet."""
    _, accepted, _ = service.runs.submit(
        admin,
        uuid.uuid4().hex,
        {"name": "r", "dataset_version_id": version_id, "execution_mode": "oracle"},
    )
    assert accepted["planned_trials"] == 3
    with service.db.read() as conn:
        victim = (
            conn.execute(
                select(t.task_admissions).join(t.tasks).where(t.tasks.c.task_key == "mul-01")
            )
            .mappings()
            .one()
        )
    service.datasets.revoke_admission(admin, victim["id"], "stop", victim["revision"])
    with service.db.write() as conn:
        conn.execute(delete(t.preparations).where(t.preparations.c.task_id == victim["task_id"]))
    environment.images.clear()  # nothing is cached
    service.worker.start_epoch()
    asyncio.run(service.worker.drain())
    with service.db.read() as conn:
        counts = conn.execute(
            select(t.trials.c.task_id, t.trials.c.state, t.trials.c.reason_code)
        ).all()
    assert {row.state for row in counts} == {"scored"}, counts
    assert sorted(environment.loads) == ["add-01", "add-02", "mul-01"]


# --- the cache disappears behind the platform's back -------------------------------------


def test_a_missing_image_marks_the_cache_stale_and_restores_it(service, source, environment):
    with running(service) as client:
        admin, version_id, task = solo(service, source, client)
        environment.images.clear()  # someone ran docker image prune
        run = submit(admin, version_id).json()["data"]
        assert wait_run(admin, run["run_id"])["counts"]["scored"] == 1
        assert environment.loads == ["solo"]  # brought back from the original
        prep = admin.get(f"/api/v1/preparations/{task['local_preparation']['id']}").json()["data"]
        assert prep["cache_state"] == "ready" and prep["generation"] == 2
        assert run_leftovers(service, run["run_id"]) == ([], [])
