"""Attachment workflow composition through the in-process public HTTP service."""

import json
from dataclasses import replace

import pytest
from maestro_sdk import ContentMismatch
from maestro_sdk.workflows.attachments import (
    build_report,
    collect_evidence,
    prepare_candidate,
    write_task_briefs,
)
from sqlalchemy import select
from support import create_pat, running, setup_admin
from support.configuration import prepare_model
from support.results import backdate
from support.sdk import sdk

from maestro.boundaries.executor import AttemptOutcome
from maestro.db import schema as t
from maestro.testing.fakes import failed, scored


@pytest.fixture
def workflow(service, version_id, tmp_path):
    with running(service) as http:
        admin = setup_admin(service, http)
        model_id, harness_id = prepare_model(admin)
        client = sdk(create_pat(admin, ("experiment",)))
        package = tmp_path / "package"
        package.mkdir()
        (package / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "name": "tools",
                    "effects": ["agent_tools"],
                    "compatible_harnesses": ["*"],
                }
            )
        )
        (package / "bootstrap.sh").write_text("#!/bin/sh\necho installed\n")
        (package / "changed.txt").write_text("before")
        (package / "removed.txt").write_text("removed")
        parent = client.attachments.publish(package)
        (package / "changed.txt").write_text("after")
        (package / "removed.txt").unlink()
        (package / "added.txt").write_text("added")
        candidate = client.attachments.publish(package, parent=parent)
        collection = client.collections.create(
            "Attachment experiment",
            "attachments",
            base_request={
                "dataset_version_id": version_id,
                "execution_mode": "agent",
                "model_config_id": model_id,
                "harness_version_id": harness_id,
                "attachments": [parent.id],
                "repetitions": 2,
            },
        )
        yield client, collection, parent, candidate


def finish(client, collection, snapshot, **config):
    submitted = client.runs.submit(
        {"attachments": [snapshot.id], **config}, name="candidate", collection_id=collection.id
    )
    assert client.operations.wait(submitted.operation_id, interval=0.02).status == "succeeded"
    return submitted.run_id


def add_artifacts(monkeypatch, executor, files):
    """Make the fake runner emit saved-format evidence through the real execution sink."""
    execute = executor.execute

    async def with_evidence(spec, sink, cancel):
        save = sink.artifact

        async def artifact(trial_id, attempt, kind, path, data, append=False):
            await save(trial_id, attempt, kind, path, data, append)
            if path == "result.txt":
                for name, payload in files.items():
                    await save(trial_id, attempt, "trial", name, payload)

        sink.artifact = artifact
        return await execute(spec, sink, cancel)

    monkeypatch.setattr(executor, "execute", with_evidence)


def test_task_briefs_copy_public_catalog_and_select_by_key_or_id(workflow, tmp_path):
    client, collection, _, _ = workflow
    version_id = collection.template.dataset_version_id
    catalog = client.catalog.tasks(version_id)
    paths = write_task_briefs(
        client,
        version_id,
        tmp_path / "briefs",
        tasks=[catalog[0].task_key, catalog[1].id, catalog[0].id],
    )
    assert len(paths) == 2
    for task, path in zip(catalog, paths, strict=False):
        assert path.name == task.id
        assert (path / "instruction.md").read_text() == task.instruction
        data = json.loads((path / "metadata.json").read_text())
        assert data["content"]["digest"] == task.content.digest
        assert data["spec_digest"] == task.spec_digest
        assert data["resources"] == task.resources.model_dump(mode="json")
        assert data["timeouts"] == task.timeouts.model_dump(mode="json")
        assert data["parameters"] == task.model_dump(mode="json")["parameters"]
        assert data["dataset_version"]["id"] == version_id
    with pytest.raises(ValueError, match="Unknown or ambiguous task"):
        write_task_briefs(client, version_id, tmp_path / "bad", tasks=["../unknown"])


