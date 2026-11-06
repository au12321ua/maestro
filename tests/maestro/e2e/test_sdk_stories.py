"""The three user stories walked by an agent through the Python SDK only.

Each story uses nothing but ``maestro_sdk`` with a PAT (the administrator's part of the
precondition uses the web session). The SDK talks to the real application through the
TestClient transport.
"""

import json
import zipfile
from pathlib import Path

import pytest
from maestro_sdk import (
    ApiError,
    BlockersPresent,
    UnsupportedCapability,
    models,
)
from support import create_pat, invite_member, running, setup_admin
from support.configuration import prepare_model
from support.datasets import evict, import_and_prepare, tasks_by_key
from support.sdk import sdk

from maestro.boundaries.executor import AttemptOutcome
from maestro.core import clock
from maestro.testing.fakes import failed, scored


def test_story_one_agent_reads_the_catalog_and_runs_on_a_cold_cache(service, source):
    source.add("demo", "solo", "v1", ["solo"])
    with running(service) as client:
        admin = setup_admin(service, client)
        version_id, _ = import_and_prepare(admin, name="solo")
        (task,) = tasks_by_key(admin, version_id).values()
        evict(admin, task)
        member = invite_member(service, admin, "member")
        reader = sdk(create_pat(member, permissions=()))
        agent = sdk(create_pat(member, permissions=("experiment",), run_limit=5))

        for pat in (reader, agent):
            (dataset,) = pat.catalog.datasets()
            assert dataset.versions[0].id == version_id
            detail = pat.catalog.version(version_id)
            assert detail.admitted_task_count == 1 and detail.cache_ready_task_count == 0
            (listed,) = pat.catalog.tasks(version_id)
            assert listed.admission.state == "admitted" and listed.can_submit
            assert listed.local_preparation.cache_state == "stale"
            assert [s.kind for s in pat.catalog.sources()] == ["fake", "upload"]
            # Searching the external source and administering are not SDK business.
            with pytest.raises(ApiError) as denied:
                pat._call("GET", "dataset-search", params={"q": "solo", "source_kind": "fake"})
            assert denied.value.status == 403
            with pytest.raises(ApiError) as denied:
                pat._call("POST", f"admin/dataset-versions/{version_id}/preparations", json={})
            assert denied.value.code == "FORBIDDEN"  # a member's token, capped by role

        config = {"dataset_version_id": version_id, "execution_mode": "oracle"}
        with pytest.raises(ApiError) as refused:
            reader.runs.submit(config, name="read-only")
        assert refused.value.code == "PAT_PERMISSION_INSUFFICIENT"

        submitted = agent.runs.submit(config, name="cold cache")
        assert agent.operations.wait(submitted.operation_id, interval=0.02).status == "succeeded"
        assert agent.runs.get(submitted.run_id).counts.scored == 1  # restored, no admin step
        preparation = reader.catalog.preparation(listed.local_preparation.id)
        assert preparation.cache_state == "ready" and preparation.pins == []


def test_story_two_agent_runs_a_complete_experiment(service, version_id, executor, tmp_path):
    executor.script = {"add-02": [scored(0.0)]}
    with running(service) as client:
        admin = setup_admin(service, client)
        model_id, harness_id = prepare_model(admin)
        agent = sdk(create_pat(admin, ("experiment",), run_limit=3))

        caps = agent.require("runs", "execution", "configuration_resolution")
        assert caps.actions["runs.submit"].pat_permission == "experiment"
        caps = agent.require("collections", "comparisons", "analysis_snapshots")
        assert caps.actions["collections.create"].pat_permission == "experiment"
        read = caps.actions["comparisons.read"]
        assert read.pat and read.pat_permission is None  # every token may read
        with pytest.raises(UnsupportedCapability):
            agent.require("unknown_feature")

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
        archive = agent.runs.download_archive(
            run.id, tmp_path / "trial.zip", trial_id=output.artifact.trial_id
        )
        with zipfile.ZipFile(archive) as opened:
            names = opened.namelist()
        assert names[-1].endswith("/manifest.json") and len(names) > 1

        # After losing all local state the agent finds its run again.
        assert [r.id for r in agent.runs.find("agent-round-1")] == [run.id]


def test_story_three_agent_queries_and_exports(service, version_id, executor, tmp_path):
    executor.script = {
        "add-01": [scored(0.0)],
        "add-02": [AttemptOutcome(raw_score=None, reward_valid=True, verifier_status="missing")],
        "mul-01": [failed("sandbox_failed")],
    }
    with running(service) as client:
        admin = setup_admin(service, client)
        alice = invite_member(service, admin, "alice")
        agent = sdk(create_pat(alice, ("experiment",)))
        reader = sdk(create_pat(admin, ()))
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
