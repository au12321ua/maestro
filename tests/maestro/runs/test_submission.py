"""Run submission: idempotency, PAT quota, admission races and request validation."""

import threading
import uuid

from sqlalchemy import func, select, update
from support import create_pat, invite_member, running, setup_admin, submit, wait_operation
from support.configuration import error
from support.runs import batch_body, post_batch, rerun

from maestro.db import schema as t
from maestro.services.configuration import Resolver


def count(service, table):
    with service.db.read() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar()


def test_same_key_same_body_returns_original_response(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        pat = create_pat(admin, run_limit=5)
        first = submit(pat, version_id, key="key-0000001", repetitions=2)
        second = submit(pat, version_id, key="key-0000001", repetitions=2)
        assert first.status_code == second.status_code == 202
        assert first.json()["data"] == second.json()["data"]
        assert "Idempotent-Replayed" not in first.headers
        assert second.headers["Idempotent-Replayed"] == "true"
        assert first.json()["data"]["planned_trials"] == 6
        assert count(service, t.runs) == 1 and count(service, t.trials) == 6
        assert admin.get("/api/v1/me/pats").json()["data"][0]["accepted_runs"] == 1
        conflict = submit(pat, version_id, key="key-0000001", repetitions=3)
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        assert count(service, t.runs) == 1


def test_same_key_concurrent_requests_accept_once(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        pat = create_pat(admin, run_limit=10)
        results = []

        def go():
            results.append(submit(pat, version_id, key="same-key-concurrent"))

        threads = [threading.Thread(target=go) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert {r.status_code for r in results} == {202}
        assert len({r.json()["data"]["run_id"] for r in results}) == 1
        assert count(service, t.runs) == 1
        assert admin.get("/api/v1/me/pats").json()["data"][0]["accepted_runs"] == 1


def test_last_quota_unit_is_won_once(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        pat = create_pat(admin, run_limit=1)
        results = []

        def go():
            results.append(submit(pat, version_id))

        threads = [threading.Thread(target=go) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        codes = sorted(r.status_code for r in results)
        assert codes == [202] + [429] * 5, codes
        assert {r.json()["error"]["code"] for r in results if r.status_code == 429} == {
            "PAT_RUN_LIMIT_REACHED"
        }
        assert count(service, t.runs) == 1
        pats = admin.get("/api/v1/me/pats").json()["data"]
        assert pats[0]["accepted_runs"] == 1 and pats[0]["remaining_runs"] == 0


def test_web_submission_does_not_touch_pat_quota(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        pat = create_pat(admin, run_limit=1)
        submit(admin, version_id).raise_for_status()
        assert pat.get("/api/v1/me").status_code == 200
        assert admin.get("/api/v1/me/pats").json()["data"][0]["accepted_runs"] == 0


def test_blocked_tasks_are_excluded_or_rejected_before_acceptance(service, version_id):
    with service.db.write() as conn:
        task_id = conn.execute(select(t.tasks.c.id).where(t.tasks.c.task_key == "mul-01")).scalar()
        conn.execute(
            update(t.task_admissions)
            .where(t.task_admissions.c.task_id == task_id)
            .values(state="first_failed")
        )
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        pat = create_pat(admin, run_limit=3)
        tasks = pat.get(f"/api/v1/dataset-versions/{version_id}/tasks").json()["data"]
        blocked = next(x for x in tasks if x["task_key"] == "mul-01")
        assert blocked["can_submit"] is False
        assert blocked["blockers"][0]["code"] == "TASK_NOT_ADMITTED"
        rejected = submit(
            pat, version_id, task_selection={"mode": "explicit", "task_ids": [task_id]}
        )
        assert rejected.status_code == 422
        assert rejected.json()["error"]["field_errors"][0]["code"] == "TASK_NOT_ADMITTED"
        assert admin.get("/api/v1/me/pats").json()["data"][0]["accepted_runs"] == 0
        accepted = submit(pat, version_id).json()["data"]
        assert accepted["planned_trials"] == 2
        run = pat.get(f"/api/v1/runs/{accepted['run_id']}").json()["data"]
        assert run["snapshot"]["provenance"]["excluded_tasks"] == [
            {
                "task_id": task_id,
                "task_key": "mul-01",
                "blockers": [{"code": "TASK_NOT_ADMITTED", "admission_state": "first_failed"}],
            }
        ]
        assert accepted["excluded_tasks"] == run["snapshot"]["provenance"]["excluded_tasks"]


def test_admission_revoked_between_expansion_and_acceptance(service, version_id, monkeypatch):
    """The write transaction re-checks admission and charges nothing on failure."""
    original = Resolver.resolve

    def expand_then_revoke(self, conn, request):
        resolved = original(self, conn, request)
        with service.db.write() as other:
            other.execute(
                update(t.task_admissions)
                .where(t.task_admissions.c.id == resolved.tasks[0]["admission_id"])
                .values(state="revoked", revision=2)
            )
        return resolved

    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        pat = create_pat(admin, run_limit=3)
        monkeypatch.setattr(Resolver, "resolve", expand_then_revoke)
        response = submit(pat, version_id)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "ADMISSION_CHANGED"
        assert count(service, t.runs) == 0 and count(service, t.trials) == 0
        with service.db.read() as conn:
            kinds = conn.execute(select(t.operations.c.kind)).scalars().all()
        assert "run" not in kinds
        assert admin.get("/api/v1/me/pats").json()["data"][0]["accepted_runs"] == 0


def test_revoked_pat_cannot_submit_and_member_disable_blocks(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        alice = invite_member(service, admin, "alice")
        pat = create_pat(alice)
        pat_id = alice.get("/api/v1/me/pats").json()["data"][0]["id"]
        alice.delete(f"/api/v1/me/pats/{pat_id}", expect=200)
        response = submit(pat, version_id)
        assert response.status_code == 401 and response.json()["error"]["code"] == "PAT_REVOKED"


def test_request_validation_and_idempotency_key_required(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        missing = admin.post("/api/v1/runs", json={"name": "x", "dataset_version_id": version_id})
        assert missing.status_code == 422
        bad = submit(admin, version_id, repetitions=999)
        assert bad.status_code == 422
        assert bad.json()["error"]["field_errors"][0]["path"] == "/repetitions"
        short = submit(admin, version_id, key="k")
        assert short.json()["error"]["code"] == "IDEMPOTENCY_KEY_INVALID"


def test_snapshot_records_fixed_content_and_audit(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        accepted = submit(admin, version_id, client_request_id="round-1").json()["data"]
        run = admin.get(f"/api/v1/runs/{accepted['run_id']}").json()["data"]
        effective = run["snapshot"]["effective"]
        assert run["snapshot"]["config_digest"] == accepted["config_digest"]
        assert [x["task_key"] for x in effective["tasks"]] == ["add-01", "add-02", "mul-01"]
        assert all(x["content_digest"].startswith("sha256:") for x in effective["tasks"])
        found = admin.get("/api/v1/runs?client_request_id=round-1").json()["data"]
        assert [x["id"] for x in found] == [accepted["run_id"]]
        events = admin.get(
            f"/api/v1/admin/audit-events?object_type=run&object_id={accepted['run_id']}"
        ).json()["data"]
        assert [e["action"] for e in events] == ["run.submit"]
        assert events[0]["request_id"]


def test_pagination_is_stable_and_filter_bound(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        ids = [submit(admin, version_id).json()["data"]["run_id"] for _ in range(5)]
        seen, cursor = [], None
        while True:
            path = "/api/v1/runs?limit=2" + (f"&cursor={cursor}" if cursor else "")
            body = admin.get(path).json()
            seen += [x["id"] for x in body["data"]]
            cursor = body["meta"].get("next_cursor")
            if not cursor:
                break
        assert seen == list(reversed(ids))
        first = admin.get("/api/v1/runs?limit=2").json()["meta"]["next_cursor"]
        moved = admin.get(f"/api/v1/runs?limit=2&state=queued&cursor={first}")
        assert moved.json()["error"]["code"] == "CURSOR_FILTER_MISMATCH"


def test_a_retry_replays_after_the_plan_changed_or_was_archived(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        template = {"dataset_version_id": version_id, "execution_mode": "oracle"}
        plan = admin.post(
            "/api/v1/plans", json={"name": "p", "template": template}, expect=201
        ).json()["data"]
        body = {"name": "from plan", "plan_id": plan["id"]}
        key = uuid.uuid4().hex
        first = admin.post("/api/v1/runs", json=body, headers={"Idempotency-Key": key})
        assert first.status_code == 202, first.text
        admin.put(
            f"/api/v1/plans/{plan['id']}",
            json={"expected_revision": 1, "template": {**template, "repetitions": 2}},
            expect=200,
        )
        again = admin.post("/api/v1/runs", json=body, headers={"Idempotency-Key": key})
        assert again.status_code == 202, again.text
        assert again.json()["data"]["run_id"] == first.json()["data"]["run_id"]
        admin.post(f"/api/v1/plans/{plan['id']}/archive", json={"archived": True}, expect=200)
        again = admin.post("/api/v1/runs", json=body, headers={"Idempotency-Key": key})
        assert again.json()["data"]["run_id"] == first.json()["data"]["run_id"]


def test_batch_and_rerun_retries_replay_after_limits_and_sources_changed(
    service, version_id, executor
):
    with running(service) as client:
        admin = setup_admin(service, client)
        oracle = {"execution_mode": "oracle"}
        body = batch_body(version_id, oracle, oracle)
        accepted = post_batch(admin, body, key="batch-key").json()["data"]
        wait_operation(admin, accepted["operation_id"])
        admin.put(
            "/api/v1/admin/team-policy",
            json={"expected_revision": 0, "values": {"batch_max_runs": 1}},
            expect=200,
        )
        again = post_batch(admin, body, key="batch-key")
        assert again.status_code == 202, again.text
        assert again.json()["data"]["batch_id"] == accepted["batch_id"]

        source = accepted["runs"][0]["run_id"]
        first = rerun(admin, source, expect=202, key="rerun-key")
        admin.delete(f"/api/v1/runs/{source}", expect=200)
        again = rerun(admin, source, expect=202, key="rerun-key")
        assert again.json()["data"]["run_id"] == first.json()["data"]["run_id"]


def test_fields_beside_a_resolution_id_are_refused(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        found = admin.post(
            "/api/v1/config-resolutions",
            json={"dataset_version_id": version_id, "execution_mode": "oracle"},
            expect=201,
        ).json()["data"]
        response = admin.post(
            "/api/v1/runs",
            json={"name": "x", "resolution_id": found["resolution_id"], "repetitions": 3},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert error(response) == (422, "CONFIG_VALIDATION_FAILED")
        assert response.json()["error"]["field_errors"][0]["path"] == "/repetitions"
