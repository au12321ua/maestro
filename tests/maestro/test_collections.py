"""Comparisons, collections and analysis snapshots through the API (06 section 8,
E2E-OUT-02 and E2E-OUT-03 with the fake executor)."""

import uuid

from support import create_pat, invite_member, running, setup_admin, wait_operation
from test_configuration import agent_body, error
from test_e2e_run import credential_of, prepare_model
from test_recycle import bin_entry, purge

from maestro.testing.fakes import failed, scored

RATE_LIMITED = failed("provider_transient", exception="ApiRateLimitError")
OUT_OF_MEMORY = failed("sandbox_failed", exception="MemoryLimitExceededError")


def second_model(admin, first_id, model_id="other-model") -> str:
    return admin.post(
        "/api/v1/admin/model-configs",
        json={
            "name": model_id,
            "provider": "demo",
            "protocol": "openai-compatible",
            "endpoint": "https://models.example.test/v1",
            "model_id": model_id,
            "params": {"temperature": 0},
            "credential_id": credential_of(admin, first_id),
        },
        expect=201,
    ).json()["data"]["id"]


def run_of(api, version_id, model_id, harness_id, wait=True, **body) -> str:
    payload = {"name": "run", "dataset_version_id": version_id, **body}
    payload.update(agent_body(model_id, harness_id))
    response = api.post("/api/v1/runs", json=payload, headers={"Idempotency-Key": uuid.uuid4().hex})
    assert response.status_code == 202, response.text
    accepted = response.json()["data"]
    if wait:
        wait_operation(api, accepted["operation_id"])
    return accepted["run_id"]


def compare(api, run_ids, expect=200, **settings):
    response = api.post("/api/v1/comparisons", json={"run_ids": run_ids, **settings})
    assert response.status_code == expect, response.text
    return response.json()["data"] if expect == 200 else response


def rows(table):
    return {r["run_id"]: r for r in table["runs"]}


