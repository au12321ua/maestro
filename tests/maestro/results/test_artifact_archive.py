"""Artifacts packed per run, task, trial or attempt and streamed on request."""

import hashlib
import io
import json
import uuid
import zipfile
from pathlib import Path

import pytest
from maestro_sdk.resources.runs import _deliverable_path
from sqlalchemy import delete, select, update
from support import create_pat, running, setup_admin, submit, wait_operation
from support.sdk import sdk

from maestro.core import clock
from maestro.db import schema as t
from maestro.domain.deliverables import deliverable_path


def finished_run(api, version_id) -> str:
    accepted = submit(api, version_id).json()["data"]
    wait_operation(api, accepted["operation_id"])
    return accepted["run_id"]


def unpack(response) -> tuple[zipfile.ZipFile, str, dict]:
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = archive.namelist()
    root = names[0].split("/", 1)[0]
    assert all(n.startswith(f"{root}/") for n in names)
    assert names[-1] == f"{root}/manifest.json"  # written last, after every file
    return archive, root, json.loads(archive.read(names[-1]))


def test_every_level_packs_its_files_and_lists_what_is_left_out(service, version_id):
    with running(service) as client:
        admin = setup_admin(service, client)
        reader = create_pat(admin, ())
        run_id = finished_run(admin, version_id)
        base = f"/api/v1/runs/{run_id}/artifacts/archive"
        artifacts = reader.get(f"/api/v1/runs/{run_id}/artifacts").json()["data"]
        trials = reader.get(f"/api/v1/runs/{run_id}/trials").json()["data"]
        trial = next(x for x in trials if any(a["trial_id"] == x["id"] for a in artifacts))
        attempt = trial["attempts"][0]

        # One file is reclaimed, two get paths that sanitize alike or try to escape.
        own = [a for a in artifacts if a["attempt_id"] == attempt["id"]]
        assert len(own) >= 2
        stale = next(a for a in artifacts if a["attempt_id"] != attempt["id"])
        with service.db.write() as conn:
            conn.execute(
                update(t.artifacts)
                .where(t.artifacts.c.id == stale["id"])
                .values(expires_at=clock.to_text(clock.now() - clock.timedelta(minutes=1)))
            )
            for item, path in zip(own[:2], ["../../etc/passwd", "etc/passwd"], strict=False):
                conn.execute(
                    update(t.artifacts)
                    .where(t.artifacts.c.id == item["id"])
                    .values(relative_path=path)
                )
        assert service.artifacts.sweep()["reclaimed"] == 1

        response = reader.get(base, expect=200)
        assert response.headers["content-type"] == "application/zip"
        assert response.headers["content-disposition"].endswith('.zip"')
        archive, root, manifest = unpack(response)
        assert manifest["scope"] == {"run_id": run_id}
        listed = {f["artifact_id"] for f in manifest["files"]}
        assert listed | {m["artifact_id"] for m in manifest["missing"]} == {
            a["id"] for a in artifacts
        }
        assert [(m["artifact_id"], m["reason"]) for m in manifest["missing"]] == [
            (stale["id"], "EXPIRED")
        ]
        # Each packed file is the same content a single download gives.
        for entry in manifest["files"]:
            single = reader.get(f"/api/v1/artifacts/{entry['artifact_id']}/download").content
            assert archive.read(f"{root}/{entry['path']}") == single
            assert entry["digest"] == "sha256:" + hashlib.sha256(single).hexdigest()
        names = archive.namelist()
        assert len(names) == len(set(names))
        assert not any(".." in n.split("/") or n.startswith("/") for n in names)
        task_dir = f"{root}/tasks/{trial['task_key']}/trial-{trial['repetition_index']}"
        folder = f"{task_dir}/attempt-{attempt['attempt_index']}"
        # The escape stays inside its attempt; the second of the same name gets a suffix.
        assert f"{folder}/etc/passwd" in names
        assert sum(n.startswith(f"{folder}/etc/passwd") for n in names) == 2

        def packed(**query) -> set[str]:
            text = "&".join(f"{k}={v}" for k, v in query.items())
            _, _, narrow = unpack(reader.get(f"{base}?{text}", expect=200))
            assert narrow["scope"] == {"run_id": run_id, **query}
            return {f["artifact_id"] for f in narrow["files"]}

        assert packed(attempt_id=attempt["id"]) == {
            a["id"] for a in artifacts if a["attempt_id"] == attempt["id"]
        }
        assert packed(trial_id=trial["id"]) == {
            a["id"] for a in artifacts if a["trial_id"] == trial["id"]
        }
        same_task = {x["id"] for x in trials if x["task_key"] == trial["task_key"]}
        assert packed(task_key=trial["task_key"]) == {
            a["id"] for a in artifacts if a["trial_id"] in same_task
        } - {stale["id"]}

        assert reader.get(f"{base}?trial_id=trial_missing").status_code == 404
        assert reader.get(f"{base}?task_key=no-such-task").status_code == 404


