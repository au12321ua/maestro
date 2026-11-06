"""Task content reads are attributed before any content is returned."""

import io
import json
import zipfile

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from support import create_pat, running, setup_admin, wait_operation
from support.results import finished_run

from maestro.core import clock
from maestro.core.errors import MaestroError
from maestro.db import schema as t
from maestro.services import task_reads


def summaries(api, ids):
    return api.post("/api/v1/tasks/reads", json={"task_ids": ids}, expect=200).json()["data"]


def make_export(api, run_id, **options):
    exported = api.post(
        "/api/v1/exports", json={"run_ids": [run_id], **options}, expect=202
    ).json()["data"]
    wait_operation(api, exported["operation_id"])
    return f"/api/v1/exports/{exported['id']}/download"


def test_instruction_lists_are_content_free_and_reads_keep_each_channel(
    service, version_id, monkeypatch
):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        pats = [create_pat(admin, ()) for _ in range(2)]
        tasks = admin.get(f"/api/v1/dataset-versions/{version_id}/tasks", expect=200).json()["data"]
        ids = [task["id"] for task in tasks]
        assert all("instruction" not in task for task in tasks)
        assert summaries(pats[0], ids) == []
        task_id = ids[0]
        url = f"/api/v1/tasks/{task_id}"
        assert "instruction" in admin.get(url, expect=200).json()["data"]
        pats[0].get(url, expect=200)
        first = summaries(admin, ids)
        later = clock.now() + clock.timedelta(seconds=2)
        monkeypatch.setattr(clock, "now", lambda: later)
        pats[0].get(url, expect=200)
        pats[1].get(url, expect=200)
        rows = summaries(admin, ids)
        assert len(rows) == 3
        assert {r["task_id"] for r in rows} == {task_id}
        assert {r["category"] for r in rows} == {"instruction"}
        web = next(r for r in rows if r["channel"] == "web_session")
        assert web["channel_id"] and web["count"] == 1
        tokens = [r for r in rows if r["channel"] == "pat"]
        assert len({r["channel_id"] for r in tokens}) == 2
        repeated = next(r for r in tokens if r["count"] == 2)
        initial = next(r for r in first if r["channel_id"] == repeated["channel_id"])
        assert repeated["first_read_at"] == initial["first_read_at"]
        assert repeated["last_read_at"] > initial["last_read_at"]
        assert admin.get("/api/v1/tasks/unknown").status_code == 404
        assert admin.post("/api/v1/tasks/reads", json={"task_ids": ["unknown"]}).status_code == 404
        with service.db.read() as conn:
            actions = conn.execute(select(t.audit_events.c.action)).scalars().all()
        assert not any("read" in action for action in actions)


def test_all_trial_file_kinds_and_log_slices_count_but_run_files_and_scores_do_not(
    service, version_id
):
    with running(service) as client:
        admin = setup_admin(service, client)
        reader = create_pat(admin, ())
        run_id = finished_run(admin, version_id)
        tasks = reader.get(f"/api/v1/dataset-versions/{version_id}/tasks").json()["data"]
        ids = [task["id"] for task in tasks]
        artifacts = reader.get(f"/api/v1/runs/{run_id}/artifacts").json()["data"]
        reader.get(f"/api/v1/runs/{run_id}/summary", expect=200)
        reader.get(f"/api/v1/runs/{run_id}/trials", expect=200)
        assert summaries(reader, ids) == []
        trial_file = next(a for a in artifacts if a["trial_id"])
        with service.db.read() as conn:
            task_id = conn.execute(
                select(t.trials.c.task_id).where(t.trials.c.id == trial_file["trial_id"])
            ).scalar_one()
        for kind in ("trace", "log", "trial", "verifier", "output"):
            with service.db.write() as conn:
                conn.execute(
                    update(t.artifacts)
                    .where(t.artifacts.c.id == trial_file["id"])
                    .values(kind=kind)
                )
            reader.get(f"/api/v1/artifacts/{trial_file['id']}/download", expect=200)
        reader.get(f"/api/v1/artifacts/{trial_file['id']}/download?offset=1&length=2", expect=200)
        (row,) = summaries(reader, ids)
        assert (row["task_id"], row["category"], row["count"]) == (task_id, "trial_artifacts", 6)
        # A file without trial ownership has no task attribution even when it is a log.
        with service.db.write() as conn:
            conn.execute(
                update(t.artifacts)
                .where(t.artifacts.c.id == trial_file["id"])
                .values(trial_id=None, attempt_id=None)
            )
        reader.get(f"/api/v1/artifacts/{trial_file['id']}/download", expect=200)
        assert summaries(reader, ids) == [row]


