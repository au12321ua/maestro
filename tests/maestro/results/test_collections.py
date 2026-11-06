"""Comparisons, collections and analysis snapshots through the API (and  with the fake executor)."""

import uuid

import pytest
from sqlalchemy import select, update
from support import create_pat, invite_member, running, setup_admin, wait_operation
from support.configuration import agent_body, credential_of, error, prepare_model
from support.datasets import import_and_prepare
from support.results import bin_entry, purge

from maestro.db import schema as t
from maestro.execution.policy import harbor_outcome
from maestro.testing.fakes import FakeDatasetSource, failed, scored

RATE_LIMITED = harbor_outcome("ApiRateLimitError", "rate limited", None, False)
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

        reader = create_pat(member, ())
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
        # The picker offers runs not yet in, saying which ones would be refused and why.
        again = run_of(alice, version_id, model_a, harness)
        offered = {
            x["run_id"]: x
            for x in bob.get(f"/api/v1/collections/{collection_id}/candidates", expect=200).json()[
                "data"
            ]
        }
        assert set(offered) == {bigger, again}
        assert offered[again]["mismatches"] == [] and offered[again]["model"] == "demo"
        assert {m["dimension"] for m in offered[bigger]["mismatches"]} == {"resources"}

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
        reader = create_pat(alice, ())
        assert reader.get(f"/api/v1/collections/{collection_id}").status_code == 200
        forbidden = reader.delete(f"/api/v1/collections/{collection_id}/runs/{run_b}")
        assert error(forbidden) == (403, "PAT_PERMISSION_INSUFFICIENT")
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
        agent = create_pat(alice, ("experiment",))
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
        conditions = snapshot["result"]["dimensions"]
        model_values = next(d["values"] for d in conditions if d["key"] == "model")
        assert model_values[run_b]["model"]["endpoint"] == "https://models.example.test/v1"
        assert model_values[run_b]["model"]["params"] == {"temperature": 0}
        assert "credential_id" not in str(conditions)
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
        assert again["result"]["dimensions"] == conditions

        # Older frozen results remain readable, with no invented historical conditions.
        with service.db.write() as conn:
            stored = conn.execute(
                select(t.analysis_snapshots.c.result_json).where(
                    t.analysis_snapshots.c.id == snapshot["id"]
                )
            ).scalar_one()
            stored["comparison_version"] = 1
            for dimension in stored["dimensions"]:
                dimension.pop("values", None)
            conn.execute(
                update(t.analysis_snapshots)
                .where(t.analysis_snapshots.c.id == snapshot["id"])
                .values(result_json=stored)
            )
        old = alice.get(f"/api/v1/analysis-snapshots/{snapshot['id']}", expect=200).json()["data"]
        assert rows(old["result"]) == before
        assert all(d["values"] == {} for d in old["result"]["dimensions"])
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
        token = create_pat(member, ("experiment",))
        agent = MaestroClient("http://testserver", token.token, http=token.client)
        base = run_of(member, version_id, model_a, harness)
        collection = agent.collections.create("models", "models", base_run_id=base, run_ids=[base])
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