def test_prepare_uses_collection_resolution_and_parent_install_records(
    workflow,
    tmp_path,
    monkeypatch,
    executor,
    service,
):
    client, collection, parent, candidate = workflow
    add_artifacts(
        monkeypatch,
        executor,
        {
            "maestro/attachments.json": json.dumps(
                [
                    {"content_digest": parent.content_digest, "status": "ok", "exit_code": 0},
                ]
            ).encode()
        },
    )
    run_id = finish(client, collection, parent)
    before = len(list(client.runs.list()))
    tasks = client.catalog.tasks(collection.template.dataset_version_id)
    prepared = prepare_candidate(
        client, collection.id, parent.id, candidate.id, tasks=[tasks[0].id, tasks[1].task_key]
    )
    assert len(list(client.runs.list())) == before
    assert prepared["planned_trials"] == len(prepared["trials"]) == 4
    assert {trial["repetition_index"] for trial in prepared["trials"]} == {1, 2}
    assert not prepared["blockers"]
    resolved = client.configs.resolve(
        {
            "collection_id": collection.id,
            "attachments": [candidate.id],
            "task_selection": {"mode": "explicit", "task_ids": [tasks[0].id, tasks[1].id]},
        }
    )
    assert prepared["resolution"]["config_digest"] == resolved.config_digest
    diff = prepared["diff"]
    assert [f["path"] for f in diff["added"]] == ["added.txt"]
    assert [f["path"] for f in diff["removed"]] == ["removed.txt"]
    assert [f["path"] for f in diff["changed"]] == ["changed.txt"]
    assert diff["changed"][0]["before"]["sha256"] != diff["changed"][0]["after"]["sha256"]
    hint = prepared["install_reuse_hint"]
    assert hint["bootstrap_unchanged"] and hint["parent_install_succeeded"]
    assert {i["run_id"] for i in hint["observations"]} == {run_id}

    for a in client.runs.artifacts(run_id):
        if a.relative_path == "maestro/attachments.json":
            backdate(service, a.id)
    service.artifacts.sweep()
    expired = prepare_candidate(client, collection.id, parent.id, candidate.id)
    assert not expired["install_reuse_hint"]["parent_install_succeeded"]
    assert all(i["status"] == "unknown" for i in expired["install_reuse_hint"]["observations"])

    package = tmp_path / "package"
    (package / "bootstrap.sh").write_text("#!/bin/sh\necho different\n")
    changed = client.attachments.publish(package, parent=candidate)
    client.attachments.set_enabled(changed.id, False)
    blocked = prepare_candidate(client, collection.id, parent.id, changed.id)
    assert blocked["blockers"] and not blocked["install_reuse_hint"]["bootstrap_unchanged"]


def lines(*events):
    return b"".join((json.dumps(event) + "\n").encode() for event in events)


def test_prepare_does_not_infer_installs_from_scores_or_other_snapshots(
    workflow,
    executor,
    monkeypatch,
):
    client, collection, parent, candidate = workflow
    finish(client, collection, parent)
    prepared = prepare_candidate(client, collection.id, parent.id, candidate.id)
    assert not prepared["install_reuse_hint"]["parent_install_succeeded"]
    assert prepared["install_reuse_hint"]["observations"][0]["status"] == "unknown"
    add_artifacts(
        monkeypatch,
        executor,
        {
            "maestro/attachments.json": json.dumps(
                [
                    {"content_digest": candidate.content_digest, "status": "ok"},
                    {"content_digest": parent.content_digest, "status": "failed", "exit_code": 1},
                ]
            ).encode()
        },
    )
    finish(client, collection, parent)
    prepared = prepare_candidate(client, collection.id, parent.id, candidate.id)
    assert not prepared["install_reuse_hint"]["parent_install_succeeded"]
    assert any(i["status"] == "failed" for i in prepared["install_reuse_hint"]["observations"])


def test_evidence_lists_malformed_streams_and_unstructured_trajectories_as_not_indexed(
    workflow,
    executor,
    monkeypatch,
    tmp_path,
):
    client, collection, parent, _ = workflow
    add_artifacts(
        monkeypatch,
        executor,
        {
            "agent/codex.txt": b'{"type":"item.completed","item":null}\n',
            "agent/opencode.txt": b'{"type":"tool_use",',
            "agent/claude-code.txt": lines(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "id": "unpaired",
                                "input": {"command": "pwd"},
                            },
                        ]
                    },
                }
            ),
            "agent/trajectory.json": json.dumps(
                {
                    "steps": [
                        {
                            "source": "agent",
                            "message": "$ pwd",
                            "observation": {"results": [{"content": "/app"}]},
                        }
                    ]
                }
            ).encode(),
        },
    )
    run_id = finish(client, collection, parent)
    evidence = collect_evidence(client, [run_id], tmp_path / "unindexed")
    assert evidence["event_index"]
    assert all(
        i["status"] == "not indexed" and i["reason"] and not i["events"]
        for i in evidence["event_index"]
    )