@pytest.mark.parametrize("channel", ["web_session", "pat"])
def test_archives_and_exports_record_once_per_included_task(service, version_id, channel, store):
    with running(service) as client:
        admin = setup_admin(service, client)
        reader = admin if channel == "web_session" else create_pat(admin, ())
        run_id = finished_run(admin, version_id, repetitions=2)
        tasks = reader.get(f"/api/v1/dataset-versions/{version_id}/tasks").json()["data"]
        ids = [task["id"] for task in tasks]
        trials = reader.get(f"/api/v1/runs/{run_id}/trials").json()["data"]
        archive_url = f"/api/v1/runs/{run_id}/artifacts/archive"
        reader.get(archive_url, expect=200)
        rows = summaries(reader, ids)
        assert len(rows) == 3 and all(
            row["count"] == 1 and row["channel"] == channel for row in rows
        )
        reader.get(archive_url + f"?trial_id={trials[0]['id']}", expect=200)
        reader.get(archive_url + f"?attempt_id={trials[0]['attempts'][0]['id']}", expect=200)
        chosen = next(task for task in tasks if task["task_key"] == trials[0]["task_key"])
        reader.get(archive_url + f"?task_key={chosen['task_key']}", expect=200)
        assert (
            next(row["count"] for row in summaries(reader, ids) if row["task_id"] == chosen["id"])
            == 4
        )
        export_url = make_export(reader, run_id, task_keys=[chosen["task_key"]])
        assert (
            next(row["count"] for row in summaries(reader, ids) if row["task_id"] == chosen["id"])
            == 4
        )
        reader.get(export_url, expect=200)
        assert (
            next(row["count"] for row in summaries(reader, ids) if row["task_id"] == chosen["id"])
            == 5
        )
        # A metadata-only export contains no task content.
        before = summaries(reader, ids)
        reader.get(make_export(reader, run_id, include_artifacts=False), expect=200)
        assert summaries(reader, ids) == before
        # An archive records its selected files before streaming, so a file lost from
        # storage still counts there (erring towards "read"); an export is recorded
        # after it is built and counts only what it contains.
        with service.db.read() as conn:
            keys = (
                conn.execute(
                    select(t.artifacts.c.storage_key).where(t.artifacts.c.run_id == run_id)
                )
                .scalars()
                .all()
            )
        for key in keys:
            store.files.pop(key, None)
        response = reader.get(archive_url, expect=200)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            manifest = json.loads(archive.read(archive.namelist()[-1]))
        assert not manifest["files"]
        after_archive = summaries(reader, ids)
        assert after_archive != before
        reader.get(make_export(reader, run_id), expect=200)
        assert summaries(reader, ids) == after_archive


@pytest.mark.parametrize(
    "path", ["instruction", "file", "slice", "archive", "deliverables", "export"]
)
def test_recording_failure_returns_no_content(service, version_id, monkeypatch, path):
    with running(service) as client:
        admin = setup_admin(service, client)
        reader = create_pat(admin, ())
        run_id = finished_run(admin, version_id)
        task_id = reader.get(f"/api/v1/dataset-versions/{version_id}/tasks").json()["data"][0]["id"]
        artifact = reader.get(f"/api/v1/runs/{run_id}/artifacts").json()["data"][0]
        urls = {
            "instruction": f"/api/v1/tasks/{task_id}",
            "file": f"/api/v1/artifacts/{artifact['id']}/download",
            "slice": f"/api/v1/artifacts/{artifact['id']}/download?offset=1&length=2",
            "archive": f"/api/v1/runs/{run_id}/artifacts/archive",
            "deliverables": f"/api/v1/runs/{run_id}/artifacts/archive?content=deliverables",
            "export": make_export(reader, run_id),
        }

        def fail(*args):
            raise MaestroError("READ_RECORD_FAILED", "Cannot record", 503)

        monkeypatch.setattr(task_reads, "record", fail)
        response = reader.get(urls[path])
        assert (
            response.status_code == 503 and response.json()["error"]["code"] == "READ_RECORD_FAILED"
        )
        assert summaries(reader, [task_id]) == []


@pytest.mark.parametrize("content", ["all", "deliverables"])
def test_archive_read_updates_roll_back_together_on_database_failure(service, version_id, content):
    with running(service) as client:
        admin = setup_admin(service, client)
        run_id = finished_run(admin, version_id)
        ids = sorted(
            task["id"]
            for task in admin.get(f"/api/v1/dataset-versions/{version_id}/tasks").json()["data"]
        )
        with service.db.write() as conn:
            conn.exec_driver_sql(
                f"CREATE TRIGGER refuse_read BEFORE INSERT ON task_reads WHEN NEW.task_id = '{ids[-1]}' BEGIN SELECT RAISE(ABORT, 'read failure'); END"
            )
        with pytest.raises(IntegrityError, match="read failure"):
            admin.get(f"/api/v1/runs/{run_id}/artifacts/archive?content={content}")
        assert summaries(admin, ids) == []