def test_text_author_parameters_work_in_templates_and_run_requests(service, source):
    source.add(
        "demo",
        "params",
        "v1",
        ["region"],
        files={
            "region": {
                **FakeDatasetSource.task_files("demo", "params", "region"),
                "task.toml": '[environment]\ncpus = 1\nmemory_mb = 2048\n[verifier.env]\nREGION = "${REGION}"\nGRADER_TOKEN = "${GRADER_TOKEN}"\n',
            }
        },
    )
    with running(service) as client:
        admin = setup_admin(service, client)
        version, _ = import_and_prepare(admin, name="params")
        model, harness = prepare_model(admin)
        credential = admin.post(
            "/api/v1/admin/credentials",
            json={
                "name": "grader",
                "kind": "generic_secret",
                "provider": "grader",
                "value": "fake-grader-value",
            },
            expect=201,
        ).json()["data"]
        parameters = {"REGION": "eu", "GRADER_TOKEN": {"credential_id": credential["id"]}}
        base = run_of(admin, version, model, harness, parameters=parameters)
        request = admin.get(f"/api/v1/runs/{base}/request", expect=200).json()["data"]
        assert request["parameters"] == parameters
        c = admin.post(
            "/api/v1/collections",
            json={
                "name": "region",
                "scenario": "models",
                "base": {"run_id": base},
            },
            expect=201,
        ).json()["data"]
        assert c["template"]["parameters"] == parameters
        assert admin.get("/api/v1/collections", expect=200).json()["data"][0]["id"] == c["id"]
        from_request = admin.post(
            "/api/v1/collections",
            json={
                "name": "from request",
                "scenario": "models",
                "base": {"request": request},
            },
            expect=201,
        ).json()["data"]
        assert from_request["template"]["parameters"] == parameters
        # A row created before this fix has the internal {value: ...} wrapper.
        with service.db.write() as conn:
            conn.execute(
                update(t.collections)
                .where(t.collections.c.id == c["id"])
                .values(
                    template_json={
                        **c["template"],
                        "parameters": {**parameters, "REGION": {"value": "eu"}},
                    }
                )
            )
        legacy = admin.get(f"/api/v1/collections/{c['id']}", expect=200).json()["data"]
        assert legacy["template"]["parameters"] == parameters
        admin.get("/api/v1/collections", expect=200)
        submitted = admin.post(
            "/api/v1/runs",
            json={
                "name": "from legacy collection",
                "collection_id": c["id"],
                **agent_body(model, harness),
            },
            headers={"Idempotency-Key": uuid.uuid4().hex},
            expect=202,
        ).json()["data"]
        wait_operation(admin, submitted["operation_id"])
        used = admin.get(f"/api/v1/runs/{submitted['run_id']}/request", expect=200).json()["data"]
        assert used["parameters"] == parameters
        assert "fake-grader-value" not in str([request, c, from_request, legacy, used])