def test_evidence_downloads_incrementally_and_indexes_saved_harness_formats(
    workflow,
    monkeypatch,
    executor,
    tmp_path,
    service,
):
    client, collection, parent, _ = workflow
    files = {
        "agent/codex.txt": b"Reading prompt from stdin...\n"
        + lines(
            {"type": "item.started", "item": {"type": "command_execution", "command": "echo hi"}},
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "echo hi",
                    "exit_code": 0,
                    "aggregated_output": "hi\n",
                },
            },
        ),
        "agent/opencode.txt": lines(
            {
                "type": "tool_use",
                "part": {
                    "tool": "bash",
                    "state": {
                        "status": "completed",
                        "input": {"command": "cat x"},
                        "output": "missing",
                        "metadata": {"exit": 1},
                    },
                },
            }
        ),
        "agent/claude-code.txt": lines(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "id": "call1",
                            "input": {"command": "pwd"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": "call1", "content": "/app"}]
                },
            },
        ),
        "agent/trajectory.json": json.dumps(
            {
                "steps": [
                    {
                        "source": "agent",
                        "tool_calls": [
                            {
                                "function_name": "bash_command",
                                "tool_call_id": "c1",
                                "arguments": {"keystrokes": "ls\n"},
                            },
                            {
                                "function_name": "bash_command",
                                "tool_call_id": "c2",
                                "arguments": {"keystrokes": "pwd\n"},
                            },
                        ],
                        "observation": {"results": [{"source_call_id": None, "content": "/app"}]},
                    }
                ]
            }
        ).encode(),
        "agent/custom.jsonl": lines({"command": "unknown"}),
        "agent/broken.log": b"$ not structured\n",
    }
    add_artifacts(monkeypatch, executor, files)
    run_id = finish(client, collection, parent)
    directory = tmp_path / "evidence"
    first = collect_evidence(client, [run_id], directory)
    assert all(a["downloaded"] and a["skipped_reason"] is None for a in first["artifacts"])
    assert json.loads((directory / "manifest.json").read_text())["artifacts"] == first["artifacts"]
    index = first["event_index"]
    for filename, command, code in [
        ("codex.txt", "echo hi", 0),
        ("opencode.txt", "cat x", 1),
        ("claude-code.txt", "pwd", None),
    ]:
        entry = next(i for i in index if i["file"].endswith(filename))
        assert entry["status"] == "indexed"
        (event,) = entry["events"]
        assert (event["command"], event["exit_code"]) == (command, code)
        assert event["line"] >= 1 and event["output"] and event["file"] == entry["file"]
    atif = next(i for i in index if i["file"].endswith("trajectory.json"))
    assert [e["command"] for e in atif["events"]] == ["ls\n", "pwd\n"]
    assert all(e["output"][0]["shared"] for e in atif["events"])
    assert any(i["status"] == "not indexed" and i["reason"] for i in index)

    downloads = []
    download = client.artifacts.download

    def counted(artifact, target):
        downloads.append(artifact.id)
        return download(artifact, target)

    monkeypatch.setattr(client.artifacts, "download", counted)
    second = collect_evidence(client, [run_id], directory)
    assert not downloads
    assert all(a["skipped_reason"] == "local digest matches" for a in second["artifacts"])
    damaged = second["artifacts"][0]
    (directory / damaged["local_path"]).write_text("damaged")
    collect_evidence(client, [run_id], directory)
    assert downloads == [damaged["id"]]

    trial = client.runs.trials(run_id)[0]
    scoped = collect_evidence(
        client, [run_id], directory, tasks=[trial.task_id], trial_ids=[trial.id]
    )
    assert all(
        a["trial_id"] == trial.id or a["skipped_reason"] == "outside selection"
        for a in scoped["artifacts"]
    )
    stale = next(a for a in first["artifacts"] if a["relative_path"] == "agent/codex.txt")
    backdate(service, stale["id"])
    service.artifacts.sweep()
    expired = collect_evidence(client, [run_id], directory)
    assert (
        next(a for a in expired["artifacts"] if a["id"] == stale["id"])["skipped_reason"]
        == "RETENTION_EXPIRED"
    )
    assert (
        next(i for i in expired["event_index"] if i["artifact_id"] == stale["id"])["status"]
        == "not indexed"
    )


