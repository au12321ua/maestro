"""Reruns and modified runs: new runs from a source run's
snapshot, never silently changed; modified runs record their differences."""

import time
import uuid

from support import create_pat, invite_member, running, setup_admin, submit, wait_operation
from support.attachments import attach
from support.configuration import agent_body, error, prepare_model, resolve
from support.datasets import tasks_by_key
from support.runs import rerun

from maestro.core.errors import MaestroError


def trials_of(api, run_id) -> list[dict]:
    return api.get(f"/api/v1/runs/{run_id}/trials", expect=200).json()["data"]


def wait_trials(api, run_id, predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while True:
        trials = trials_of(api, run_id)
        if predicate(trials):
            return trials
        if time.monotonic() > deadline:
            raise AssertionError([(t["task_key"], t["state"]) for t in trials])
        time.sleep(0.02)


def snapshot(api, run_id) -> dict:
    return api.get(f"/api/v1/runs/{run_id}", expect=200).json()["data"]["snapshot"]


def test_cancel_then_rerun_what_did_not_finish_then_modify(service, version_id, executor):
    """Rerun a selected task without changing the source run."""
    executor.script = {"add-02": ["hang"]}
    with running(service) as client:
        admin = setup_admin(service, client)
        model_id, harness_id = prepare_model(admin)
        member = invite_member(service, admin, "member")
        pat = create_pat(member)
        tasks = {k: v["id"] for k, v in tasks_by_key(member, version_id).items()}
        selection = {"mode": "explicit", "task_ids": [tasks["add-01"], tasks["add-02"]]}
        body = agent_body(model_id, harness_id, task_selection=selection)
        source = submit(pat, version_id, **body).json()["data"]
        wait_trials(
            pat,
            source["run_id"],
            lambda ts: (
                {(t["task_key"], t["state"]) for t in ts}
                >= {("add-01", "scored"), ("add-02", "executing")}
            ),
        )
        pat.post(f"/api/v1/runs/{source['run_id']}/cancel", expect=200)
        wait_operation(pat, source["operation_id"])
        before = {t["id"]: (t["state"], t["result"]) for t in trials_of(pat, source["run_id"])}
        states = {t["task_key"]: t["state"] for t in trials_of(pat, source["run_id"])}
        assert states == {"add-01": "scored", "add-02": "cancelled"}

        executor.script = {}
        accepted = rerun(pat, source["run_id"], 202, task_selection={"mode": "unfinished"})
        accepted = accepted.json()["data"]
        wait_operation(pat, accepted["operation_id"])
        new = pat.get(f"/api/v1/runs/{accepted['run_id']}").json()["data"]
        assert new["relation"] == "rerun" and new["source_run_id"] == source["run_id"]
        assert new["name"] == "baseline (rerun)" and new["owner"]["username"] == "member"
        assert [t["task_key"] for t in trials_of(pat, accepted["run_id"])] == ["add-02"]
        provenance = new["snapshot"]["provenance"]
        origin = provenance["source"]
        assert origin["relation"] == "rerun"
        assert origin["task_causes"] == {tasks["add-02"]: ["USER_CANCELLED"]}
        old = snapshot(pat, source["run_id"])
        assert origin["config_digest"] == old["config_digest"]
        # Same configuration but the narrower task set; same comparison conditions.
        assert new["snapshot"]["effective"]["model"] == old["effective"]["model"]
        assert new["snapshot"]["comparison_digest"] == old["comparison_digest"]
        # The source keeps its results and gains no trials.
        after = {t["id"]: (t["state"], t["result"]) for t in trials_of(pat, source["run_id"])}
        assert after == before

        # Modified run: start from the source's request, add an attachment.
        tools = attach(member)
        edited = pat.get(f"/api/v1/runs/{source['run_id']}/request", expect=200).json()["data"]
        assert edited["repetitions"] == 1 and edited["model_revision"] == 1
        edited["attachments"] = [tools]
        resolution = resolve(pat, version_id, **{k: v for k, v in edited.items() if v is not None})
        assert resolution["blockers"] == []
        modified = pat.post(
            "/api/v1/runs",
            json={
                "name": "with tools",
                "resolution_id": resolution["resolution_id"],
                "source_run_id": source["run_id"],
            },
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert modified.status_code == 202, modified.text
        run = pat.get(f"/api/v1/runs/{modified.json()['data']['run_id']}").json()["data"]
        assert run["relation"] == "modified" and run["source_run_id"] == source["run_id"]
        changes = run["snapshot"]["provenance"]["source"]["differences"]
        assert [d["path"] for d in changes] == ["/attachments", "/bootstrap_timeout_sec"]
        # Each new run charged one; the source run counted once when it was accepted.
        pats = member.get("/api/v1/me/pats").json()["data"]
        assert next(p for p in pats if p["permissions"] == ["experiment"])["accepted_runs"] == 3


def test_a_rerun_keeps_the_source_configuration_or_refuses(service, version_id):
    with running(service) as client:
        admin = setup_admin(service, client)
        model_id, harness_id = prepare_model(admin)
        source = submit(admin, version_id, **agent_body(model_id, harness_id)).json()["data"]
        wait_operation(admin, source["operation_id"])
        old = snapshot(admin, source["run_id"])

        # Later team defaults and a new model revision do not reach a rerun.
        admin.put(
            "/api/v1/admin/team-policy",
            json={"values": {"run_default_concurrency": 2}, "expected_revision": 0},
            expect=200,
        )
        model = admin.get(f"/api/v1/model-configs/{model_id}").json()["data"]
        admin.put(
            f"/api/v1/admin/model-configs/{model_id}",
            json={
                "expected_revision": 1,
                "provider": "demo",
                "protocol": "openai-compatible",
                "endpoint": "https://models.example.test/v2",
                "model_id": "demo-model-2",
                "credential_id": model["credential_id"],
            },
            expect=200,
        )
        again = rerun(admin, source["run_id"], 202, key="same-rerun-key").json()["data"]
        assert rerun(admin, source["run_id"], 202, key="same-rerun-key").json()["data"] == again
        wait_operation(admin, again["operation_id"])
        new = snapshot(admin, again["run_id"])
        assert new["config_digest"] == old["config_digest"]
        assert new["effective"]["concurrency"] == old["effective"]["concurrency"] == 4
        assert new["effective"]["model"]["endpoint"] == "https://models.example.test/v1"
        assert (
            new["provenance"]["source"]["source_field_sources"]
            == old["provenance"]["field_sources"]
        )
        # A modified run started from the same request also keeps the old revision...
        edited = admin.get(f"/api/v1/runs/{source['run_id']}/request").json()["data"]
        assert edited["concurrency"] == 4 and edited["model_revision"] == 1

        # ...but a tighter limit, or a disabled model, refuses instead of running otherwise.
        admin.put(
            "/api/v1/admin/team-policy",
            json={
                "values": {"run_default_concurrency": 2, "run_max_concurrency": 2},
                "expected_revision": 1,
            },
            expect=200,
        )
        refused = rerun(admin, source["run_id"])
        assert error(refused) == (422, "CONFIG_VALIDATION_FAILED")
        assert refused.json()["error"]["field_errors"][0]["path"] == "/concurrency"
        admin.put(
            "/api/v1/admin/team-policy",
            json={"values": {"run_max_concurrency": 10}, "expected_revision": 2},
            expect=200,
        )
        admin.post(f"/api/v1/admin/model-configs/{model_id}/enabled", json={"enabled": False})
        assert error(rerun(admin, source["run_id"])) == (422, "CONFIG_UNAVAILABLE")

        assert error(rerun(admin, source["run_id"], task_selection={"mode": "unfinished"})) == (
            422,
            "NOTHING_TO_RERUN",
        )
        bad = rerun(admin, source["run_id"], task_selection={"mode": "explicit", "task_ids": ["x"]})
        assert error(bad) == (422, "CONFIG_VALIDATION_FAILED")


def test_rerun_needs_experiment_rights_and_a_live_source(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "member")
        reader = create_pat(member, permissions=())
        source = submit(admin, version_id).json()["data"]
        assert error(rerun(reader, source["run_id"])) == (403, "PAT_PERMISSION_INSUFFICIENT")
        # Unfinished work can only be rerun once the source ended.
        refused = rerun(member, source["run_id"], task_selection={"mode": "unfinished"})
        assert error(refused) == (409, "RUN_NOT_FINISHED")
        admin.post(f"/api/v1/runs/{source['run_id']}/cancel", expect=200)
        # Anyone in the team may rerun; the new run is theirs.
        mine = rerun(member, source["run_id"], 202).json()["data"]
        run = member.get(f"/api/v1/runs/{mine['run_id']}").json()["data"]
        assert run["owner"]["username"] == "member"
        admin.delete(f"/api/v1/runs/{source['run_id']}", expect=200)
        assert error(rerun(member, source["run_id"])) == (409, "SOURCE_RUN_DELETED")
        assert error(member.get(f"/api/v1/runs/{source['run_id']}/request")) == (
            409,
            "SOURCE_RUN_DELETED",
        )


class _Low:
    def refuse_if_low(self, work="runs"):
        raise MaestroError("STORAGE_LOW", "low", 503)


def test_a_rerun_respects_the_storage_watermark(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        source = submit(admin, version_id).json()["data"]["run_id"]
        admin.post(f"/api/v1/runs/{source}/cancel", expect=200)
        service.runs.disk = _Low()
        assert error(rerun(admin, source)) == (503, "STORAGE_LOW")
