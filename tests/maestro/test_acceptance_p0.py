"""Acceptance races, cleanup failures, cache restoration and revoked admission."""

import asyncio
import shutil
import threading
import uuid

import pytest
from sqlalchemy import func, select
from support import create_pat, invite_member, running, setup_admin, submit
from test_configuration import resolve
from test_datasets import evict, tasks_by_key, wait_run
from test_preparation import solo
from test_reruns import rerun
from test_worker import _drain, accept, trials_by_key

from maestro.db import schema as t
from maestro.db.engine import Database
from maestro.domain.states import CleanupState, Role
from maestro.services.principal import Channel, Principal
from maestro.services.worker import RunSink, StaleLease, Worker, _Cancel
from maestro.testing.fakes import scored


@pytest.fixture
def owner(service):
    with service.db.write() as conn:
        conn.execute(
            t.users.insert().values(
                id="usr_owner",
                username="owner",
                display_name="o",
                role="member",
                status="active",
                password_hash="x",
                revision=1,
                created_at="t",
                updated_at="t",
            )
        )
    return Principal("usr_owner", "owner", Role.MEMBER, Channel.WEB_SESSION, session_id="s")


def result_rows(service, run_id) -> dict:
    with service.db.read() as conn:
        rows = conn.execute(
            select(t.trials.c.id, func.count(t.trial_results.c.trial_id))
            .outerjoin(t.trial_results, t.trial_results.c.trial_id == t.trials.c.id)
            .where(t.trials.c.run_id == run_id)
            .group_by(t.trials.c.id)
        ).all()
    return dict(rows)


def test_f02_racing_claimers_get_one_lease_and_execute_once(
    service, owner, version_id, settings, executor, store
):
    data = accept(service, owner, version_id, concurrency=3)
    others = [
        Worker(Database.open(settings.database), executor, store, service.worker.capacity)
        for _ in range(3)
    ]
    claimers = [service.worker, *others]
    for worker in claimers:
        worker.start_epoch()
    barrier = threading.Barrier(len(claimers))
    claims = [None] * len(claimers)

    def claim(index):
        barrier.wait()
        claims[index] = claimers[index]._claim()

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(len(claimers))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    won = [(w, c) for w, c in zip(claimers, claims, strict=True) if c is not None]
    assert len(won) == 1
    winner, (run_id, lease, spec) = won[0]
    assert run_id == data["run_id"]
    with service.db.read() as conn:
        run = conn.execute(select(t.runs).where(t.runs.c.id == run_id)).mappings().one()
        leases = set(
            conn.execute(
                select(t.work_items.c.lease_token)
                .join(t.trials, t.trials.c.id == t.work_items.c.trial_id)
                .where(t.trials.c.run_id == run_id)
            ).scalars()
        )
    assert run["lease_token"] == lease and run["service_epoch"] == winner.epoch
    assert leases == {lease}
    assert all(w._claim() is None for w in claimers)  # nothing left for a late claimer
    asyncio.run(winner._execute(run_id, lease, spec, _Cancel()))
    for trial in service.runs.trials(run_id):
        assert trial["state"] == "scored" and len(trial["attempts"]) == 1
    for worker in others:
        worker.db.dispose()


@pytest.mark.parametrize("first", ["result", "cancel"])
def test_f03_cancel_and_final_result_accept_one_terminal_state(service, owner, version_id, first):
    """Whichever the server commits first wins; a score recorded earlier stays."""
    data = accept(service, owner, version_id, concurrency=3)
    worker = service.worker
    worker.start_epoch()
    run_id, lease, spec = worker._claim()
    done, racing, unstarted = spec.trials
    sink = RunSink(worker, run_id, lease)

    async def scenario():
        await sink.attempt_started(done.trial_id, 1)
        await sink.attempt_finished(done.trial_id, 1, scored(0.5), final=True)
        await sink.attempt_started(racing.trial_id, 1)
        await sink.phase_changed(racing.trial_id, 1, "agent")
        service.runs.cancel(owner, run_id)  # the result is held while the stop arrives
        if first == "result":
            await sink.attempt_finished(racing.trial_id, 1, scored(1.0), final=True)
            worker._close(run_id, lease, CleanupState.COMPLETE, None)
        else:
            worker._close(run_id, lease, CleanupState.COMPLETE, None)
            with pytest.raises(StaleLease):
                await sink.attempt_finished(racing.trial_id, 1, scored(1.0), final=True)

    asyncio.run(scenario())
    trials = {x["id"]: x for x in service.runs.trials(run_id)}
    assert trials[done.trial_id]["state"] == "scored"
    assert trials[done.trial_id]["result"]["score"] == 0.5
    late = trials[racing.trial_id]
    if first == "result":
        assert (late["state"], late["result"]["score"]) == ("scored", 1.0)
    else:
        assert (late["state"], late["reason_code"]) == ("cancelled", "USER_CANCELLED")
        assert late["result"] is None
    assert trials[unstarted.trial_id]["state"] == "not_started"
    assert result_rows(service, run_id) == {
        done.trial_id: 1,
        racing.trial_id: 1 if first == "result" else 0,
        unstarted.trial_id: 0,
    }
    run = service.runs.get(owner, data["run_id"])
    assert run["state"] == "cancelled" and run["stop_reason"] == "USER_CANCELLED"