def test_read_summaries_outlive_web_sessions_and_revoked_tokens(service, version_id):
    from sqlalchemy import delete
    from support import PASSWORD, Api

    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        task_id = admin.get(f"/api/v1/dataset-versions/{version_id}/tasks").json()["data"][0]["id"]
        token = create_pat(admin, ())
        admin.get(f"/api/v1/tasks/{task_id}", expect=200)
        token.get(f"/api/v1/tasks/{task_id}", expect=200)
        other_http = type(client)(client.app)
        logged_in = other_http.post(
            "/api/v1/session", json={"username": "admin", "password": PASSWORD}
        )
        assert logged_in.status_code == 201, logged_in.text
        other = Api(other_http, csrf=logged_in.json()["data"]["csrf_token"])
        other.get(f"/api/v1/tasks/{task_id}", expect=200)
        rows = summaries(admin, [task_id])
        # Two logins of one account share the web row; the PAT has its own.
        assert len(rows) == 2
        [web] = [r for r in rows if r["channel"] == "web_session"]
        assert web["channel_id"] == "web" and web["count"] == 2
        with service.db.write() as conn:
            conn.execute(delete(t.sessions))
            conn.execute(
                update(t.pats)
                .where(t.pats.c.id == next(r["channel_id"] for r in rows if r["channel"] == "pat"))
                .values(revoked_at=clock.to_text(clock.now()))
            )
        assert token.get(f"/api/v1/tasks/{task_id}").status_code == 401
        assert task_reads.query(service.db, [task_id]) == rows


def test_existing_export_packages_recover_task_attribution_from_their_manifest(service, version_id):
    with running(service) as client:
        admin = setup_admin(service, client)
        reader = create_pat(admin, ())
        run_id = finished_run(admin, version_id)
        tasks = reader.get(f"/api/v1/dataset-versions/{version_id}/tasks").json()["data"]
        chosen = tasks[0]
        url = make_export(reader, run_id, task_keys=[chosen["task_key"]])
        with service.db.write() as conn:
            row = conn.execute(select(t.exports)).mappings().one()
            conn.execute(
                update(t.exports)
                .where(t.exports.c.id == row["id"])
                .values(
                    summary_json={k: v for k, v in row["summary_json"].items() if k != "task_ids"}
                )
            )
        reader.get(url, expect=200)
        (read,) = summaries(reader, [task["id"] for task in tasks])
        assert (read["task_id"], read["category"], read["count"]) == (
            chosen["id"],
            "trial_artifacts",
            1,
        )


@pytest.mark.parametrize("kind", ["fake", "hub"])
def test_task_reads_merge_cross_version_identity_without_rewriting_rows(service, source, kind):
    from support.datasets import import_and_prepare, tasks_by_key
    from support.results import purge

    service.datasets.sources[kind] = source
    service.preparer.sources[kind] = source
    source.add("n", "versions", "v1", ["same", "changed"])
    source.add(
        "n",
        "versions",
        "v2",
        ["same", "changed"],
        files={"changed": source.task_files("n", "versions", "changed", "new")},
    )
    # A separate dataset with identical bytes must never contribute reads.
    source.add(
        "n", "other", "v1", ["same"], files={"same": source.task_files("n", "versions", "same")}
    )
    with running(service) as client:
        admin = setup_admin(service, client)
        reader = create_pat(admin, ())
        first, _ = import_and_prepare(admin, "n", "versions", "v1", source_kind=kind)
        second, _ = import_and_prepare(admin, "n", "versions", "v2", source_kind=kind)
        other, _ = import_and_prepare(admin, "n", "other", "v1", source_kind=kind)
        before, after = tasks_by_key(admin, first), tasks_by_key(admin, second)
        for task in [*before.values(), *tasks_by_key(admin, other).values()]:
            reader.get(f"/api/v1/tasks/{task['id']}", expect=200)
        with service.db.read() as conn:
            stored = conn.execute(select(t.task_reads)).mappings().all()
        for key, changed in (("same", False), ("changed", True)):
            (read,) = summaries(reader, [after[key]["id"]])
            assert read["task_id"] == before[key]["id"]
            assert read["dataset_version_id"] == first and read["content_changed"] is changed
        # Querying both targets returns each original row once, preserving its identity.
        batch = summaries(reader, [before["same"]["id"], after["same"]["id"]])
        assert len(batch) == 1 and not batch[0]["content_changed"]
        entry = admin.delete(f"/api/v1/admin/dataset-versions/{first}", expect=200).json()["data"]
        assert summaries(reader, [after["same"]["id"]]) == batch
        with service.db.read() as conn:
            assert conn.execute(select(t.task_reads)).mappings().all() == stored
        assert purge(admin, entry).status_code == 200
        assert summaries(reader, [after["same"]["id"]]) == []
        assert summaries(reader, [before["same"]["id"]]) == batch
