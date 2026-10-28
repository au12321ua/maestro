"""The Python SDK (M7): the three stories walked by an agent through the SDK only.

Each story test uses nothing but ``maestro_sdk`` with a PAT (the administrator's part of
the precondition uses the web session, as in the scenarios). The SDK talks to the real
application through the TestClient transport. Further tests: retries keep the
idempotency key, every public operation has an SDK method, the SDK imports without the
service, and the generated files match the API.
"""

import json
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import httpx
import pytest
from maestro_sdk import (
    ApiError,
    BlockersPresent,
    MaestroClient,
    TransportError,
    UnsupportedCapability,
    WaitTimeout,
    models,
)
from support import create_pat, invite_member, running, setup_admin
from test_datasets import evict, import_and_prepare, tasks_by_key
from test_e2e_run import prepare_model

from maestro.boundaries.executor import AttemptOutcome
from maestro.core import clock
from maestro.testing.fakes import failed, scored

ROOT = Path(__file__).resolve().parents[2]


def sdk(api, http=None) -> MaestroClient:
    """An SDK client on the test application, authenticated by the caller's PAT."""
    return MaestroClient("http://testserver", api.token, http=http or api.client)


# --- story one: datasets (E2E-DS-03) ----------------------------------------------------


def test_story_one_agent_reads_the_catalog_and_runs_on_a_cold_cache(service, source):
    source.add("demo", "solo", "v1", ["solo"])
    with running(service) as client:
        admin = setup_admin(service, client)
        version_id, _ = import_and_prepare(admin, name="solo")
        (task,) = tasks_by_key(admin, version_id).values()
        evict(admin, task)
        member = invite_member(service, admin, "member")
        reader = sdk(create_pat(member, scope="read"))
        agent = sdk(create_pat(member, scope="experiment", run_limit=5))

        for pat in (reader, agent):
            (dataset,) = pat.catalog.datasets()
            assert dataset.versions[0].id == version_id
            detail = pat.catalog.version(version_id)
            assert detail.admitted_task_count == 1 and detail.cache_ready_task_count == 0
            (listed,) = pat.catalog.tasks(version_id)
            assert listed.admission.state == "admitted" and listed.can_submit
            assert listed.local_preparation.cache_state == "stale"
            assert [s.kind for s in pat.catalog.sources()] == ["fake"]
            # Searching the external source and administering are not SDK business (U-20).
            with pytest.raises(ApiError) as denied:
                pat._call("GET", "dataset-search", params={"q": "solo", "source_kind": "fake"})
            assert denied.value.status == 403
            with pytest.raises(ApiError) as denied:
                pat._call("POST", f"admin/dataset-versions/{version_id}/preparations", json={})
            assert denied.value.code == "ADMIN_SESSION_REQUIRED"

        config = {"dataset_version_id": version_id, "execution_mode": "oracle"}
        with pytest.raises(ApiError) as refused:
            reader.runs.submit(config, name="read-only")
        assert refused.value.code == "PAT_SCOPE_INSUFFICIENT"

        submitted = agent.runs.submit(config, name="cold cache")
        assert agent.operations.wait(submitted.operation_id, interval=0.02).status == "succeeded"
        assert agent.runs.get(submitted.run_id).counts.scored == 1  # restored, no admin step
        preparation = reader.catalog.preparation(listed.local_preparation.id)
        assert preparation.cache_state == "ready" and preparation.pins == []


# --- story two: runs (E2E-RUN-01) -------------------------------------------------------