@pytest.mark.parametrize(
    ("before", "after"), [({}, {"temperature": None}), ({"flag": False}, {"flag": 0})]
)
def test_collection_refuses_model_params_that_only_python_considers_equal(
    service, version_id, before, after
):
    with running(service) as client:
        admin = setup_admin(service, client)
        model, harness = prepare_model(admin)
        m = admin.get(f"/api/v1/model-configs/{model}", expect=200).json()["data"]
        m = admin.put(
            f"/api/v1/admin/model-configs/{model}",
            json={
                **{k: m[k] for k in ("provider", "protocol", "endpoint", "model_id")},
                "expected_revision": m["revision"],
                "params": before,
            },
            expect=200,
        ).json()["data"]
        base = run_of(admin, version_id, model, harness)
        c = admin.post(
            "/api/v1/collections",
            json={
                "name": "fixed",
                "scenario": "repeats",
                "base": {"run_id": base},
            },
            expect=201,
        ).json()["data"]
        resolution = admin.post(
            "/api/v1/config-resolutions",
            json={"dataset_version_id": version_id, **agent_body(model, harness)},
            expect=201,
        ).json()["data"]
        admin.put(
            f"/api/v1/admin/model-configs/{model}",
            json={
                **{k: m[k] for k in ("provider", "protocol", "endpoint", "model_id")},
                "expected_revision": m["revision"],
                "params": after,
            },
            expect=200,
        )
        changed = run_of(admin, version_id, model, harness, source_run_id=base)
        refused = admin.post(
            f"/api/v1/collections/{c['id']}/runs", json={"runs": [{"run_id": changed}]}
        )
        assert error(refused) == (409, "COLLECTION_MISMATCH")
        assert "model" in refused.json()["error"]["field_errors"][0]["message"]
        parameter_path = f"/model/params/{next(iter(after))}"
        detail = admin.get(f"/api/v1/runs/{changed}", expect=200).json()["data"]
        changes = detail["snapshot"]["provenance"]["source"]["differences"]
        assert parameter_path in {d["path"] for d in changes}
        stale = admin.post(
            "/api/v1/runs",
            json={"name": "stale", "resolution_id": resolution["resolution_id"]},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert error(stale) == (409, "CONFIG_RESOLUTION_STALE")
        assert parameter_path in {d["path"] for d in stale.json()["error"]["field_errors"]}


@pytest.mark.parametrize("batch", [False, True])
def test_confirmed_resolution_keeps_collection_provenance(service, version_id, batch):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        model, harness = prepare_model(admin)
        c = admin.post(
            "/api/v1/collections",
            json={
                "name": "resources",
                "scenario": "models",
                "base": {
                    "request": {
                        "dataset_version_id": version_id,
                        **agent_body(model, harness),
                        "resources": {"cpus": 2},
                        "repetitions": 2,
                    }
                },
            },
            expect=201,
        ).json()["data"]
        res = admin.post(
            "/api/v1/config-resolutions",
            json={
                "collection_id": c["id"],
                **agent_body(model, harness),
            },
            expect=201,
        ).json()["data"]
        expected = {"layer": "collection", "source_id": c["id"]}
        assert res["field_sources"]["/resources/cpus"] == expected
        member = {
            "name": "confirmed",
            "collection_id": c["id"],
            "resolution_id": res["resolution_id"],
        }
        accepted = admin.post(
            "/api/v1/run-batches" if batch else "/api/v1/runs",
            json={"name": "batch", "runs": [member]} if batch else member,
            headers={"Idempotency-Key": uuid.uuid4().hex},
            expect=202,
        ).json()["data"]
        run_id = accepted["runs"][0]["run_id"] if batch else accepted["run_id"]
        detail = admin.get(f"/api/v1/runs/{run_id}", expect=200).json()["data"]
        provenance = detail["snapshot"]["provenance"]
        assert provenance["field_sources"]["/resources/cpus"] == expected
        assert provenance["field_sources"]["/repetitions"] == expected
        assert provenance["reference_revisions"]["collection"] == {"id": c["id"]}
        assert admin.get(f"/api/v1/collections/{c['id']}").json()["data"]["run_count"] == 1


def test_current_best_requires_a_live_member_run_and_records_every_reason(service, version_id):
    from support.attachments import attach

    with running(service) as client:
        admin = setup_admin(service, client)
        alice = invite_member(service, admin, "alice")
        model, harness = prepare_model(admin)
        attachment = attach(alice)
        unused = attach(alice, name="unused")
        run_id = run_of(alice, version_id, model, harness, attachments=[attachment])
        collection = alice.post(
            "/api/v1/collections",
            json={
                "name": "candidates",
                "scenario": "attachments",
                "base": {"run_id": run_id},
                "run_ids": [run_id],
            },
            expect=201,
        ).json()["data"]
        path = f"/api/v1/collections/{collection['id']}"
        assert collection["current_best"] is None
        reader = create_pat(alice, ())
        assert (
            reader.put(
                path + "/current-best", json={"snapshot_id": attachment, "reason": "best"}
            ).status_code
            == 403
        )
        baseline = reader.post(path + "/comparison").json()["data"]["settings"]["baseline_run_id"]
        writer = create_pat(alice)
        for body in (
            {"snapshot_id": attachment, "reason": " "},
            {"snapshot_id": None, "reason": ""},
            {"snapshot_id": attachment, "reason": "x" * 2001},
            {"snapshot_id": unused, "reason": "not used"},
            {"snapshot_id": "missing", "reason": "unknown"},
        ):
            assert writer.put(path + "/current-best", json=body).status_code == 422
        assert writer.put(path + "/current-best", json={"snapshot_id": None}).status_code == 422
        set_best = writer.put(
            path + "/current-best",
            json={"snapshot_id": attachment, "reason": "  strongest evidence  "},
            expect=200,
        ).json()["data"]
        assert set_best["current_best"] == {
            "snapshot_id": attachment,
            "reason": "strongest evidence",
            "actor_id": collection["owner_id"],
            "actor_username": "alice",
            "pat_id": writer.get("/api/v1/me").json()["data"]["pat_id"],
            "at": set_best["current_best"]["at"],
        }
        assert reader.get(path).json()["data"]["current_best"] == set_best["current_best"]
        assert (
            reader.post(path + "/comparison").json()["data"]["settings"]["baseline_run_id"]
            == baseline
        )
        # Recycling the only supporting run blocks a new selection but keeps the fact.
        entry = alice.delete(f"/api/v1/runs/{run_id}", expect=200).json()["data"]
        assert (
            writer.put(
                path + "/current-best", json={"snapshot_id": attachment, "reason": "deleted run"}
            ).status_code
            == 422
        )
        assert reader.get(path).json()["data"]["current_best"] == set_best["current_best"]
        admin.post(f"/api/v1/admin/recycle-bin/{entry['id']}/restore", expect=200)
        bob = invite_member(service, admin, "bob")
        bob.put(
            path + "/current-best",
            json={"snapshot_id": attachment, "reason": "confirmed"},
            expect=200,
        )
        recycled = alice.delete(f"/api/v1/attachments/{attachment}", expect=200).json()["data"]
        assert (
            writer.put(
                path + "/current-best",
                json={"snapshot_id": attachment, "reason": "deleted snapshot"},
            ).status_code
            == 422
        )
        admin.post(f"/api/v1/admin/recycle-bin/{recycled['id']}/restore", expect=200)
        cleared = bob.put(
            path + "/current-best",
            json={"snapshot_id": None, "reason": "more evidence needed"},
            expect=200,
        ).json()["data"]
        assert cleared["current_best"]["snapshot_id"] is None
        assert cleared["current_best"]["reason"] == "more evidence needed"
        changes = [
            e for e in reader.get(path + "/events").json()["data"] if e["action"] == "current_best"
        ]
        assert len(changes) == 3
        assert changes[-1]["actor_username"] == "alice" and changes[-1]["pat_id"]
        assert changes[0]["actor_username"] == "bob" and changes[0]["pat_id"] is None
        assert changes[0]["detail"] == {
            "previous_snapshot_id": attachment,
            "snapshot_id": None,
            "reason": "more evidence needed",
        }
        copy = bob.post(path + "/copy", json={"name": "fresh decision"}, expect=201).json()["data"]
        assert copy["current_best"] is None
        recycled = alice.delete(path, expect=200).json()["data"]
        assert (
            writer.put(
                path + "/current-best", json={"snapshot_id": None, "reason": "blocked"}
            ).status_code
            == 409
        )
        admin.post(f"/api/v1/admin/recycle-bin/{recycled['id']}/restore", expect=200)
        writer.delete(path + f"/runs/{run_id}", expect=200)
        assert (
            writer.put(
                path + "/current-best", json={"snapshot_id": attachment, "reason": "removed member"}
            ).status_code
            == 422
        )


def test_held_out_spec_validation_copy_and_immutability(service, version_id, source):
    with running(service) as client:
        admin = setup_admin(service, client)
        model, harness = prepare_model(admin)
        tasks = admin.get(f"/api/v1/dataset-versions/{version_id}/tasks").json()["data"]
        ids = [task["id"] for task in tasks]
        base = {"request": {"dataset_version_id": version_id, **agent_body(model, harness)}}
        body = {"name": "held out", "scenario": "attachments", "base": base}
        for marks in (["unknown"], ids):
            refused = admin.post("/api/v1/collections", json={**body, "held_out_task_ids": marks})
            assert refused.status_code == 422
            assert refused.json()["error"]["field_errors"][0]["path"] == "/held_out_task_ids"
        collection = admin.post(
            "/api/v1/collections", json={**body, "held_out_task_ids": ids[:1]}, expect=201
        ).json()["data"]
        path = f"/api/v1/collections/{collection['id']}"
        assert collection["held_out_task_ids"] == ids[:1]
        assert collection["held_out_task_keys"] == [tasks[0]["task_key"]]
        assert admin.put(path, json={"held_out_task_ids": []}).status_code == 422
        assert admin.get(path).json()["data"]["held_out_task_ids"] == ids[:1]
        assert (
            admin.post(path + "/copy", json={}, expect=201).json()["data"]["held_out_task_ids"]
            == ids[:1]
        )
        assert (
            admin.post(path + "/copy", json={"held_out_task_ids": ids[1:2]}, expect=201).json()[
                "data"
            ]["held_out_task_ids"]
            == ids[1:2]
        )
        source.add("demo", "other", "v1", ["other-01", "other-02"])
        other, _ = import_and_prepare(admin, "demo", "other", "v1")
        other_base = {"request": {**base["request"], "dataset_version_id": other}}
        refused = admin.post(path + "/copy", json={"base": other_base})
        assert refused.status_code == 422
        assert "new marks" in refused.json()["error"]["message"]
        assert (
            admin.post(
                "/api/v1/collections",
                json={**body, "base": other_base, "held_out_task_ids": ids[:1]},
            ).status_code
            == 422
        )
        copied = admin.post(
            path + "/copy", json={"base": other_base, "held_out_task_ids": []}, expect=201
        ).json()["data"]
        assert (
            copied["held_out_task_ids"] == [] and copied["template"]["dataset_version_id"] == other
        )


def test_held_out_comparison_and_snapshots_split_without_recording_reads(
    service, version_id, executor
):
    with running(service) as client:
        admin = setup_admin(service, client)
        model, harness = prepare_model(admin)
        tasks = admin.get(f"/api/v1/dataset-versions/{version_id}/tasks").json()["data"]
        held = next(task for task in tasks if task["task_key"] == "add-02")
        first = run_of(admin, version_id, model, harness)
        collection = admin.post(
            "/api/v1/collections",
            json={
                "name": "split",
                "scenario": "models",
                "base": {"run_id": first},
                "run_ids": [first],
                "held_out_task_ids": [held["id"]],
            },
            expect=201,
        ).json()["data"]
        path = f"/api/v1/collections/{collection['id']}"
        executor.script = {"add-01": [scored(0)], "add-02": [scored(0)]}
        second = run_of(admin, version_id, model, harness, collection_id=collection["id"])
        result = admin.post(path + "/comparison", expect=200).json()["data"]
        assert result["comparison_version"] == 3
        assert {task["task_key"] for task in result["tasks"]} == {"add-01", "mul-01"}
        assert result["held_out"]["tasks"][0]["task_key"] == "add-02"
        assert result["held_out"]["tasks"][0]["held_out"]
        assert rows(result)[second]["mean"] == 0.5
        assert rows(result)[second]["difference"]["mean"] == -0.5
        assert rows(result["held_out"])[second]["mean"] == 0
        assert rows(result["held_out"])[second]["difference"]["mean"] == -1
        ad_hoc = compare(admin, [first, second])
        assert ad_hoc["held_out"] is None and len(ad_hoc["tasks"]) == 3
        # An excluded failure in the held-out group drops only that group's task.
        executor.script = {"add-02": [RATE_LIMITED]}
        third = run_of(admin, version_id, model, harness, collection_id=collection["id"])
        filtered = admin.post(path + "/comparison", expect=200).json()["data"]
        assert filtered["dropped_tasks"] == 0 and filtered["held_out"]["dropped_tasks"] == 1
        assert rows(filtered["held_out"])[third]["excluded_by_group"] == {"provider_unavailable": 1}
        all_tasks = admin.post(
            path + "/comparison", json={"scope": "all", "exclude": []}, expect=200
        ).json()["data"]
        assert rows(all_tasks["held_out"])[third]["failed_trials"] == 1
        snapshot = admin.post(
            "/api/v1/analysis-snapshots",
            json={"name": "frozen", "collection_id": collection["id"]},
            expect=201,
        ).json()["data"]
        assert snapshot["result"] == filtered and snapshot["spec"]["held_out_task_ids"] == [
            held["id"]
        ]
        with service.db.read() as conn:
            stored = (
                conn.execute(
                    select(t.analysis_snapshots).where(t.analysis_snapshots.c.id == snapshot["id"])
                )
                .mappings()
                .one()
            )
            assert stored["result_json"]["held_out"]["runs"]
            assert conn.execute(select(t.task_reads)).all() == []
        # Frozen older results remain readable without adding or recomputing a split.
        legacy = {k: v for k, v in stored["result_json"].items() if k != "held_out"}
        legacy["comparison_version"] = 2
        with service.db.write() as conn:
            conn.execute(
                update(t.analysis_snapshots)
                .where(t.analysis_snapshots.c.id == snapshot["id"])
                .values(
                    result_json=legacy,
                    spec_json={
                        k: v for k, v in stored["spec_json"].items() if not k.startswith("held_out")
                    },
                )
            )
        old = admin.get(f"/api/v1/analysis-snapshots/{snapshot['id']}", expect=200).json()["data"]
        assert old["result"]["comparison_version"] == 2 and old["result"]["held_out"] is None
        assert old["result"]["runs"] == filtered["runs"]


def test_held_out_view_counts_team_runs_once_and_keeps_read_history(service, version_id):
    from support.results import finished_run

    with running(service) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "member")
        model, harness = prepare_model(admin)
        tasks = admin.get(f"/api/v1/dataset-versions/{version_id}/tasks").json()["data"]
        task = tasks[0]
        admin.get(f"/api/v1/tasks/{task['id']}", expect=200)
        before = run_of(admin, version_id, model, harness)
        collection = admin.post(
            "/api/v1/collections",
            json={
                "name": "reads and runs",
                "scenario": "models",
                "base": {"run_id": before},
                "held_out_task_ids": [task["id"]],
            },
            expect=201,
        ).json()["data"]
        path = f"/api/v1/collections/{collection['id']}/held-out"
        (view,) = member.get(path, expect=200).json()["data"]
        assert not view["read_since_creation"] and view["accepted_runs"] == 0
        assert view["reads"][0]["count"] == 1
        # Runs outside the collection and Oracle runs count; repetitions do not multiply it.
        run_id = finished_run(
            member,
            version_id,
            repetitions=2,
            task_selection={"mode": "explicit", "task_ids": [task["id"]]},
        )
        other = finished_run(
            member, version_id, task_selection={"mode": "explicit", "task_ids": [tasks[1]["id"]]}
        )
        reader = create_pat(member, ())
        reader.get(f"/api/v1/tasks/{task['id']}", expect=200)
        (view,) = reader.get(path, expect=200).json()["data"]
        assert view["read_since_creation"] and view["accepted_runs"] == 1
        assert len(view["reads"]) == 2
        member.delete(f"/api/v1/runs/{run_id}", expect=200)
        assert member.get(path).json()["data"][0]["accepted_runs"] == 1
        purge(admin, bin_entry(admin, run_id)).raise_for_status()
        assert member.get(path).json()["data"][0]["accepted_runs"] == 1
        assert admin.get(f"/api/v1/runs/{other}").status_code == 200
        # Purging task definitions does not discard read summaries.
        admin.delete(f"/api/v1/admin/dataset-versions/{version_id}", expect=200)
        purge(admin, bin_entry(admin, version_id)).raise_for_status()
        assert member.get(path).json()["data"][0]["reads"] == view["reads"]