@pytest.mark.parametrize("content", ["all", "deliverables"])
def test_a_run_that_has_not_ended_is_not_packed(service, version_id, content):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        queued = submit(admin, version_id).json()["data"]["run_id"]
        response = admin.get(f"/api/v1/runs/{queued}/artifacts/archive?content={content}")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "RUN_ACTIVE"


@pytest.mark.parametrize("content", ["all", "deliverables"])
def test_a_run_larger_than_the_limit_is_refused_before_streaming(
    service, version_id, content, monkeypatch
):
    with running(service) as client:
        admin = setup_admin(service, client)
        run_id = finished_run(admin, version_id)
        service.artifact_archives.max_bytes = 1

        def no_content(key):
            pytest.fail("oversized content was opened")

        monkeypatch.setattr(service.store, "open", no_content)
        response = admin.get(f"/api/v1/runs/{run_id}/artifacts/archive?content={content}")
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "ARCHIVE_TOO_LARGE"
        with service.db.read() as conn:
            assert conn.execute(select(t.task_reads)).all() == []


@pytest.mark.parametrize("purge", [False, True], ids=["recycled", "purged"])
def test_deleted_files_remain_in_missing_at_every_level(service, version_id, purge, monkeypatch):
    with running(service) as client:
        admin = setup_admin(service, client)
        run_id = finished_run(admin, version_id)
        artifacts = admin.get(f"/api/v1/runs/{run_id}/artifacts").json()["data"]
        chosen = artifacts[0]
        trials = admin.get(f"/api/v1/runs/{run_id}/trials").json()["data"]
        trial = next(t for t in trials if t["id"] == chosen["trial_id"])
        entry = admin.delete(f"/api/v1/artifacts/{chosen['id']}", expect=200).json()["data"]
        if purge:
            admin.post(
                f"/api/v1/admin/recycle-bin/{entry['id']}/purge",
                json={"confirm_object_id": chosen["id"]},
                expect=200,
            )
        # Soft deletion leaves the stored content available. It still must not be opened.
        with service.db.read() as conn:
            row = conn.execute(t.artifacts.select().where(t.artifacts.c.id == chosen["id"])).one()
        assert row.status == ("deleted" if purge else "available")
        original = service.artifact_archives.store.open

        def checked(key):
            assert key != row.storage_key, "deleted content was opened"
            return original(key)

        monkeypatch.setattr(service.artifact_archives.store, "open", checked)
        base = f"/api/v1/runs/{run_id}/artifacts/archive"
        for query in (
            "",
            f"?task_key={trial['task_key']}",
            f"?trial_id={chosen['trial_id']}",
            f"?attempt_id={chosen['attempt_id']}",
        ):
            _, _, manifest = unpack(admin.get(base + query, expect=200))
            assert chosen["id"] not in {a["artifact_id"] for a in manifest["files"]}
            assert (chosen["id"], "DELETED") in {
                (a["artifact_id"], a["reason"]) for a in manifest["missing"]
            }
        # Deleted files do not consume the archive's size limit either.
        with service.db.write() as conn:
            conn.execute(
                update(t.artifacts)
                .where(t.artifacts.c.id == chosen["id"])
                .values(size_bytes=10**12)
            )
        admin.get(base, expect=200)


def test_a_file_recycled_after_selection_is_not_opened(service, version_id, monkeypatch):
    with running(service) as client:
        admin = setup_admin(service, client)
        run_id = finished_run(admin, version_id)
        artifacts = admin.get(f"/api/v1/runs/{run_id}/artifacts").json()["data"]
        _, stream = service.artifact_archives.open(run_id)
        for item in artifacts:
            admin.delete(f"/api/v1/artifacts/{item['id']}", expect=200)

        def no_content(key):
            pytest.fail("recycled content was opened")

        monkeypatch.setattr(service.artifact_archives.store, "open", no_content)
        archive = zipfile.ZipFile(io.BytesIO(b"".join(stream)))
        assert len(archive.namelist()) == 1
        manifest = json.loads(archive.read(archive.namelist()[0]))
        assert manifest["files"] == []
        assert {a["artifact_id"] for a in manifest["missing"]} == {a["id"] for a in artifacts}
        assert {a["reason"] for a in manifest["missing"]} == {"DELETED"}