def test_story_two_agent_runs_a_complete_experiment(service, version_id, executor, tmp_path):
    executor.script = {"add-02": [scored(0.0)]}
    with running(service) as client:
        admin = setup_admin(service, client)
        model_id, harness_id = prepare_model(admin)
        agent = sdk(create_pat(admin, "experiment", run_limit=3))

        caps = agent.require("runs", "execution", "configuration_resolution")
        assert caps.actions["runs.submit"].pat_scopes == ["experiment"]
        with pytest.raises(UnsupportedCapability):
            agent.require("collections")  # P1, switched off

        assert [d.id for d in agent.catalog.datasets()]
        assert all(task.can_submit for task in agent.catalog.tasks(version_id))
        (model,) = agent.configs.models()
        assert model.id == model_id and model.credential_bound and model.credential_id is None
        assert [h.id for h in agent.configs.harnesses()] == [harness_id]

        config = models.ConfigRequestInput(
            dataset_version_id=version_id,
            execution_mode="agent",
            model_config_id=model_id,
            harness_version_id=harness_id,
            repetitions=2,
        )
        blocked = agent.configs.resolve(config.model_copy(update={"repetitions": 100_000}))
        with pytest.raises(BlockersPresent):
            blocked.raise_for_blockers()
        resolution = agent.configs.resolve(config).raise_for_blockers()
        assert resolution.planned_trials == 6
        assert resolution.field_sources["/repetitions"].layer == "override"
        assert agent.configs.resolution(resolution.resolution_id).config_digest == (
            resolution.config_digest
        )

        submitted = agent.runs.submit(
            name="baseline",
            resolution_id=resolution.resolution_id,
            client_request_id="agent-round-1",
        )
        assert not submitted.replayed
        again = agent.runs.submit(
            name="baseline",
            resolution_id=resolution.resolution_id,
            client_request_id="agent-round-1",
            idempotency_key=submitted.idempotency_key,
        )
        assert again.replayed and again.accepted == submitted.accepted

        operation = agent.operations.wait(submitted.operation_id, interval=0.02)
        assert operation.status == "succeeded"
        run = agent.runs.get(submitted.run_id)
        assert run.state == "finished" and run.result_completeness == "complete"
        assert run.snapshot.config_digest == resolution.config_digest
        trials = agent.runs.trials(run.id)
        scores = {(x.task_key, x.repetition_index): x.result.score for x in trials}
        assert scores[("add-02", 1)] == 0.0 and scores[("add-01", 1)] == 1.0
        assert agent.runs.summary(run.id).scored_trials == 6

        saved = agent.runs.download_artifacts(run.id, tmp_path / "evidence")
        assert saved and all(d.path for d in saved)
        output = next(d for d in saved if d.artifact.kind == "output")
        assert Path(output.path).is_file()
        assert Path(output.path).is_relative_to(tmp_path / "evidence" / "trials")

        # After losing all local state the agent finds its run again.
        assert [r.id for r in agent.runs.find("agent-round-1")] == [run.id]


def test_client_timeout_does_not_cancel_the_run(service, version_id, executor):
    executor.script = {"add-01": ["hang"]}
    with running(service) as client:
        admin = setup_admin(service, client)
        agent = sdk(create_pat(admin, "experiment"))
        config = {"dataset_version_id": version_id, "execution_mode": "oracle"}
        submitted = agent.runs.submit(config, name="slow")
        with pytest.raises(WaitTimeout) as timeout:
            agent.operations.wait(submitted.operation_id, timeout=0.2, interval=0.05)
        assert timeout.value.last.status in {"accepted", "running"}
        assert agent.runs.get(submitted.run_id).state in {"queued", "running"}
        agent.runs.cancel(submitted.run_id)
        assert agent.operations.wait(submitted.operation_id, interval=0.02).status == "cancelled"


# --- story three: query and export (E2E-OUT-01) ----------------------------------------


def test_story_three_agent_queries_and_exports(service, version_id, executor, tmp_path):
    executor.script = {
        "add-01": [scored(0.0)],
        "add-02": [AttemptOutcome(raw_score=None, reward_valid=True, verifier_status="missing")],
        "mul-01": [failed("sandbox_failed")],
    }
    with running(service) as client:
        admin = setup_admin(service, client)
        alice = invite_member(service, admin, "alice")
        agent = sdk(create_pat(alice, "experiment"))
        reader = sdk(create_pat(admin, "read"))
        start = clock.to_text(clock.now())
        config = {"dataset_version_id": version_id, "execution_mode": "oracle"}
        mixed = agent.runs.submit(config, name="mixed")
        agent.operations.wait(mixed.operation_id, interval=0.02)
        executor.script = {}
        clean = agent.runs.submit(config, name="clean")
        agent.operations.wait(clean.operation_id, interval=0.02)

        listed = list(reader.runs.list(dataset_version_id=version_id, accepted_after=start))
        assert {r.id for r in listed} == {mixed.run_id, clean.run_id}
        assert list(reader.runs.list(accepted_after="2099-01-01T00:00:00Z")) == []
        paged = list(reader.runs.list(limit=1))  # the iterator follows the cursor
        assert {r.id for r in paged} == {mixed.run_id, clean.run_id}

        by_id = {r.id: r for r in listed}
        scoring = by_id[mixed.run_id].scoring
        assert scoring.scored_trials == 1 and scoring.mean == 0.0  # a real zero
        assert scoring.excluded_error_trials == 2
        trials = {t.task_key: t for t in reader.runs.trials(mixed.run_id)}
        assert trials["add-01"].state == "scored" and trials["add-01"].result.score == 0.0
        assert trials["add-02"].state == "execution_error"
        assert trials["add-02"].result.score is None and trials["add-02"].reason_code
        assert trials["mul-01"].result.failure_cause == "sandbox_failed"

        package = reader.exports.create([mixed.run_id, clean.run_id])
        assert reader.operations.wait(package.operation_id, interval=0.02).status == "succeeded"
        package = reader.exports.get(package.id)
        assert package.state == "ready"
        path = reader.exports.download(package.id, tmp_path / "export.zip")
        archive = zipfile.ZipFile(path)
        root = f"export-{package.id}"
        manifest = json.loads(archive.read(f"{root}/manifest.json"))
        assert [r["run_id"] for r in manifest["runs"]] == [mixed.run_id, clean.run_id]
        names = archive.namelist()
        assert all(f"{root}/{entry['path']}" in names for entry in manifest["files"])
        content = b"".join(archive.read(n) for n in names)
        assert agent._token.encode() not in content and reader._token.encode() not in content