def test_f07_storage_and_cleanup_failures_keep_scores_and_both_show(
    service, owner, version_id, store, executor
):
    store.fail_put = True
    executor.release = False
    data = accept(service, owner, version_id)
    asyncio.run(_drain(service))
    run = service.runs.get(owner, data["run_id"])
    assert run["state"] == "finished" and run["counts"]["scored"] == 3
    assert (run["artifact_state"], run["cleanup_state"]) == ("failed", "failed")
    for (trial,) in trials_by_key(service, data["run_id"]).values():
        assert trial["state"] == "scored" and trial["result"]["score"] == 1.0
        assert (trial["artifact_state"], trial["cleanup_state"]) == ("failed", "failed")


def fixed_content(task) -> tuple:
    content = task["content"]
    resources = {(r["resource_kind"], r["digest"], r["availability"]) for r in content["resources"]}
    return content["digest"], content["material_digest"], resources


def test_f21_rerun_after_cache_eviction_restores_by_digest(service, source, environment):
    with running(service) as client:
        admin, version_id, task = solo(service, source, client)
        member = invite_member(service, admin, "member")
        pat = create_pat(member, permissions=("experiment",), run_limit=5)
        first = submit(pat, version_id).json()["data"]
        assert wait_run(pat, first["run_id"])["counts"]["scored"] == 1
        evict(admin, task)
        assert environment.restores == []
        # From here on the administrator does nothing.
        again = rerun(pat, first["run_id"], 202).json()["data"]
        assert again["config_digest"] == first["config_digest"]
        assert wait_run(pat, again["run_id"])["counts"]["scored"] == 1
        assert environment.restores == ["solo"] and environment.loads == ["solo"]
        assert environment.builds == 1  # loaded from the fixed original, not rebuilt
        after = tasks_by_key(pat, version_id)["solo"]
        assert after["admission"] == task["admission"]
        assert fixed_content(after) == fixed_content(task)
        assert after["local_preparation"]["cache_state"] == "ready"
        assert member.get("/api/v1/me/pats").json()["data"][0]["accepted_runs"] == 2


def content_gone(service, source, settings, client):
    """A finished run, then its exact task content lost locally and upstream."""
    admin, version_id, task = solo(service, source, client)
    member = invite_member(service, admin, "member")
    pat = create_pat(member, permissions=("experiment",), run_limit=5)
    accepted = submit(pat, version_id).json()["data"]
    wait_run(pat, accepted["run_id"])
    evict(admin, task)
    shutil.rmtree(settings.cache_dir / "tasks" / task["content"]["digest"][7:])
    source.gone.add(task["content"]["digest"])
    # One run finds the content gone and marks it unavailable.
    failed = submit(pat, version_id).json()["data"]
    assert wait_run(pat, failed["run_id"])["counts"]["errors"] == 1
    (blocker,) = tasks_by_key(pat, version_id)["solo"]["blockers"]
    assert blocker["code"] == "RESOURCE_CONTENT_UNAVAILABLE"
    return member, pat, version_id, task, accepted["run_id"]


def test_f23_rerun_is_refused_before_acceptance_when_the_exact_content_is_gone(
    service, source, settings, environment
):
    with running(service) as client:
        member, pat, _, _, source_run = content_gone(service, source, settings, client)
        refused = rerun(pat, source_run)
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["code"] == "TASKS_NOT_SUBMITTABLE"
        codes = [e["code"] for e in refused.json()["error"]["field_errors"]]
        assert codes == ["RESOURCE_CONTENT_UNAVAILABLE"]
        pats = member.get("/api/v1/me/pats").json()["data"]
        assert pats[0]["accepted_runs"] == 2  # the refused rerun charged nothing


def test_f23_refusals_name_the_expected_digest(service, source, settings, environment):
    with running(service) as client:
        _, pat, version_id, task, source_run = content_gone(service, source, settings, client)
        digest = task["content"]["digest"]
        for refused in (rerun(pat, source_run), submit(pat, version_id)):
            assert refused.status_code == 422, refused.text
            assert digest in refused.text


def test_f26_cache_eviction_alone_does_not_invalidate_a_resolution(service, version_id):
    with running(service) as client:
        admin = setup_admin(service, client)
        pat = create_pat(admin, run_limit=5)
        resolution = resolve(pat, version_id)
        assert resolution["blockers"] == []
        for task in tasks_by_key(admin, version_id).values():
            evict(admin, task)
        accepted = pat.post(
            "/api/v1/runs",
            json={"name": "after eviction", "resolution_id": resolution["resolution_id"]},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert accepted.status_code == 202, accepted.text
        data = accepted.json()["data"]
        assert data["config_digest"] == resolution["config_digest"]
        assert wait_run(pat, data["run_id"])["counts"]["scored"] == 3
        assert admin.get("/api/v1/me/pats").json()["data"][0]["accepted_runs"] == 1
