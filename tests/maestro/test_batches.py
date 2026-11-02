"""Batches: several runs accepted all or none, PAT quota
charged per run in the same transaction, members run and end on their own."""

import uuid
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import func, select
from support import create_pat, invite_member, running, setup_admin, submit, wait_operation
from test_configuration import agent_body, error
from test_datasets import tasks_by_key
from test_e2e_run import prepare_model

from maestro.db import schema as t


def batch_body(version_id, *configs, name="sweep") -> dict:
    return {
        "name": name,
        "runs": [
            {"name": f"member {i}", "dataset_version_id": version_id, **config}
            for i, config in enumerate(configs)
        ],
    }


def post_batch(api, body, key=None):
    return api.post(
        "/api/v1/run-batches", json=body, headers={"Idempotency-Key": key or uuid.uuid4().hex}
    )


def counts(service) -> dict:
    with service.db.read() as conn:
        return {
            "runs": conn.execute(select(func.count()).select_from(t.runs)).scalar(),
            "trials": conn.execute(select(func.count()).select_from(t.trials)).scalar(),
            "batches": conn.execute(
                select(func.count()).select_from(t.submission_batches)
            ).scalar(),
            "accepted_runs": conn.execute(select(func.sum(t.pats.c.accepted_runs))).scalar(),
        }


def test_lost_responses_concurrent_batches_and_quota(service, version_id):
    """Retrying the same batch request preserves run identities and quota."""
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "member")
        first = create_pat(member, run_limit=2)
        # A second client with the same token, so the two requests really overlap.
        second = type(first)(type(first.client)(first.client.app), token=first.token)
        oracle = {"execution_mode": "oracle"}
        jobs = [
            (first, batch_body(version_id, oracle, {**oracle, "repetitions": 2}), "batch-key-a"),
            (second, batch_body(version_id, oracle, oracle), "batch-key-b"),
        ]
        with ThreadPoolExecutor(2) as pool:
            responses = list(pool.map(lambda job: post_batch(*job), jobs))
        assert sorted(r.status_code for r in responses) == [202, 429], [r.text for r in responses]
        winner = next(i for i, r in enumerate(responses) if r.status_code == 202)
        assert responses[1 - winner].json()["error"]["code"] == "PAT_RUN_LIMIT_REACHED"
        accepted = responses[winner].json()["data"]
        assert len(accepted["runs"]) == 2

        # The response was lost: the same key brings back the same batch, charging nothing.
        _, body, key = jobs[winner]
        again = post_batch(first, body, key)
        assert again.status_code == 202 and again.headers["Idempotent-Replayed"] == "true"
        assert again.json()["data"] == accepted
        # Further submissions, single or batch, find no quota.
        assert error(post_batch(first, body)) == (429, "PAT_RUN_LIMIT_REACHED")
        assert error(submit(first, version_id)) == (429, "PAT_RUN_LIMIT_REACHED")
        assert counts(service) == {
            "runs": 2,
            "trials": accepted["planned_trials"],
            "batches": 1,
            "accepted_runs": 2,
        }
        found = member.get(f"/api/v1/run-batches/{accepted['batch_id']}", expect=200).json()
        found = found["data"]
        assert {r["id"] for r in found["runs"]} == {r["run_id"] for r in accepted["runs"]}
        assert all(r["batch_id"] == accepted["batch_id"] for r in found["runs"])
        assert found["status"] == "accepted" and found["progress"]["runs"] == 2


def test_one_blocked_member_refuses_the_whole_batch(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        model_id, harness_id = prepare_model(admin)
        pat = create_pat(admin, run_limit=5)
        good = agent_body(model_id, harness_id)
        bad = {**good, "repetitions": 99}
        refused = post_batch(pat, batch_body(version_id, good, bad))
        assert error(refused) == (422, "CONFIG_VALIDATION_FAILED")
        assert refused.json()["error"]["field_errors"][0]["path"] == "/runs/1/repetitions"
        missing = post_batch(pat, batch_body(version_id, good, {"execution_mode": "agent"}))
        assert missing.json()["error"]["field_errors"][0]["path"] == "/runs/1/model_config_id"
        assert counts(service) == {"runs": 0, "trials": 0, "batches": 0, "accepted_runs": 0}

        preview = pat.post(
            "/api/v1/run-batches/preview", json=batch_body(version_id, good, good, bad)
        ).json()["data"]
        assert not preview["can_submit"] and preview["pat_quota"] == {"needed": 3, "left": 5}
        assert preview["runs"][2]["blockers"][0]["code"] == "LIMIT_EXCEEDED"
        assert preview["duplicate_configs"] == [preview["runs"][0]["config_digest"]]
        assert counts(service)["accepted_runs"] == 0

        admin.put(
            "/api/v1/admin/team-policy",
            json={"values": {"batch_max_runs": 2}, "expected_revision": 0},
            expect=200,
        )
        too_many = post_batch(pat, batch_body(version_id, good, good, good))
        assert error(too_many) == (413, "BATCH_TOO_LARGE")


def test_members_run_on_their_own_and_cancelling_stops_the_rest(service, version_id, executor):
    executor.script = {"add-02": ["hang"]}
    with running(service) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "member")
        tasks = tasks_by_key(member, version_id)

        def only(key):
            return {
                "execution_mode": "oracle",
                "task_selection": {"mode": "explicit", "task_ids": [tasks[key]["id"]]},
            }

        accepted = post_batch(member, batch_body(version_id, only("add-01"), only("add-02")))
        accepted = accepted.json()["data"]
        quick, stuck = (r["run_id"] for r in accepted["runs"])
        wait_operation(member, accepted["runs"][0]["operation_id"])
        batch = member.get(f"/api/v1/run-batches/{accepted['batch_id']}").json()["data"]
        assert batch["status"] == "running" and batch["progress"]["terminal_runs"] == 1
        other = invite_member(service, admin, "other")
        path = f"/api/v1/run-batches/{accepted['batch_id']}/cancel"
        assert error(other.post(path)) == (403, "FORBIDDEN")
        cancelled = member.post(path, expect=200).json()["data"]
        assert cancelled["stop_requested"] and "cancel" not in cancelled["allowed_actions"]
        operation = wait_operation(member, accepted["operation_id"])
        assert operation["status"] == "cancelled"
        assert operation["result"]["finished"] == 1 and operation["result"]["cancelled"] == 1
        assert operation["result"]["scored_trials"] == 1
        runs = {r["id"]: r for r in member.get(path.removesuffix("/cancel")).json()["data"]["runs"]}
        assert runs[quick]["state"] == "finished" and runs[stuck]["state"] == "cancelled"
        listed = member.get("/api/v1/run-batches").json()["data"]
        assert [b["id"] for b in listed] == [accepted["batch_id"]]
