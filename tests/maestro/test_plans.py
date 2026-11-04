"""Experiment plans: saved partial configurations, a layer
between the team policy and the request, fixed per run by revision."""

import uuid

from support import create_pat, invite_member, running, setup_admin, submit, wait_operation
from test_configuration import agent_body, error, resolve
from test_e2e_run import prepare_model


def create_plan(api, expect=201, **body):
    response = api.post("/api/v1/plans", json={"name": "nightly", **body})
    if expect is not None:
        assert response.status_code == expect, response.text
    return response.json()["data"] if expect and expect < 300 else response


def test_a_plan_fills_in_what_the_request_leaves_out(service, version_id):
    with running(service) as client:
        admin = setup_admin(service, client)
        model_id, harness_id = prepare_model(admin)
        member = invite_member(service, admin, "member")
        pat = create_pat(member)
        template = {
            "dataset_version_id": version_id,
            **agent_body(model_id, harness_id),
            "repetitions": 2,
            "resources": {"memory_mb": 4096},
        }
        plan = create_plan(pat, template=template)
        assert plan["revision"] == 1 and plan["owner_username"] == "member"

        found = member.post(
            "/api/v1/config-resolutions", json={"plan_id": plan["id"], "concurrency": 2}
        ).json()["data"]
        assert found["blockers"] == []
        effective, sources = found["effective_config"], found["field_sources"]
        assert effective["repetitions"] == 2 and effective["concurrency"] == 2
        planned = {"layer": "plan", "source_id": plan["id"], "revision": 1}
        assert sources["/repetitions"] == planned
        assert sources["/resources/memory_mb"] == planned
        assert sources["/model"] == planned and sources["/harness"] == planned
        assert sources["/concurrency"] == {"layer": "override"}
        assert sources["/run_deadline_hours"]["layer"] == "team"
        assert found["reference_revisions"]["plan"] == {"id": plan["id"], "revision": 1}
        # The request's own value wins over the plan's.
        mine = resolve(
            member, version_id, plan_id=plan["id"], execution_mode="agent", repetitions=1
        )
        assert mine["effective_config"]["repetitions"] == 1
        assert mine["field_sources"]["/repetitions"] == {"layer": "override"}

        accepted = pat.post(
            "/api/v1/runs",
            json={"name": "from plan", "plan_id": plan["id"]},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert accepted.status_code == 202, accepted.text
        run_id = accepted.json()["data"]["run_id"]
        wait_operation(pat, accepted.json()["data"]["operation_id"])

        # Editing the plan makes a new revision; the run keeps what it used.
        updated = pat.put(
            f"/api/v1/plans/{plan['id']}",
            json={"expected_revision": 1, "template": {**template, "repetitions": 3}},
            expect=200,
        ).json()["data"]
        assert updated["revision"] == 2 and updated["template"]["repetitions"] == 3
        stale = pat.put(f"/api/v1/plans/{plan['id']}", json={"expected_revision": 1, "name": "x"})
        assert error(stale) == (409, "PLAN_REVISION_CHANGED")
        run = pat.get(f"/api/v1/runs/{run_id}").json()["data"]
        assert run["plan_id"] == plan["id"] and run["plan_revision"] == 1
        assert run["snapshot"]["effective"]["repetitions"] == 2
        listed = pat.get(f"/api/v1/runs?plan_id={plan['id']}").json()["data"]
        assert [r["id"] for r in listed] == [run_id]
        # A rerun reproduces the run, not the plan's newer revision.
        again = pat.post(
            f"/api/v1/runs/{run_id}/rerun", json={}, headers={"Idempotency-Key": uuid.uuid4().hex}
        )
        assert again.status_code == 202, again.text
        rerun = pat.get(f"/api/v1/runs/{again.json()['data']['run_id']}").json()["data"]
        assert rerun["snapshot"]["effective"]["repetitions"] == 2

        pinned = member.post(
            "/api/v1/config-resolutions", json={"plan_id": plan["id"], "plan_revision": 1}
        )
        assert error(pinned) == (409, "PLAN_REVISION_CHANGED")


def test_plans_belong_to_their_creator_and_are_shared_for_reading(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        alice = invite_member(service, admin, "alice")
        bob = invite_member(service, admin, "bob")
        reader = create_pat(bob, permissions=())
        plan = create_plan(alice, template={"dataset_version_id": version_id, "repetitions": 2})
        assert [p["id"] for p in reader.get("/api/v1/plans").json()["data"]] == [plan["id"]]
        assert create_plan(reader, expect=403).status_code == 403
        # Others copy to change; administrators archive but do not edit content.
        edit = {"expected_revision": 1, "name": "mine now"}
        assert error(bob.put(f"/api/v1/plans/{plan['id']}", json=edit)) == (403, "FORBIDDEN")
        assert error(admin.put(f"/api/v1/plans/{plan['id']}", json=edit)) == (403, "FORBIDDEN")
        copied = bob.post(f"/api/v1/plans/{plan['id']}/copy", json={}, expect=201).json()["data"]
        assert copied["owner_username"] == "bob" and copied["copied_from"] == plan["id"]
        assert copied["template"] == plan["template"] and copied["name"] == "nightly (copy)"
        archive = f"/api/v1/plans/{plan['id']}/archive"
        assert bob.post(archive, json={"archived": True}).status_code == 403
        admin.post(archive, json={"archived": True}, expect=200)
        assert [p["id"] for p in bob.get("/api/v1/plans").json()["data"]] == [copied["id"]]
        used = bob.post(
            "/api/v1/config-resolutions",
            json={"plan_id": plan["id"], "execution_mode": "oracle"},
        )
        assert error(used) == (409, "PLAN_ARCHIVED")
        # Templates are checked like requests, field by field.
        # The contract itself refuses unknown fields; values are checked like a request's.
        bad = create_plan(alice, expect=None, template={"repetitions": 0, "surprise": 1})
        assert error(bad) == (422, "REQUEST_INVALID")
        assert bad.json()["error"]["field_errors"][0]["path"] == "/template/surprise"
        bad = create_plan(alice, expect=None, template={"repetitions": 0})
        assert bad.json()["error"]["field_errors"][0]["path"] == "/template/repetitions"
        alice.delete(f"/api/v1/plans/{plan['id']}", expect=200)
        assert bob.get(f"/api/v1/plans/{plan['id']}").json()["data"]["deletion"]["state"] == (
            "recycled"
        )
        gone = bob.post("/api/v1/config-resolutions", json={"plan_id": plan["id"]})
        assert error(gone) == (409, "PLAN_DELETED")
        submitted = submit(bob, version_id, plan_id=copied["id"])
        assert submitted.status_code == 202, submitted.text