def test_evidence_refuses_wrong_digests_and_destination_links(workflow, service, tmp_path):
    client, collection, parent, _ = workflow
    run_id = finish(client, collection, parent)
    artifact = client.runs.artifacts(run_id)[0]
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = tmp_path / "evidence"
    destination.mkdir()
    (destination / "runs").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="link"):
        collect_evidence(client, [run_id], destination)
    assert not list(outside.iterdir())
    # The destination itself may be reached through a link the caller chose.
    (tmp_path / "real").mkdir()
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(tmp_path / "real", target_is_directory=True)
    collect_evidence(client, [run_id], linked_root)
    assert (tmp_path / "real" / "manifest.json").is_file()
    with service.db.write() as conn:
        key = conn.execute(
            select(t.artifacts.c.storage_key).where(t.artifacts.c.id == artifact.id)
        ).scalar_one()
    service.artifacts.store.files[key] = b"tampered"
    with pytest.raises(ContentMismatch):
        collect_evidence(client, [run_id], tmp_path / "corrupt")


def test_report_keeps_failures_and_computes_completeness_from_comparison(
    workflow,
    executor,
    tmp_path,
):
    client, collection, parent, candidate = workflow
    executor.script = {
        "add-01": [scored(0.0)],
        "add-02": [replace(scored(1.0), usage={"cost_usd": 0.5})],
    }
    baseline = finish(client, collection, parent)
    client.collections.label_run(collection.id, baseline, "baseline label")
    complete = build_report(client, collection.id, tmp_path / "complete", baseline_run_id=baseline)
    assert complete["complete"]
    (row,) = complete["runs"]
    assert row["label"] == "baseline label"
    assert row["planned_trials"] == row["scored_trials"] == 6
    assert row["attachments"][0]["version_id"] == parent.id
    assert row["usage"] == {
        "known_trials": 2,
        "unknown_trials": 4,
        "known_totals": {"cost_usd": 1.0},
    }
    assert any(t["mean"] == 0.0 for t in row["summary"]["tasks"])
    assert row["scoring_contract"]
    assert json.loads((tmp_path / "complete" / "report.json").read_text()) == complete
    markdown = (tmp_path / "complete" / "report.md").read_text()
    assert "baseline label" in markdown
    assert "| Task | Status |" in markdown and "```json" not in markdown

    executor.script = {
        "add-01": [failed("sandbox_failed")],
        "add-02": [AttemptOutcome(raw_score=None, reward_valid=True, verifier_status="missing")],
    }
    finish(client, collection, candidate)
    client.collections.update(
        collection.id, settings={"exclude": ["environment_setup", "verifier"]}
    )
    partial = build_report(client, collection.id, tmp_path / "partial")
    assert not partial["complete"]
    assert partial["comparison"]["settings"]["exclude"] == []
    assert any(r["failed_trials"] and r["failed_by_group"] for r in partial["runs"])
    dropped = build_report(
        client, collection.id, tmp_path / "dropped", exclude=["environment_setup", "verifier"]
    )
    assert dropped["comparison"]["dropped_tasks"] > 0 and not dropped["complete"]


def test_report_marks_missing_tasks_incomplete_even_when_all_trials_scored(workflow, tmp_path):
    client, collection, parent, candidate = workflow
    tasks = client.catalog.tasks(collection.template.dataset_version_id)
    finish(client, collection, parent)
    finish(
        client,
        collection,
        candidate,
        task_selection={"mode": "explicit", "task_ids": [tasks[0].id]},
    )
    report = build_report(client, collection.id, tmp_path / "missing")
    assert all(r["scored_trials"] == r["planned_trials"] for r in report["runs"])
    assert report["comparison"]["common_tasks"] < report["comparison"]["all_tasks"]
    assert not report["complete"]