@pytest.mark.parametrize(
    "path,expected",
    [
        ("artifacts/manifest.json", None),
        ("artifacts/logs/artifacts/nested/output.step", "nested/output.step"),
        ("artifacts/logs/artifacts/manifest.json", "manifest.json"),
        ("artifacts/logs/other/output.step", "logs/other/output.step"),
        ("artifacts/output.step", "output.step"),
        ("artifacts/子目录/part + 1.step", "子目录/part + 1.step"),
        ("artifacts/sub/manifest.json", "sub/manifest.json"),
        ("other/file.txt", "other/file.txt"),
    ],
)
def test_deliverable_paths_match_the_standalone_sdk(path, expected):
    assert deliverable_path(path) == _deliverable_path(path) == expected


def copy_artifact(service, artifact, **changes):
    item = {**artifact, "id": "art_" + uuid.uuid4().hex, **changes}
    item["storage_key"] = item["id"]
    raw = service.store.files[artifact["storage_key"]]
    service.store.files[item["storage_key"]] = raw
    item.update(digest="sha256:" + hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))
    with service.db.write() as conn:
        conn.execute(t.artifacts.insert().values(**item))
    return item


@pytest.fixture
def deliverables_run(service, version_id):
    with running(service) as client:
        admin = setup_admin(service, client)
        accepted = submit(admin, version_id, repetitions=2).json()["data"]
        wait_operation(admin, accepted["operation_id"])
        run_id = accepted["run_id"]
        with service.db.write() as conn:
            conn.execute(
                update(t.artifacts)
                .where(t.artifacts.c.run_id == run_id, t.artifacts.c.kind == "output")
                .values(relative_path="artifacts/logs/artifacts/nested/output.step")
            )
            artifacts = [
                dict(row)
                for row in conn.execute(
                    select(t.artifacts).where(t.artifacts.c.run_id == run_id)
                ).mappings()
            ]
        trials = admin.get(f"/api/v1/runs/{run_id}/trials").json()["data"]
        yield admin, run_id, trials, artifacts


@pytest.mark.parametrize("has_result", [True, False], ids=["result-attempt", "highest-attempt"])
def test_deliverables_archive_matches_sdk_files_and_final_attempts(
    service, deliverables_run, tmp_path, has_result
):
    admin, run_id, trials, artifacts = deliverables_run
    trial = trials[0]
    output = next(a for a in artifacts if a["trial_id"] == trial["id"] and a["kind"] == "output")
    with service.db.write() as conn:
        attempt = dict(
            conn.execute(
                select(t.execution_attempts).where(
                    t.execution_attempts.c.id == output["attempt_id"]
                )
            )
            .mappings()
            .one()
        )
        attempt.update(id="att_" + uuid.uuid4().hex, attempt_index=2)
        conn.execute(t.execution_attempts.insert().values(**attempt))
        if not has_result:
            conn.execute(delete(t.trial_results).where(t.trial_results.c.trial_id == trial["id"]))
    later = copy_artifact(service, output, attempt_id=attempt["id"])
    raw = b"later attempt output"
    service.store.files[later["storage_key"]] = raw
    with service.db.write() as conn:
        conn.execute(
            update(t.artifacts)
            .where(t.artifacts.c.id == later["id"])
            .values(digest="sha256:" + hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))
        )
    for path in (
        "artifacts/manifest.json",
        "artifacts/logs/artifacts/manifest.json",
        "artifacts/logs/other/answer.step",
        "artifacts/子目录/part + 1.step",
        "artifacts/answer.step",
        "artifacts/logs/artifacts/answer.step",
    ):
        copy_artifact(service, later if not has_result else output, relative_path=path)
    # Run-level outputs and logs have no place in a deliverables package.
    copy_artifact(service, output, trial_id=None, attempt_id=None)
    reader = sdk(create_pat(admin, ()))
    directory = tmp_path / "sdk-files"
    downloads = reader.runs.download_deliverables(run_id, directory)
    expected = {
        Path(d.path).relative_to(directory).as_posix(): Path(d.path).read_bytes() for d in downloads
    }
    saved = reader.runs.download_archive(run_id, tmp_path / "delivery.zip", content="deliverables")
    with zipfile.ZipFile(saved) as archive:
        assert archive.namelist()[-1] == "manifest.json"
        assert len(archive.namelist()) == len(set(archive.namelist()))
        actual = {
            name: archive.read(name) for name in archive.namelist() if name != "manifest.json"
        }
        assert actual == expected
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["scope"] == {"run_id": run_id, "content": "deliverables"}
    assert {f["path"] for f in manifest["files"]} == set(expected)
    chosen = later if not has_result else output
    included = {f["artifact_id"] for f in manifest["files"]}
    assert chosen["id"] in included
    assert (output if not has_result else later)["id"] not in included
    assert not manifest["missing"] and not manifest["warnings"]
    base = f"/api/v1/runs/{run_id}/artifacts/archive?content=deliverables"
    narrow = admin.get(base + f"&attempt_id={attempt['id']}", expect=200)
    with zipfile.ZipFile(io.BytesIO(narrow.content)) as archive:
        files = json.loads(archive.read("manifest.json"))["files"]
        assert bool(files) is (not has_result)
    assert admin.get(base.replace("deliverables", "unknown")).status_code == 422