def test_compare_two_models_and_exclude_external_failures(service, version_id, executor):
    with running(service) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "member")
        model_a, harness = prepare_model(admin)
        model_b = second_model(admin, model_a)
        run_a = run_of(member, version_id, model_a, harness)
        executor.script = {"add-02": [RATE_LIMITED], "mul-01": [OUT_OF_MEMORY]}
        run_b = run_of(member, version_id, model_b, harness)

        table = compare(member, [run_a, run_b])
        assert table["varying"] == ["model"] and table["comparable"]
        assert table["warnings"] == []
        found = rows(table)
        assert found[run_a]["title"] == "demo-model" and found[run_b]["title"] == "other-model"
        # The rate limit is left out and takes add-02 out of both columns; running out of
        # memory may be the agent's doing, so it counts as a failed (0) trial.
        assert table["dropped_tasks"] == 1
        assert found[run_b]["excluded_by_group"] == {"provider_unavailable": 1}
        assert found[run_b]["failed_by_group"] == {"out_of_memory": 1}
        assert found[run_b]["task_count"] == 2 and found[run_b]["mean"] == 0.5
        assert found[run_a]["task_count"] == 2 and found[run_a]["mean"] == 1.0
        assert found[run_b]["difference"]["mean"] == -0.5
        assert found[run_a]["evidence"] in {"available", "none"}
        everything = rows(compare(member, [run_a, run_b], exclude=[]))
        assert everything[run_b]["task_count"] == 3 and everything[run_b]["failed_trials"] == 2
        # The run detail page keeps its own summary: failures are still excluded there.
        summary = member.get(f"/api/v1/runs/{run_b}/summary").json()["data"]
        assert summary["scored_trials"] == 1 and summary["excluded_error_trials"] == 2

        reader = create_pat(member, "read")
        assert compare(reader, [run_a, run_b])["runs"]
        oracle = member.post(
            "/api/v1/runs",
            json={"name": "o", "dataset_version_id": version_id, "execution_mode": "oracle"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        ).json()["data"]
        wait_operation(member, oracle["operation_id"])
        refused = compare(member, [run_a, oracle["run_id"]], expect=422)
        assert error(refused) == (422, "COMPARISON_ORACLE_RUN")


def test_a_collection_keeps_only_matching_runs(service, version_id, executor):
    with running(service) as client:
        admin = setup_admin(service, client)
        alice = invite_member(service, admin, "alice")
        bob = invite_member(service, admin, "bob")
        model_a, harness = prepare_model(admin)
        model_b = second_model(admin, model_a)
        run_a = run_of(alice, version_id, model_a, harness)
        run_b = run_of(alice, version_id, model_b, harness)

        created = alice.post(
            "/api/v1/collections",
            json={
                "name": "models on arith",
                "scenario": "models",
                "base": {"run_id": run_a},
                "run_ids": [run_a, run_b],
            },
            expect=201,
        ).json()["data"]
        collection_id = created["id"]
        assert created["variable_dimensions"] == ["model"]
        assert "model" not in created["fixed"] and "harness" in created["fixed"]
        assert "model_config_id" not in created["template"]
        assert created["template"]["dataset_version_id"] == version_id
        assert created["settings"]["layout"]["mode"] == "leaderboard"
        assert [m["run_id"] for m in created["members"]] == [run_a, run_b]

        # A run with more memory differs in a fixed dimension.
        bigger = run_of(alice, version_id, model_a, harness, resources={"memory_mb": 4096})
        refused = bob.post(
            f"/api/v1/collections/{collection_id}/runs", json={"runs": [{"run_id": bigger}]}
        )
        assert error(refused) == (409, "COLLECTION_MISMATCH")
        assert "resources" in refused.json()["error"]["field_errors"][0]["message"]

        # Submitting from the collection: it fills in the fixed fields, the run joins.
        response = bob.post(
            "/api/v1/runs",
            json={
                "name": "from collection",
                "collection_id": collection_id,
                **agent_body(model_b, harness),
            },
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert response.status_code == 202, response.text
        joined = response.json()["data"]["run_id"]
        wait_operation(bob, response.json()["data"]["operation_id"])
        detail = bob.get(f"/api/v1/collections/{collection_id}").json()["data"]
        assert [m["run_id"] for m in detail["members"]][-1] == joined
        assert detail["members"][-1]["added_by_username"] == "bob"
        # A submission that breaks a fixed value is refused before anything is written.
        mismatch = bob.post(
            "/api/v1/runs",
            json={
                "name": "too big",
                "collection_id": collection_id,
                "resources": {"memory_mb": 4096},
                **agent_body(model_b, harness),
            },
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert error(mismatch) == (409, "COLLECTION_MISMATCH")
        resolved = bob.post(
            "/api/v1/config-resolutions",
            json={
                "collection_id": collection_id,
                "resources": {"memory_mb": 4096},
                **agent_body(model_b, harness),
            },
        ).json()["data"]
        assert [b["code"] for b in resolved["blockers"]] == ["COLLECTION_MISMATCH"]
        good = bob.post(
            "/api/v1/config-resolutions",
            json={"collection_id": collection_id, **agent_body(model_a, harness)},
        ).json()["data"]
        assert good["blockers"] == []
        assert good["field_sources"]["/repetitions"]["layer"] == "collection"
        # A batch from the collection: every member joins.
        batch = bob.post(
            "/api/v1/run-batches",
            json={
                "name": "fill in",
                "runs": [
                    {"name": "a", "collection_id": collection_id, **agent_body(model_a, harness)},
                    {"name": "b", "collection_id": collection_id, **agent_body(model_b, harness)},
                ],
            },
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert batch.status_code == 202, batch.text
        detail = bob.get(f"/api/v1/collections/{collection_id}").json()["data"]
        assert detail["run_count"] == 5

        # A rerun is one more run of its own, not merged with its source.
        rerun = alice.post(
            f"/api/v1/runs/{run_a}/rerun", json={}, headers={"Idempotency-Key": uuid.uuid4().hex}
        ).json()["data"]
        wait_operation(alice, rerun["operation_id"])
        alice.post(
            f"/api/v1/collections/{collection_id}/runs",
            json={"runs": [{"run_id": rerun["run_id"], "label": "again"}]},
            expect=200,
        )
        for operation in batch.json()["data"]["runs"]:
            wait_operation(bob, operation["operation_id"])
        table = alice.post(f"/api/v1/collections/{collection_id}/comparison").json()["data"]
        assert len(table["runs"]) == 6 and table["warnings"] == []
        assert rows(table)[rerun["run_id"]]["label"] == "again"

        # Read tokens look but do not touch.
        reader = create_pat(alice, "read")
        assert reader.get(f"/api/v1/collections/{collection_id}").status_code == 200
        forbidden = reader.delete(f"/api/v1/collections/{collection_id}/runs/{run_b}")
        assert error(forbidden) == (403, "PAT_SCOPE_INSUFFICIENT")
        bob.delete(f"/api/v1/collections/{collection_id}/runs/{run_b}", expect=200)
        events = alice.get(f"/api/v1/collections/{collection_id}/events").json()["data"]
        assert events[0]["action"] == "remove" and events[0]["actor_username"] == "bob"
        # Settings belong to the team; the specification does not change.
        updated = bob.put(
            f"/api/v1/collections/{collection_id}",
            json={"settings": {"scope": "all", "exclude": []}},
            expect=200,
        ).json()["data"]
        assert updated["settings"]["scope"] == "all" and updated["settings"]["exclude"] == []
        bad = bob.put(
            f"/api/v1/collections/{collection_id}",
            json={
                "settings": {"layout": {"mode": "matrix", "rows": "model", "columns": "harness"}}
            },
        )
        assert error(bad) == (422, "SETTINGS_INVALID")

        # Copy with a new specification: fixing the model needs values for it.
        missing = alice.post(
            f"/api/v1/collections/{collection_id}/copy", json={"scenario": "repeats"}
        )
        assert error(missing) == (422, "COLLECTION_BASE_REQUIRED")
        copied = alice.post(
            f"/api/v1/collections/{collection_id}/copy",
            json={"name": "repeats of A", "scenario": "repeats", "base": {"run_id": run_a}},
            expect=201,
        ).json()["data"]
        carried = {m["run_id"] for m in copied["members"]}
        assert run_a in carried and rerun["run_id"] in carried and joined not in carried
        assert {x["run_id"] for x in copied["not_carried"]} >= {joined}
        assert copied["not_carried"][0]["mismatches"][0]["dimension"] == "model"
        assert copied["copied_from"] == collection_id
        # Bob may keep it up, but deleting is the creator's (or an administrator's).
        assert bob.delete(f"/api/v1/collections/{collection_id}").status_code == 403
        alice.delete(f"/api/v1/collections/{collection_id}", expect=200)
        listed = alice.get("/api/v1/collections").json()["data"]
        assert [c["id"] for c in listed] == [copied["id"]]


def test_a_snapshot_stays_while_runs_and_files_go(service, version_id, executor):
    with running(service) as client:
        admin = setup_admin(service, client)
        alice = invite_member(service, admin, "alice")
        bob = invite_member(service, admin, "bob")
        model_a, harness = prepare_model(admin)
        model_b = second_model(admin, model_a)
        run_a = run_of(alice, version_id, model_a, harness)
        executor.script = {"add-01": [scored(0.0)]}
        run_b = run_of(alice, version_id, model_b, harness)
        collection = alice.post(
            "/api/v1/collections",
            json={
                "name": "tb",
                "scenario": "models",
                "base": {"run_id": run_a},
                "run_ids": [run_a, run_b],
            },
            expect=201,
        ).json()["data"]
        agent = create_pat(alice, "experiment")
        snapshot = agent.post(
            "/api/v1/analysis-snapshots",
            json={
                "name": "October",
                "collection_id": collection["id"],
                "note": "A solves add-01, B does not.",
            },
            expect=201,
        ).json()["data"]
        before = rows(snapshot["result"])
        assert before[run_b]["mean"] == 2 / 3
        assert snapshot["spec"]["variable_dimensions"] == ["model"]
        note = snapshot["notes"][0]
        assert note["author_username"] == "alice" and note["pat_id"] is not None

        # The collection moves on; the snapshot does not.
        executor.script = {}
        run_c = run_of(alice, version_id, model_b, harness, wait=True)
        alice.post(
            f"/api/v1/collections/{collection['id']}/runs",
            json={"runs": [{"run_id": run_c}]},
            expect=200,
        )
        alice.delete(f"/api/v1/runs/{run_b}", expect=200)
        table = alice.post(f"/api/v1/collections/{collection['id']}/comparison").json()["data"]
        assert [x["run_id"] for x in table["set_aside"]] == [run_b]
        assert len(table["runs"]) == 2
        again = alice.get(f"/api/v1/analysis-snapshots/{snapshot['id']}").json()["data"]
        assert rows(again["result"]) == before
        assert again["evidence"][run_b] == "deleted"
        purge(admin, bin_entry(admin, run_b), run_b)
        again = alice.get(f"/api/v1/analysis-snapshots/{snapshot['id']}").json()["data"]
        assert rows(again["result"]) == before and again["evidence"][run_b] == "purged"
        events = alice.get(f"/api/v1/collections/{collection['id']}/events").json()["data"]
        assert events[0]["action"] == "purged" and events[0]["run_id"] == run_b

        # Notes are their authors'.
        bob_note = bob.post(
            f"/api/v1/analysis-snapshots/{snapshot['id']}/notes",
            json={"body": "Agreed."},
            expect=201,
        ).json()["data"]["notes"][-1]
        refused = alice.put(
            f"/api/v1/analysis-snapshots/{snapshot['id']}/notes/{bob_note['id']}",
            json={"body": "No."},
        )
        assert error(refused) == (403, "FORBIDDEN")
        listed = bob.get(f"/api/v1/analysis-snapshots?collection_id={collection['id']}")
        assert [s["id"] for s in listed.json()["data"]] == [snapshot["id"]]

        # Only ended runs are frozen.
        executor.script = {"add-01": ["hang"]}
        running_run = run_of(alice, version_id, model_a, harness, wait=False)
        busy = alice.post(
            "/api/v1/analysis-snapshots", json={"name": "too early", "run_ids": [running_run]}
        )
        assert error(busy) == (409, "SNAPSHOT_RUNS_ACTIVE")
        alice.post(f"/api/v1/runs/{running_run}/cancel", expect=200)


def test_an_agent_keeps_a_collection_through_the_sdk(service, version_id, executor):
    from maestro_sdk import MaestroClient

    with running(service) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "member")
        model_a, harness = prepare_model(admin)
        model_b = second_model(admin, model_a)
        token = create_pat(member, "experiment")
        agent = MaestroClient("http://testserver", token.token, http=token.client)
        base = run_of(member, version_id, model_a, harness)
        collection = agent.collections.create(
            "models", "models", base_run_id=base, run_ids=[base]
        )
        executor.script = {"add-02": [RATE_LIMITED]}
        submission = agent.runs.submit(
            agent_body(model_b, harness), name="b", collection_id=collection.id
        )
        wait_operation(token, submission.accepted.operation_id)
        table = agent.collections.compare(collection.id)
        assert [r.run_id for r in table.runs] == [base, submission.accepted.run_id]
        assert table.dropped_tasks == 1
        everything = agent.collections.compare(collection.id, exclude=[])
        assert everything.dropped_tasks == 0
        snapshot = agent.snapshots.create("first look", collection_id=collection.id)
        snapshot = agent.snapshots.add_note(snapshot.id, "B is rate limited on add-02.")
        assert snapshot.notes[0].pat_id is not None
        assert agent.comparisons.compare([base, submission.accepted.run_id]).comparable
        assert [e.action for e in agent.collections.events(collection.id)][:1] == ["add"]