# --- transport ----------------------------------------------------------------------------


class LoseFirstAnswer(httpx.BaseTransport):
    """Delivers the request, then drops the first answer to a matching one."""

    def __init__(self, inner, method, path):
        self.inner, self.method, self.path = inner, method, path
        self.lost = 0

    def handle_request(self, request):
        response = self.inner.handle_request(request)
        if request.method == self.method and request.url.path == self.path and not self.lost:
            self.lost += 1
            response.read()
            raise httpx.ReadError("connection reset", request=request)
        return response


def test_a_lost_answer_is_retried_under_the_same_key(service, version_id):
    """E2E-RUN-02's lost response: one run, one unit of quota, the original answer."""
    with running(service) as client:
        admin = setup_admin(service, client)
        pat = create_pat(admin, "experiment", run_limit=2)
        transport = LoseFirstAnswer(pat.client._transport, "POST", "/api/v1/runs")
        http = httpx.Client(base_url="http://testserver", transport=transport)
        agent = sdk(pat, http=http)
        config = {"dataset_version_id": version_id, "execution_mode": "oracle"}
        submitted = agent.runs.submit(config, name="once", client_request_id="lost-answer")
        assert transport.lost == 1 and submitted.replayed
        assert len(agent.runs.find("lost-answer")) == 1
        pats = sdk(pat).me()
        assert pats.pat_id
        (record,) = admin.get("/api/v1/me/pats").json()["data"]
        assert record["accepted_runs"] == 1

        # A plain POST is not repeated: the caller learns the outcome is unknown.
        transport = LoseFirstAnswer(pat.client._transport, "POST", "/api/v1/exports")
        http = httpx.Client(base_url="http://testserver", transport=transport)
        with pytest.raises(TransportError):
            sdk(pat, http=http).exports.create([submitted.run_id])


def test_errors_are_structured_and_never_carry_the_token(service):
    with running(service) as client:
        setup_admin(service, client)
        bad = MaestroClient("http://testserver", "mae_pat_not-a-real-token", http=client)
        with pytest.raises(ApiError) as error:
            bad.me()
        assert error.value.status == 401 and error.value.request_id
        assert "not-a-real-token" not in str(error.value) and "not-a-real" not in repr(bad)
    with pytest.raises(ValueError):
        MaestroClient("http://user:secret@example.test", "t")


def test_attachment_published_from_a_directory(service, tmp_path):
    package = tmp_path / "tools"
    (package / "bin").mkdir(parents=True)
    (package / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "helper",
                "version": "1.0.0",
                "effects": ["agent_tools"],
                "compatible_harnesses": ["*"],
            }
        )
    )
    (package / "bin" / "run.sh").write_text("#!/bin/sh\necho hi\n")
    (package / "bin" / "run.sh").chmod(0o755)
    with running(service) as client:
        admin = setup_admin(service, client)
        agent = sdk(create_pat(admin, "experiment"))
        published = agent.attachments.publish(package)
        assert published.name == "helper" and published.version == "1.0.0"
        archive = tmp_path / "helper.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for path in sorted(package.rglob("*")):
                tar.add(path, arcname=str(path.relative_to(package)), recursive=False)
        assert agent.attachments.publish(archive).id == published.id  # same content
        assert [a.id for a in agent.attachments.list(name="helper")] == [published.id]
        (package / "link").symlink_to("manifest.json")
        with pytest.raises(ValueError):
            agent.attachments.publish(package)


# --- contract -----------------------------------------------------------------------------

