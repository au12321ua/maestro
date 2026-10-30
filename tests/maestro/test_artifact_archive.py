"""Artifacts packed per run, task, trial or attempt and streamed on request (07 section 5)."""

import hashlib
import io
import json
import zipfile

import pytest
from sqlalchemy import update
from support import create_pat, running, setup_admin, submit, wait_operation

from maestro.core import clock
from maestro.db import schema as t


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
        reader = create_pat(admin, "read")
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


def test_a_run_that_has_not_ended_is_not_packed(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        queued = submit(admin, version_id).json()["data"]["run_id"]
        response = admin.get(f"/api/v1/runs/{queued}/artifacts/archive")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "RUN_ACTIVE"


def test_a_run_larger_than_the_limit_is_refused_before_streaming(service, version_id):
    with running(service) as client:
        admin = setup_admin(service, client)
        run_id = finished_run(admin, version_id)
        service.artifact_archives.max_bytes = 1
        response = admin.get(f"/api/v1/runs/{run_id}/artifacts/archive")
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "ARCHIVE_TOO_LARGE"


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
