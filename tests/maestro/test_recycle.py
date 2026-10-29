"""07 section 8 with U-19: two-step deletion, who may do what, and what survives."""

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select, update
from support import (
    PASSWORD,
    create_pat,
    invite_member,
    running,
    setup_admin,
    submit,
    wait_operation,
)

from maestro.db import schema as t
from maestro.testing.fakes import FakeDatasetSource


def finished_run(api, version_id, **body) -> str:
    response = submit(api, version_id, **body)
    assert response.status_code == 202, response.text
    accepted = response.json()["data"]
    wait_operation(api, accepted["operation_id"])
    return accepted["run_id"]


def bin_entry(admin, object_id, state="recycled") -> dict:
    entries = admin.get(f"/api/v1/admin/recycle-bin?state={state}").json()["data"]
    return next(x for x in entries if x["object_id"] == object_id)


def purge(admin, entry, confirm=None):
    return admin.post(
        f"/api/v1/admin/recycle-bin/{entry['id']}/purge",
        json={"confirm_object_id": confirm or entry["object_id"]},
    )


def test_member_bins_own_run_admin_restores_and_purges(service, version_id, store):
    with running(service) as client:
        admin = setup_admin(service, client)
        alice = invite_member(service, admin, "alice")
        bob = invite_member(service, admin, "bob")
        run_id = finished_run(alice, version_id)
        export = admin.post("/api/v1/exports", json={"run_ids": [run_id]}, expect=202).json()
        wait_operation(admin, export["data"]["operation_id"])
        run = alice.get(f"/api/v1/runs/{run_id}").json()["data"]
        assert "delete" in run["allowed_actions"]

        assert bob.delete(f"/api/v1/runs/{run_id}").status_code == 403
        reader = create_pat(alice, "read")
        assert reader.delete(f"/api/v1/runs/{run_id}").status_code == 403
        entry = alice.delete(f"/api/v1/runs/{run_id}", expect=200).json()["data"]
        assert entry["state"] == "recycled" and entry["label"] == "baseline"
        # Idempotent: deleting again returns the same entry.
        assert (
            alice.delete(f"/api/v1/runs/{run_id}", expect=200).json()["data"]["id"] == entry["id"]
        )

        assert alice.get("/api/v1/runs").json()["data"] == []
        still = alice.get(f"/api/v1/runs/{run_id}", expect=200).json()["data"]
        assert still["deletion"]["state"] == "recycled"
        assert "delete" not in still["allowed_actions"]
        # The bin is administrators' only (U-19).
        assert alice.get("/api/v1/admin/recycle-bin").status_code == 403
        assert alice.post(f"/api/v1/admin/recycle-bin/{entry['id']}/restore").status_code == 403

        restored = admin.post(f"/api/v1/admin/recycle-bin/{entry['id']}/restore", expect=200)
        assert restored.json()["data"]["state"] == "restored"
        assert [x["id"] for x in alice.get("/api/v1/runs").json()["data"]] == [run_id]

        entry = alice.delete(f"/api/v1/runs/{run_id}", expect=200).json()["data"]
        artifact_keys = set(store.files)
        assert artifact_keys
        wrong = purge(admin, entry, confirm="nope")
        assert wrong.status_code == 422 and wrong.json()["error"]["code"] == "PURGE_NOT_CONFIRMED"
        purged = purge(admin, entry).json()["data"]
        assert purged["state"] == "purged" and purged["detail"]["purged"]["trials"] == 3
        assert not store.files  # files gone with the run
        gone = alice.get(f"/api/v1/runs/{run_id}")
        assert gone.status_code == 410 and gone.json()["error"]["code"] == "OBJECT_PURGED"
        assert alice.get(f"/api/v1/runs/{run_id}/trials").status_code == 410
        receipt = bin_entry(admin, run_id, "purged")
        assert receipt["owner_username"] == "alice" and receipt["deleted_by_username"] == "alice"
        restore = admin.post(f"/api/v1/admin/recycle-bin/{entry['id']}/restore")
        assert restore.status_code == 409 and restore.json()["error"]["code"] == "ALREADY_PURGED"
        # An export holding the purged run's data does not outlive it.
        package = admin.get(f"/api/v1/exports/{export['data']['id']}").json()["data"]
        assert package["state"] == "expired"