# Web-account flows (session, setup, invitations, own PATs and password) and administration
# need a browser session by design; health checks are for the deployment.
NOT_FOR_PATS = (
    "/api/v1/admin/",
    "/api/v1/session",
    "/api/v1/setup",  # first administrator; ``/api/v1/setup-network`` is for PATs too
    "/api/v1/invitations/",
    "/api/v1/me/",
    "/api/v1/dataset-search",
    "/health/",
)
SDK_METHODS = {
    "capabilities": "capabilities",
    "get_me": "me",
    "system_status": "system_status",
    "list_sources": "catalog.sources",
    "list_datasets": "catalog.datasets",
    "get_dataset_version": "catalog.version",
    "list_tasks": "catalog.tasks",
    "get_preparation": "catalog.preparation",
    "list_model_configs": "configs.models",
    "get_model_config": "configs.model",
    "list_harnesses": "configs.harnesses",
    "get_harness": "configs.harness",
    "list_credential_names": "configs.credential_names",
    "get_team_policy": "configs.team_policy",
    "get_setup_network": "configs.setup_network",
    "list_setup_network_revisions": "configs.setup_network_revisions",
    "resolve_config": "configs.resolve",
    "get_resolution": "configs.resolution",
    "list_attachments": "attachments.list",
    "get_attachment": "attachments.get",
    "publish_attachment": "attachments.publish",
    "attachment_enabled": "attachments.set_enabled",
    "delete_attachment": "attachments.delete",
    "list_plans": "plans.list",
    "get_plan": "plans.get",
    "create_plan": "plans.create",
    "update_plan": "plans.update",
    "copy_plan": "plans.copy",
    "archive_plan": "plans.archive",
    "delete_plan": "plans.delete",
    "compare_runs": "comparisons.compare",
    "list_collections": "collections.list",
    "create_collection": "collections.create",
    "get_collection": "collections.get",
    "update_collection": "collections.update",
    "copy_collection": "collections.copy",
    "delete_collection": "collections.delete",
    "compare_collection": "collections.compare",
    "list_collection_candidates": "collections.candidates",
    "add_collection_runs": "collections.add_runs",
    "label_collection_run": "collections.label_run",
    "remove_collection_run": "collections.remove_run",
    "list_collection_events": "collections.events",
    "list_snapshots": "snapshots.list",
    "create_snapshot": "snapshots.create",
    "get_snapshot": "snapshots.get",
    "delete_snapshot": "snapshots.delete",
    "add_snapshot_note": "snapshots.add_note",
    "update_snapshot_note": "snapshots.update_note",
    "submit_run": "runs.submit",
    "rerun": "runs.rerun",
    "run_request": "runs.request",
    "get_run": "runs.get",
    "list_runs": "runs.list",
    "run_trials": "runs.trials",
    "run_summary": "runs.summary",
    "cancel_run": "runs.cancel",
    "delete_run": "runs.delete",
    "run_artifacts": "runs.artifacts",
    "preview_batch": "batches.preview",
    "submit_batch": "batches.submit",
    "get_batch": "batches.get",
    "list_batches": "batches.list",
    "cancel_batch": "batches.cancel",
    "get_operation": "operations.get",
    "list_operations": "operations.list",
    "download_artifact": "artifacts.download",
    "delete_artifact": "artifacts.delete",
    "create_export": "exports.create",
    "get_export": "exports.get",
    "list_exports": "exports.list",
    "download_export": "exports.download",
}


def test_every_operation_open_to_pats_has_an_sdk_method():
    document = json.loads((ROOT / "openapi" / "maestro-v1.json").read_text())
    public = {
        operation["operationId"]
        for path, methods in document["paths"].items()
        if not path.startswith(NOT_FOR_PATS) or path.startswith("/api/v1/setup-network")
        for operation in methods.values()
    }
    assert public == set(SDK_METHODS)
    client = MaestroClient("http://127.0.0.1:9")
    for dotted in SDK_METHODS.values():
        target = client
        for part in dotted.split("."):
            target = getattr(target, part)
        assert callable(target), dotted


def test_capabilities_report_the_contract_the_sdk_was_built_from(service):
    from maestro_sdk._contract import CONTRACT_DIGEST

    with running(service) as client:
        caps = MaestroClient("http://testserver", http=client).capabilities()
        assert caps.contract_digest == CONTRACT_DIGEST


def test_sdk_imports_without_the_service():
    code = (
        "import sys\n"
        "for name in ('maestro', 'fastapi', 'sqlalchemy', 'harbor'):\n"
        "    sys.modules[name] = None\n"
        "import maestro_sdk\n"
        "print(maestro_sdk.MaestroClient('http://127.0.0.1:9'))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    for path in (ROOT / "sdk" / "src" / "maestro_sdk").glob("*.py"):
        text = path.read_text()
        assert "from maestro." not in text and "import maestro." not in text, path


def test_generated_contract_and_sdk_models_are_current():
    """F-20: changing the API without regenerating fails here (web types: npm check:api)."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "generate_clients.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_the_skill_and_the_guide_only_name_real_sdk_calls():
    """skills/maestro-sdk/SKILL.md and sdk/README.md teach agents; a renamed method fails here."""
    client = MaestroClient("http://127.0.0.1:9")
    for doc in (ROOT / "skills" / "maestro-sdk" / "SKILL.md", ROOT / "sdk" / "README.md"):
        calls = set(re.findall(r"client\.([a-z_]+(?:\.[a-z_]+)?)\(", doc.read_text()))
        assert calls, doc
        for dotted in calls:
            target = client
            for part in dotted.split("."):
                assert hasattr(target, part), f"{doc.name}: client.{dotted}"
                target = getattr(target, part)
