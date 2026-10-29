"""Artifacts packed per run, task, trial or attempt and streamed on request (07 section 5)."""

import hashlib
import io
import json
import zipfile

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