def test_active_runs_cannot_be_deleted(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        run_id = submit(admin, version_id).json()["data"]["run_id"]
        refused = admin.delete(f"/api/v1/runs/{run_id}")
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "RUN_ACTIVE"
        assert refused.json()["error"]["suggested_actions"][0]["type"] == "cancel_run"
        admin.post(f"/api/v1/runs/{run_id}/cancel", expect=200)
        admin.delete(f"/api/v1/runs/{run_id}", expect=200)


def test_experiment_pat_may_bin_own_run(service, version_id):
    with running(service) as client:
        admin = setup_admin(service, client)
        agent = create_pat(admin, "experiment")
        run_id = finished_run(agent, version_id)
        agent.delete(f"/api/v1/runs/{run_id}", expect=200)
        assert agent.get("/api/v1/admin/recycle-bin").status_code == 403


def test_deleted_dataset_version_keeps_runs_and_blocks_new_ones(service, version_id, source):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        alice = invite_member(service, admin, "alice")
        queued = submit(alice, version_id).json()["data"]["run_id"]
        entry = admin.delete(f"/api/v1/admin/dataset-versions/{version_id}", expect=200).json()
        entry = entry["data"]
        assert entry["label"] == "demo/arith@v1"
        assert alice.get("/api/v1/datasets").json()["data"][0]["versions"] == []
        refused = submit(alice, version_id)
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == "DATASET_VERSION_DELETED"
        detail = alice.get(f"/api/v1/dataset-versions/{version_id}").json()["data"]
        assert detail["deletion"]["state"] == "recycled"
        run = alice.get(f"/api/v1/runs/{queued}").json()["data"]
        assert run["dataset_version_deletion"]["state"] == "recycled"
        # A queued run still needs the tasks: no purge until it ends.
        busy = purge(admin, entry)
        assert busy.status_code == 409 and busy.json()["error"]["code"] == "DATASET_VERSION_IN_USE"
        alice.post(f"/api/v1/runs/{queued}/cancel", expect=200)
        purged = purge(admin, entry).json()["data"]
        assert purged["detail"]["purged"]["tasks"] == 3
        run = alice.get(f"/api/v1/runs/{queued}").json()["data"]
        assert run["dataset_version_deletion"]["state"] == "purged"
        assert run["snapshot"]["effective"]["dataset"]["name"] == "arith"
        trials = alice.get(f"/api/v1/runs/{queued}/trials").json()["data"]
        assert {x["task_key"] for x in trials} == {"add-01", "add-02", "mul-01"}
        with service.db.read() as conn:
            assert conn.execute(select(t.task_admissions)).first() is None


def test_dataset_deletion_hides_versions_and_guards_restore_order(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        dataset_id = admin.get("/api/v1/datasets").json()["data"][0]["id"]
        version_entry = admin.delete(f"/api/v1/admin/dataset-versions/{version_id}").json()["data"]
        dataset_entry = admin.delete(f"/api/v1/admin/datasets/{dataset_id}").json()["data"]
        assert admin.get("/api/v1/datasets").json()["data"] == []
        assert submit(admin, version_id).json()["error"]["code"] == "DATASET_VERSION_DELETED"
        blocked = admin.post(f"/api/v1/admin/recycle-bin/{version_entry['id']}/restore")
        assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "PARENT_DELETED"
        admin.post(f"/api/v1/admin/recycle-bin/{dataset_entry['id']}/restore", expect=200)
        admin.post(f"/api/v1/admin/recycle-bin/{version_entry['id']}/restore", expect=200)
        assert submit(admin, version_id).status_code == 202
        admin.post(f"/api/v1/runs/{submit(admin, version_id).json()['data']['run_id']}/cancel")


def test_credentials_in_use_disable_first_and_purge_frees_the_name(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        body = {"name": "openai", "kind": "model_api_key", "provider": "openai", "value": "sk-1"}
        cred = admin.post("/api/v1/admin/credentials", json=body, expect=201).json()["data"]
        run_id = submit(admin, version_id).json()["data"]["run_id"]
        with service.db.write() as conn:  # M5 binds credentials through the snapshot
            conn.execute(
                update(t.run_snapshots)
                .where(t.run_snapshots.c.run_id == run_id)
                .values(provenance_json={"credential_ids": [cred["id"]]})
            )
        busy = admin.delete(f"/api/v1/admin/credentials/{cred['id']}")
        assert busy.status_code == 409 and busy.json()["error"]["code"] == "CREDENTIAL_IN_USE"
        admin.post(
            f"/api/v1/admin/credentials/{cred['id']}/disable",
            json={"expected_revision": cred["revision"]},
            expect=200,
        )
        admin.post(f"/api/v1/runs/{run_id}/cancel", expect=200)
        entry = admin.delete(f"/api/v1/admin/credentials/{cred['id']}", expect=200).json()["data"]
        assert admin.get("/api/v1/admin/credentials").json()["data"] == []
        enable = admin.post(
            f"/api/v1/admin/credentials/{cred['id']}/enable", json={"expected_revision": 2}
        )
        assert enable.status_code == 409 and enable.json()["error"]["code"] == "CREDENTIAL_DELETED"
        taken = admin.post("/api/v1/admin/credentials", json=body)
        assert taken.status_code == 409
        purge(admin, entry).raise_for_status()
        with service.db.read() as conn:
            row = conn.execute(select(t.credentials).where(t.credentials.c.id == cred["id"])).one()
        assert row.encrypted_payload == b"" and row.name != "openai"
        admin.post("/api/v1/admin/credentials", json=body, expect=201)
        # Rotation skips the purged row and still re-encrypts the live one.
        status = admin.get("/api/v1/admin/master-key").json()["data"]
        rotated = admin.post(
            "/api/v1/admin/master-key/rotations",
            json={"expected_generation": status["generation"]},
            expect=200,
        ).json()["data"]
        assert rotated["generation"] == status["generation"] + 1


def test_account_deletion_rules_and_history(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        me = admin.get("/api/v1/me").json()["data"]
        self_delete = admin.delete(f"/api/v1/admin/members/{me['id']}")
        assert self_delete.json()["error"]["code"] == "CANNOT_DELETE_SELF"
        alice = invite_member(service, admin, "alice")
        alice_id = alice.get("/api/v1/me").json()["data"]["id"]
        token = create_pat(alice, "experiment")
        run_id = submit(alice, version_id).json()["data"]["run_id"]
        busy = admin.delete(f"/api/v1/admin/members/{alice_id}")
        assert busy.json()["error"]["code"] == "ACCOUNT_HAS_ACTIVE_RUNS"
        admin.post(f"/api/v1/runs/{run_id}/cancel", expect=200)
        entry = admin.delete(f"/api/v1/admin/members/{alice_id}", expect=200).json()["data"]
        # Session and PAT stop at once.
        assert alice.get("/api/v1/me").status_code == 401
        assert token.get("/api/v1/me").status_code == 401
        names = [x["username"] for x in admin.get("/api/v1/admin/members").json()["data"]]
        assert "alice" not in names
        run = admin.get(f"/api/v1/runs/{run_id}").json()["data"]
        assert run["owner"] == {"username": "alice", "display_name": "alice", "deleted": True}
        enable = admin.post(
            f"/api/v1/admin/members/{alice_id}/enable", json={"expected_revision": 3}
        )
        assert enable.json()["error"]["code"] == "USER_DELETED"

        admin.post(f"/api/v1/admin/recycle-bin/{entry['id']}/restore", expect=200)
        fresh = TestClient(client.app)  # keep the admin's cookie jar
        login = fresh.post("/api/v1/session", json={"username": "alice", "password": PASSWORD})
        assert login.status_code == 201
        assert token.get("/api/v1/me").json()["error"]["code"] == "PAT_REVOKED"

        entry = admin.delete(f"/api/v1/admin/members/{alice_id}", expect=200).json()["data"]
        purge(admin, entry).raise_for_status()
        login = fresh.post("/api/v1/session", json={"username": "alice", "password": PASSWORD})
        assert login.status_code == 401
        # The name stays reserved: history never points at a different person.
        issued = admin.post("/api/v1/admin/invitations", json={}).json()["data"]
        taken = fresh.post(
            "/api/v1/invitations/accept",
            json={"token": issued["token"], "username": "alice", "password": PASSWORD},
        )
        assert taken.status_code == 409
        assert admin.get(f"/api/v1/runs/{run_id}").json()["data"]["owner"]["deleted"] is True


def test_only_admins_delete_artifacts_and_other_peoples_pats(service, version_id):
    with running(service) as client:
        admin = setup_admin(service, client)
        alice = invite_member(service, admin, "alice")
        run_id = finished_run(alice, version_id)
        artifact = alice.get(f"/api/v1/runs/{run_id}/artifacts").json()["data"][0]
        assert alice.delete(f"/api/v1/artifacts/{artifact['id']}").status_code == 403
        entry = admin.delete(f"/api/v1/artifacts/{artifact['id']}", expect=200).json()["data"]
        listed = alice.get(f"/api/v1/runs/{run_id}/artifacts").json()["data"]
        assert artifact["id"] not in {a["id"] for a in listed}
        blocked = alice.get(f"/api/v1/artifacts/{artifact['id']}/download")
        assert blocked.status_code == 410 and blocked.json()["error"]["code"] == "ARTIFACT_DELETED"
        purge(admin, entry).raise_for_status()
        with service.db.read() as conn:
            status = conn.execute(
                select(t.artifacts.c.status).where(t.artifacts.c.id == artifact["id"])
            ).scalar()
        assert status == "deleted"

        token = create_pat(alice, "read")
        pat_id = alice.get("/api/v1/me/pats").json()["data"][0]["id"]
        bob = invite_member(service, admin, "bob")
        assert bob.delete(f"/api/v1/admin/pats/{pat_id}").status_code == 403
        assert token.delete(f"/api/v1/me/pats/{pat_id}").status_code == 403
        admin.delete(f"/api/v1/admin/pats/{pat_id}", expect=200)
        assert token.get("/api/v1/me").json()["error"]["code"] == "PAT_REVOKED"
        assert alice.get("/api/v1/me/pats").json()["data"] == []
        assert bin_entry(admin, pat_id)["owner_username"] == "alice"


def test_unknown_object_and_bin_pagination(service, version_id):
    with running(service) as client:
        admin = setup_admin(service, client)
        assert admin.delete(f"/api/v1/runs/run_{uuid.uuid4().hex[:26]}").status_code == 404
        runs = [finished_run(admin, version_id) for _ in range(3)]
        for run_id in runs:
            admin.delete(f"/api/v1/runs/{run_id}", expect=200)
        first = admin.get("/api/v1/admin/recycle-bin?limit=2&object_type=run").json()
        assert len(first["data"]) == 2 and first["meta"]["next_cursor"]
        rest = admin.get(
            f"/api/v1/admin/recycle-bin?limit=2&object_type=run&cursor={first['meta']['next_cursor']}"
        ).json()["data"]
        assert {x["object_id"] for x in first["data"] + rest} == set(runs)


def test_purging_a_dataset_purges_its_versions_with_receipts(service, version_id):
    with running(service) as client:
        admin = setup_admin(service, client)
        run_id = finished_run(admin, version_id)
        artifact = admin.get(f"/api/v1/runs/{run_id}/artifacts").json()["data"][0]
        dataset_id = admin.get("/api/v1/datasets").json()["data"][0]["id"]
        entry = admin.delete(f"/api/v1/admin/datasets/{dataset_id}", expect=200).json()["data"]
        purged = purge(admin, entry).json()["data"]
        assert purged["detail"]["purged"]["versions"] == 1
        receipt = bin_entry(admin, version_id, "purged")
        assert receipt["label"] == "demo/arith@v1" and receipt["purged_at"]
        detail = admin.get(f"/api/v1/dataset-versions/{version_id}").json()["data"]
        assert detail["deletion"]["state"] == "purged"
        # The run, its scores and files survive the dataset (U-08).
        run = admin.get(f"/api/v1/runs/{run_id}").json()["data"]
        assert run["dataset_version_deletion"]["state"] == "purged"
        assert run["scoring"]["mean"] == 1.0
        admin.get(f"/api/v1/artifacts/{artifact['id']}/download", expect=200)


def test_files_of_a_binned_run_stay_readable_until_purged(service, version_id):
    with running(service) as client:
        admin = setup_admin(service, client)
        run_id = finished_run(admin, version_id)
        artifact = admin.get(f"/api/v1/runs/{run_id}/artifacts").json()["data"][0]
        admin.delete(f"/api/v1/runs/{run_id}", expect=200)
        admin.get(f"/api/v1/artifacts/{artifact['id']}/download", expect=200)
        assert admin.get(f"/api/v1/runs/{run_id}/summary", expect=200).json()["data"]


def test_purging_a_version_reclaims_caches_except_shared_content(
    service, source, environment, settings
):
    """U-19: purge takes the version's preparation caches, local images and registry
    copies, and its task materials, but keeps what a remaining version still uses."""
    from test_datasets import import_and_prepare, tasks_by_key

    shared = FakeDatasetSource.task_files("demo", "pair", "shared")
    source.add("demo", "pair", "v1", ["shared", "only-v1"], files={"shared": shared})
    source.add("demo", "pair", "v2", ["shared"], files={"shared": shared})
    with running(service) as client:
        admin = setup_admin(service, client)
        v1, _ = import_and_prepare(admin, name="pair", ref="v1")
        v2, _ = import_and_prepare(admin, name="pair", ref="v2")
        old = tasks_by_key(admin, v1)
        specs = {k: x["spec_digest"] for k, x in old.items()}
        assert all(environment.present(spec) for spec in specs.values())
        only_v1 = {i.digest for i in environment.outputs[specs["only-v1"]].images}
        entry = admin.delete(f"/api/v1/admin/dataset-versions/{v1}", expect=200).json()["data"]
        purged = purge(admin, entry).json()["data"]
        assert purged["detail"]["purged"]["released_preparations"] == 1
        assert not only_v1 & (environment.images | environment.registry)
        assert environment.present(specs["shared"])  # v2 still uses the same spec
        only = old["only-v1"]["content"]["digest"][7:]
        assert not (settings.cache_dir / "tasks" / only).exists()
        assert (settings.cache_dir / "tasks" / old["shared"]["content"]["digest"][7:]).is_dir()
        (kept,) = tasks_by_key(admin, v2).values()
        assert kept["local_preparation"]["cache_state"] == "ready" and kept["can_submit"]


def test_imports_respect_the_recycle_bin(service, source):
    """A recycled version or dataset is restored, not imported again; after a purge
    the same content can be added as a new version (M6 hand-over to M4)."""
    from test_datasets import import_and_prepare, import_dataset, tasks_by_key

    with running(service) as client:
        admin = setup_admin(service, client)
        first, _ = import_and_prepare(admin)
        entry = admin.delete(f"/api/v1/admin/dataset-versions/{first}", expect=200).json()["data"]
        again = import_dataset(admin)
        assert again["status"] == "failed"
        assert again["error"]["code"] == "DATASET_VERSION_RECYCLED"
        purge(admin, entry).raise_for_status()
        second, prepared = import_and_prepare(admin)
        assert second != first and prepared["status"] == "succeeded"
        assert all(x["can_submit"] for x in tasks_by_key(admin, second).values())

        (dataset,) = admin.get("/api/v1/datasets").json()["data"]
        entry = admin.delete(f"/api/v1/admin/datasets/{dataset['id']}", expect=200).json()["data"]
        assert import_dataset(admin)["error"]["code"] == "DATASET_RECYCLED"
        purge(admin, entry).raise_for_status()
        third, _ = import_and_prepare(admin)
        (fresh,) = admin.get("/api/v1/datasets").json()["data"]
        assert fresh["id"] != dataset["id"] and fresh["versions"][0]["id"] == third


def test_admin_confirms_the_scoring_contract_for_new_runs(service, version_id):
    with running(service) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "member")
        before = finished_run(member, version_id)
        detail = member.get(f"/api/v1/dataset-versions/{version_id}").json()["data"]
        assert detail["scoring_contract"]["source"] == "platform_default"
        body = {"direction": "higher_is_better", "pass_threshold": 0.5, "name": "Accuracy"}
        url = f"/api/v1/admin/dataset-versions/{version_id}/scoring-contract"
        assert member.put(url, json=body).status_code == 403
        bad = admin.put(url, json={"direction": "higher_is_better", "pass_threshold": None})
        assert bad.status_code == 200  # no pass rate is a valid choice
        confirmed = admin.put(url, json=body, expect=200).json()["data"]
        assert confirmed["source"] == "dataset_version"
        assert confirmed["passed"] == {"rule": "threshold", "threshold": 0.5}
        after = finished_run(member, version_id)
        policy = lambda run_id: member.get(f"/api/v1/runs/{run_id}").json()["data"]["snapshot"][
            "policy"
        ]["scoring"]
        assert policy(before)["source"] == "platform_default"
        assert policy(after)["source"] == "dataset_version" and policy(after)["name"] == "Accuracy"
