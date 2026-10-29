"""Purges that only partly succeed, and what an account deletion must leave behind."""

import io
import json
import time
import zipfile

from fastapi.testclient import TestClient
from sqlalchemy import select, update
from support import PASSWORD, invite_member, running, setup_admin, wait_operation
from test_recycle import finished_run, purge

from maestro.db import schema as t
from maestro.services.artifacts import UNINDEXED_GRACE_SECONDS
from maestro.services.maintenance import Maintenance


def backdate(store, key: str) -> None:
    store.written[key] = time.time() - UNINDEXED_GRACE_SECONDS - 60


def bin_detail(admin, object_id: str) -> dict:
    entries = admin.get("/api/v1/admin/recycle-bin?state=purged").json()["data"]
    return next(x for x in entries if x["object_id"] == object_id)["detail"]["purged"]


# --- invitations of a deleted account ---------------------------------------------------


def test_deleting_an_account_revokes_the_invitations_it_issued(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        second = invite_member(service, admin, "second", role="admin")
        second_id = second.get("/api/v1/me").json()["data"]["id"]
        pending = [
            second.post("/api/v1/admin/invitations", json={"role": role}, expect=201).json()["data"]
            for role in ("admin", "member")
        ]
        used = invite_member(service, second, "newcomer")  # an invitation already redeemed
        mine = admin.post("/api/v1/admin/invitations", json={"role": "member"}, expect=201)
        mine = mine.json()["data"]

        entry = admin.delete(f"/api/v1/admin/members/{second_id}", expect=200).json()["data"]
        assert entry["detail"]["revoked_invitations"] == 2

        states = {
            x["id"]: x["state"] for x in admin.get("/api/v1/admin/invitations").json()["data"]
        }
        assert [states[x["id"]] for x in pending] == ["revoked", "revoked"]
        assert states[mine["id"]] == "pending"
        assert used.get("/api/v1/me", expect=200)  # the account it created is not affected
        for invitation in pending:
            fresh = TestClient(client.app)
            redeemed = fresh.post(
                "/api/v1/invitations/accept",
                json={"token": invitation["token"], "username": "mallory", "password": PASSWORD},
            )
            assert redeemed.status_code == 410
            assert redeemed.json()["error"]["code"] == "INVITATION_INVALID"
        other = TestClient(client.app).post(
            "/api/v1/invitations/accept",
            json={"token": mine["token"], "username": "legit", "password": PASSWORD},
        )
        assert other.status_code == 201

        # Restoring the account does not bring the invitations back, like its tokens.
        admin.post(f"/api/v1/admin/recycle-bin/{entry['id']}/restore", expect=200)
        states = {
            x["id"]: x["state"] for x in admin.get("/api/v1/admin/invitations").json()["data"]
        }
        assert [states[x["id"]] for x in pending] == ["revoked", "revoked"]


# --- file removal after a purge ----------------------------------------------------------


def stuck_delete(store, monkeypatch, keys: set[str]) -> None:
    real = store.delete

    def flaky(key):
        if key in keys:
            raise OSError("permission denied")
        real(key)

    monkeypatch.setattr(store, "delete", flaky)


def test_a_purge_removes_every_file_it_can_and_reports_the_rest(
    service, version_id, store, monkeypatch, caplog
):
    with running(service) as client:
        admin = setup_admin(service, client)
        run_id = finished_run(admin, version_id)
        keys = sorted(store.files)
        assert len(keys) >= 4
        stuck = {keys[0], keys[2]}
        stuck_delete(store, monkeypatch, stuck)
        entry = admin.delete(f"/api/v1/runs/{run_id}", expect=200).json()["data"]
        result = purge(admin, entry).json()["data"]
        assert result["state"] == "purged"
        assert result["detail"]["purged"]["files_failed"] == 2
        assert set(result["detail"]["purged"]["failed_keys"]) == stuck
        assert set(store.files) == stuck  # the first failure did not skip the other files
        assert bin_detail(admin, run_id)["files_failed"] == 2  # recorded in the receipt
        assert any("could not be removed" in r.getMessage() for r in caplog.records)

        # The unindexed-file sweep finishes the job, after the grace period.
        maintenance = service.maintenance
        assert maintenance.artifacts.sweep_unindexed() == {"removed": 0, "failed": 0}
        for key in stuck:
            backdate(store, key)
        assert maintenance.artifacts.sweep_unindexed() == {"removed": 0, "failed": 2}
        monkeypatch.undo()
        assert maintenance.artifacts.sweep_unindexed() == {"removed": 2, "failed": 0}
        assert not store.files


def test_a_purged_artifact_whose_file_stayed_is_swept_later(
    service, version_id, store, monkeypatch
):
    with running(service) as client:
        admin = setup_admin(service, client)
        run_id = finished_run(admin, version_id)
        artifact = admin.get(f"/api/v1/runs/{run_id}/artifacts").json()["data"][0]
        with service.db.read() as conn:
            key = conn.execute(
                select(t.artifacts.c.storage_key).where(t.artifacts.c.id == artifact["id"])
            ).scalar()
        stuck_delete(store, monkeypatch, {key})
        entry = admin.delete(f"/api/v1/artifacts/{artifact['id']}", expect=200).json()["data"]
        result = purge(admin, entry).json()["data"]
        assert result["detail"]["purged"]["files_failed"] == 1 and key in store.files
        # The row says 'deleted', so retention never looks at it again; the sweep does.
        backdate(store, key)
        monkeypatch.undo()
        assert service.artifacts.sweep_unindexed()["removed"] == 1
        assert key not in store.files


def test_the_unindexed_sweep_leaves_fresh_files_and_files_with_live_rows(
    service, version_id, store
):
    with running(service) as client:
        admin = setup_admin(service, client)
        run_id = finished_run(admin, version_id)
        indexed = set(store.files)
        # A file whose row is about to be inserted (RunSink stores the file first).
        store.put("runs/run_new/att_new/abc123", b"just written")
        # A file nothing indexes, last written long ago.
        store.put("runs/run_gone/att_old/def456", b"left over")
        backdate(store, "runs/run_gone/att_old/def456")
        for key in indexed:  # old, but a row still needs them
            backdate(store, key)
        assert service.artifacts.sweep_unindexed() == {"removed": 1, "failed": 0}
        assert set(store.files) == indexed | {"runs/run_new/att_new/abc123"}
        assert service.artifacts.for_run(run_id)

        # It also runs inside the maintenance loop, but not on every pass.
        loop = Maintenance(service.artifacts, service.exports)
        backdate(store, "runs/run_new/att_new/abc123")
        assert loop.run_once()["unindexed_files"] == {"removed": 1, "failed": 0}
        assert "unindexed_files" not in loop.run_once()


# --- preparation caches ----------------------------------------------------------------


def test_discarding_preparations_goes_on_after_one_image_fails(service, version_id, environment):
    with running(service) as client:
        admin = setup_admin(service, client)
        assert len(environment.images) == 3
        real = environment.discard
        calls = []

        async def flaky(expected, keep=frozenset()):
            calls.append(expected.content_id)
            if len(calls) == 1:
                raise RuntimeError("docker daemon went away")
            await real(expected, keep)

        environment.discard = flaky
        entry = admin.delete(f"/api/v1/admin/dataset-versions/{version_id}", expect=200)
        result = purge(admin, entry.json()["data"]).json()["data"]
        assert result["state"] == "purged"
        assert result["detail"]["purged"]["released_preparations"] == 3
        assert result["detail"]["purged"]["preparations_failed"] == 1
        assert len(calls) == 3  # none was skipped
        assert len(environment.images) == 1  # only the failing one is left


# --- what summaries say about artifacts ------------------------------------------------


def trial_counts(admin, run_id: str) -> dict[str, dict]:
    trials = admin.get(f"/api/v1/runs/{run_id}/trials", expect=200).json()["data"]
    return {x["id"]: x["artifact_counts"] for x in trials}


def test_trial_artifact_counts_follow_what_the_artifact_list_shows(service, version_id):
    """A recycled or purged artifact leaves the list; the counts must not keep it."""
    with running(service) as client:
        admin = setup_admin(service, client)
        run_id = finished_run(admin, version_id)
        listed = admin.get(f"/api/v1/runs/{run_id}/artifacts").json()["data"]
        by_trial: dict[str, list[dict]] = {}
        for item in listed:
            by_trial.setdefault(item["trial_id"], []).append(item)
        first, second, third = (by_trial[k] for k in sorted(by_trial))
        assert all(
            c == {"available": 2, "failed": 0, "expired": 0}
            for c in trial_counts(admin, run_id).values()
        )

        admin.delete(f"/api/v1/artifacts/{first[0]['id']}", expect=200)  # in the bin
        entry = admin.delete(f"/api/v1/artifacts/{second[0]['id']}", expect=200).json()["data"]
        purge(admin, entry).raise_for_status()  # purged
        with service.db.write() as conn:  # expired and reclaimed
            conn.execute(
                update(t.artifacts)
                .where(t.artifacts.c.id == third[0]["id"])
                .values(expires_at="2000-01-01T00:00:00.000000Z")
            )
        assert service.artifacts.sweep()["reclaimed"] == 1

        counts = trial_counts(admin, run_id)
        now = admin.get(f"/api/v1/runs/{run_id}/artifacts").json()["data"]
        for trial_id in by_trial:
            shown = [x for x in now if x["trial_id"] == trial_id]
            assert counts[trial_id] == {
                "available": sum(x["status"] == "available" for x in shown),
                "failed": sum(x["status"] in {"failed", "unavailable"} for x in shown),
                "expired": sum(x["status"] in {"expired", "deleting"} for x in shown),
            }
        assert counts[first[0]["trial_id"]]["available"] == 1
        assert counts[second[0]["trial_id"]]["available"] == 1
        assert counts[third[0]["trial_id"]] == {"available": 1, "failed": 0, "expired": 1}


def test_export_summary_counts_the_bytes_it_included(service, version_id):
    with running(service) as client:
        admin = setup_admin(service, client)
        run_id = finished_run(admin, version_id)
        listed = admin.get(f"/api/v1/runs/{run_id}/artifacts").json()["data"]
        # One trial file is reclaimed (listed as missing, not included) ...
        with service.db.write() as conn:
            conn.execute(
                update(t.artifacts)
                .where(t.artifacts.c.id == listed[0]["id"])
                .values(expires_at="2000-01-01T00:00:00.000000Z")
            )
        assert service.artifacts.sweep()["reclaimed"] == 1
        # ... and the run has a file of its own, which belongs to no trial.
        data = b"runner diagnostics " * 50
        digest, size = service.store.put("runs/run_x/run/diag", data)
        with service.db.write() as conn:
            conn.execute(
                t.artifacts.insert().values(
                    id="art_runlevel",
                    run_id=run_id,
                    kind="diagnostic",
                    relative_path="runner-stderr.txt",
                    storage_key="runs/run_x/run/diag",
                    digest=digest,
                    size_bytes=size,
                    status="available",
                    created_at="2026-10-23T00:00:00.000000Z",
                )
            )
        created = admin.post("/api/v1/exports", json={"run_ids": [run_id]}, expect=202).json()
        wait_operation(admin, created["data"]["operation_id"])
        package = admin.get(f"/api/v1/exports/{created['data']['id']}").json()["data"]
        assert package["state"] == "ready", package
        archive = zipfile.ZipFile(
            io.BytesIO(admin.get(f"/api/v1/exports/{package['id']}/download").content)
        )
        manifest = json.loads(archive.read(f"export-{package['id']}/manifest.json"))
        included = [f for f in manifest["files"] if "artifact_id" in f]
        assert any(f["artifact_id"] == "art_runlevel" for f in included)
        assert listed[0]["id"] not in {f["artifact_id"] for f in included}
        assert package["summary"]["artifact_bytes"] == sum(f["size_bytes"] for f in included)
        assert package["summary"]["artifact_bytes"] == sum(
            archive.getinfo(f"export-{package['id']}/{f['path']}").file_size for f in included
        )