@pytest.mark.parametrize("channel", ["web_session", "pat"])
def test_deliverables_record_only_included_tasks_before_opening_files(
    service, deliverables_run, channel, monkeypatch
):
    admin, run_id, trials, artifacts = deliverables_run
    reader = admin if channel == "web_session" else create_pat(admin, ())
    keys = sorted({trial["task_key"] for trial in trials})
    included_trials = {trial["id"] for trial in trials if trial["task_key"] == keys[0]}
    included_tasks = {trial["task_id"] for trial in trials if trial["id"] in included_trials}
    with service.db.write() as conn:
        for artifact in artifacts:
            if artifact["trial_id"] in included_trials:
                continue
            trial = next(trial for trial in trials if trial["id"] == artifact["trial_id"])
            changes = (
                {"status": "expired"}
                if trial["task_key"] == keys[1]
                else {"relative_path": "artifacts/manifest.json"}
            )
            conn.execute(
                update(t.artifacts).where(t.artifacts.c.id == artifact["id"]).values(**changes)
            )
    original = service.store.open
    opened = []

    def checked(key):
        with service.db.read() as conn:
            rows = conn.execute(select(t.task_reads)).mappings().all()
        assert {row["task_id"] for row in rows} == included_tasks
        assert all(row["count"] == 1 and row["channel"] == channel for row in rows)
        opened.append(key)
        return original(key)

    monkeypatch.setattr(service.store, "open", checked)
    # Excluded logs and bookkeeping must not consume the deliverables size allowance.
    with service.db.write() as conn:
        conn.execute(
            update(t.artifacts).where(t.artifacts.c.kind != "output").values(size_bytes=10**12)
        )
    response = reader.get(
        f"/api/v1/runs/{run_id}/artifacts/archive?content=deliverables", expect=200
    )
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert {f["trial_id"] for f in manifest["files"]} == included_trials
    assert len(opened) == len(included_trials)
    assert {m["reason"] for m in manifest["missing"]} == {"EXPIRED"}


@pytest.mark.parametrize(
    "status,reason",
    [
        ("expired", "EXPIRED"),
        ("failed", "SAVE_FAILED"),
        ("deleted", "DELETED"),
        ("purged", "DELETED"),
    ],
)
def test_deliverables_manifest_lists_missing_outputs(service, deliverables_run, status, reason):
    admin, run_id, _, artifacts = deliverables_run
    output = next(a for a in artifacts if a["kind"] == "output")
    if status in ("deleted", "purged"):
        entry = admin.delete(f"/api/v1/artifacts/{output['id']}", expect=200).json()["data"]
        if status == "purged":
            admin.post(
                f"/api/v1/admin/recycle-bin/{entry['id']}/purge",
                json={"confirm_object_id": output["id"]},
                expect=200,
            )
    else:
        with service.db.write() as conn:
            conn.execute(
                update(t.artifacts)
                .where(t.artifacts.c.id == output["id"])
                .values(status=status, size_bytes=10**12)
            )
    response = admin.get(
        f"/api/v1/runs/{run_id}/artifacts/archive?content=deliverables", expect=200
    )
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert [(m["artifact_id"], m["reason"]) for m in manifest["missing"]] == [
        (output["id"], reason)
    ]
    assert output["id"] not in {f["artifact_id"] for f in manifest["files"]}


def test_deliverables_refuse_paths_the_sdk_cannot_write(service, deliverables_run):
    admin, run_id, _, artifacts = deliverables_run
    output = next(a for a in artifacts if a["kind"] == "output")
    with service.db.write() as conn:
        conn.execute(
            update(t.artifacts)
            .where(t.artifacts.c.id == output["id"])
            .values(relative_path="artifacts/logs/artifacts/../../outside.step")
        )
    response = admin.get(f"/api/v1/runs/{run_id}/artifacts/archive?content=deliverables")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_DELIVERABLE_PATH"
    with service.db.read() as conn:
        assert conn.execute(select(t.task_reads)).all() == []
