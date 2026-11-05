"""Full experiment flow with the fake executor: an agent uses only a PAT and the HTTP API.

The administrator prepares a model configuration bound to a team credential and a
harness version beforehand (as the scenario's precondition says). The agent then reads
capabilities, the catalog, models and harnesses, resolves the configuration, checks the
blockers, submits the resolution idempotently, polls the Operation, reads per-trial
scores and attempts, and downloads an artifact with a digest check. The generated SDK
 is not yet part of the flow.
"""

import hashlib
import uuid

from support import create_pat, running, setup_admin, wait_operation
from support.configuration import credential_of, prepare_model
from support.datasets import import_version

from maestro.testing.fakes import scored


def test_agent_runs_a_complete_experiment_with_only_a_pat(service, version_id, executor):
    executor.script = {"add-02": [scored(0.0)]}
    with running(service) as client:
        admin = setup_admin(service, client)
        agent = create_pat(admin, ("experiment",), run_limit=3)

        caps = agent.get("/api/v1/capabilities", expect=200).json()["data"]
        assert caps["features"]["execution"] is True
        assert caps["actions"]["runs.submit"]["pat_permission"] == "experiment"

        datasets = agent.get("/api/v1/datasets", expect=200).json()["data"]
        version = datasets[0]["versions"][0]
        tasks = agent.get(f"/api/v1/dataset-versions/{version['id']}/tasks").json()["data"]
        assert all(task["can_submit"] for task in tasks)

        model_id, harness_id = prepare_model(admin)
        models = agent.get("/api/v1/model-configs", expect=200).json()["data"]
        assert [m["id"] for m in models] == [model_id]
        assert models[0]["credential_bound"] and models[0]["credential_id"] is None
        harnesses = agent.get("/api/v1/harness-versions", expect=200).json()["data"]
        assert [h["id"] for h in harnesses] == [harness_id]

        resolution = agent.post(
            "/api/v1/config-resolutions",
            json={
                "dataset_version_id": version["id"],
                "execution_mode": "agent",
                "model_config_id": model_id,
                "harness_version_id": harness_id,
                "repetitions": 2,
            },
            expect=201,
        ).json()["data"]
        assert resolution["blockers"] == [] and resolution["planned_trials"] == 6
        effective = resolution["effective_config"]
        assert effective["model"]["model_id"] == "demo-model"
        assert effective["harness"]["name"] == "fake-harness"
        assert resolution["field_sources"]["/repetitions"] == {"layer": "override"}
        assert resolution["field_sources"]["/concurrency"]["layer"] == "team"

        key = uuid.uuid4().hex
        body = {
            "name": "baseline",
            "resolution_id": resolution["resolution_id"],
            "client_request_id": "agent-round-1",
        }
        accepted = agent.post(
            "/api/v1/runs", json=body, headers={"Idempotency-Key": key}, expect=202
        ).json()["data"]
        # The response is "lost" and the agent retries with the same key.
        retried = agent.post(
            "/api/v1/runs", json=body, headers={"Idempotency-Key": key}, expect=202
        ).json()["data"]
        assert retried == accepted

        operation = wait_operation(agent, accepted["operation_id"])
        assert operation["status"] == "succeeded"
        assert operation["progress"]["planned"] == 6 == operation["progress"]["terminal"]

        run = agent.get(f"/api/v1/runs/{accepted['run_id']}").json()["data"]
        assert run["state"] == "finished" and run["result_completeness"] == "complete"
        assert run["artifact_state"] == "complete" and run["cleanup_state"] == "complete"
        assert run["snapshot"]["config_digest"] == accepted["config_digest"]
        assert accepted["config_digest"] == resolution["config_digest"]
        provenance = run["snapshot"]["provenance"]
        assert provenance["resolution_id"] == resolution["resolution_id"]
        assert provenance["credential_ids"] == []  # a PAT never sees the binding
        bound = admin.get(f"/api/v1/runs/{accepted['run_id']}").json()["data"]["snapshot"]
        assert bound["provenance"]["credential_ids"] == [credential_of(admin, model_id)]
        assert run["pat_id"] is not None

        trials = agent.get(f"/api/v1/runs/{accepted['run_id']}/trials").json()["data"]
        assert len(trials) == 6
        scores = {(x["task_key"], x["repetition_index"]): x["result"]["score"] for x in trials}
        assert scores[("add-02", 1)] == 0.0 and scores[("add-01", 1)] == 1.0
        assert all(len(x["attempts"]) == 1 for x in trials)

        artifacts = agent.get(f"/api/v1/runs/{accepted['run_id']}/artifacts").json()["data"]
        output = next(a for a in artifacts if a["kind"] == "output")
        download = agent.get(f"/api/v1/artifacts/{output['id']}/download")
        assert download.status_code == 200
        digest = "sha256:" + hashlib.sha256(download.content).hexdigest()
        assert digest == output["digest"] == download.headers["X-Content-Digest"]

        # Discovery by client_request_id after losing all local state.
        found = agent.get("/api/v1/runs?client_request_id=agent-round-1").json()["data"]
        assert [x["id"] for x in found] == [accepted["run_id"]]
        pats = admin.get("/api/v1/me/pats").json()["data"]
        assert pats[0]["accepted_runs"] == 1


def test_service_without_executor_refuses_runs_honestly(settings, source, environment):
    from maestro.application import Maestro
    from maestro.db import migrate

    service = Maestro.build(settings, environment=environment, sources={"fake": source})
    migrate.upgrade(service.db)
    service.keys.initialize()
    version = import_version(service)["dataset_version_id"]
    with running(service) as client:
        admin = setup_admin(service, client)
        assert client.get("/health/ready").json()["accepting_runs"] is False
        response = admin.post(
            "/api/v1/runs",
            json={"name": "x", "dataset_version_id": version, "execution_mode": "oracle"},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "EXECUTION_UNAVAILABLE"
    service.db.dispose()


def test_readiness_and_openapi(service):
    with running(service) as client:
        assert client.get("/health/live").json() == {"status": "alive", "accepting_runs": None}
        ready = client.get("/health/ready").json()
        assert ready["status"] == "ready" and ready["accepting_runs"] is True
        spec = client.get("/api/v1/openapi.json").json()
        assert "/api/v1/runs" in spec["paths"]
        assert "Idempotency-Key" in str(spec["paths"]["/api/v1/runs"]["post"]["parameters"])
