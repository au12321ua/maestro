"""Results with the fake executor: query runs, read summaries, export and verify.

An agent with only a read PAT filters runs, reads their scoring, exports them and
checks the package: a real zero, a missing score and an execution failure stay
distinguishable; expired and unsaved files are listed with reasons; no secret
leaves the service.
"""

import hashlib
import io
import json
import time
import uuid
import zipfile

from sqlalchemy import select, update
from support import PASSWORD, create_pat, invite_member, running, setup_admin, wait_operation

from maestro.boundaries.executor import AttemptOutcome
from maestro.core import clock
from maestro.db import schema as t
from maestro.domain import limits
from maestro.domain.states import Role
from maestro.services.principal import Channel, Principal
from maestro.testing.fakes import failed, scored


def run_to_end(api, version_id, **body) -> str:
    accepted = api.post(
        "/api/v1/runs",
        json={"name": "r", "dataset_version_id": version_id, "execution_mode": "oracle", **body},
        headers={"Idempotency-Key": uuid.uuid4().hex},
        expect=202,
    ).json()["data"]
    wait_operation(api, accepted["operation_id"])
    return accepted["run_id"]


def export(api, **body) -> dict:
    created = api.post("/api/v1/exports", json=body, expect=202).json()["data"]
    assert created["state"] == "queued"
    wait_operation(api, created["operation_id"])
    return api.get(f"/api/v1/exports/{created['id']}", expect=200).json()["data"]


def test_query_summarize_and_export_runs_with_a_read_pat(service, version_id, executor, store):
    executor.script = {
        "add-01": [scored(0.0)],
        "add-02": [AttemptOutcome(raw_score=None, reward_valid=True, verifier_status="missing")],
        "mul-01": [failed("sandbox_failed")],
    }
    with running(service) as client:
        admin = setup_admin(service, client)
        alice = invite_member(service, admin, "alice")
        agent = create_pat(alice, ("experiment",))
        start = clock.to_text(clock.now())
        mixed = run_to_end(agent, version_id)
        executor.script = {}
        clean = run_to_end(agent, version_id)
        store.fail_put = True
        unsaved = run_to_end(
            agent,
            version_id,
            task_selection={"mode": "explicit", "task_ids": [first_task(agent, version_id)]},
        )
        store.fail_put = False

        # One file of the clean run reaches its expiry and is reclaimed.
        artifacts = agent.get(f"/api/v1/runs/{clean}/artifacts").json()["data"]
        stale = next(a for a in artifacts if a["kind"] == "output")
        due = clock.to_text(clock.now() - clock.timedelta(minutes=1))
        with service.db.write() as conn:
            conn.execute(
                update(t.artifacts).where(t.artifacts.c.id == stale["id"]).values(expires_at=due)
            )
        assert service.artifacts.sweep()["reclaimed"] == 1

        reader = create_pat(admin, ())
        listed = reader.get(
            f"/api/v1/runs?dataset_version_id={version_id}&accepted_after={start}"
        ).json()["data"]
        assert {x["id"] for x in listed} == {mixed, clean, unsaved}
        # Fewer fractional digits mean the same instant (compared as times, not text).
        short = start[:-8] + "Z" if start.endswith("000000Z") else None
        if short:
            again = reader.get(f"/api/v1/runs?accepted_after={short}").json()["data"]
            assert {x["id"] for x in again} == {mixed, clean, unsaved}
        future = reader.get("/api/v1/runs?accepted_after=2099-01-01T00:00:00.0Z").json()
        assert future["data"] == []
        by_id = {x["id"]: x for x in listed}
        scoring = by_id[mixed]["scoring"]
        assert scoring["scored_trials"] == 1 and scoring["mean"] == 0.0
        assert scoring["excluded_error_trials"] == 2 and scoring["scored_coverage"] == 1 / 3
        assert by_id[clean]["scoring"]["mean"] == 1.0
        assert by_id[clean]["scoring"]["pass_rate"] == 1.0

        summary = reader.get(f"/api/v1/runs/{mixed}/summary", expect=200).json()["data"]
        assert summary["excluded_by_cause"] == {"sandbox_failed": 1, "verifier_failed": 1}
        assert summary["contract"]["source"] == "platform_default"
        assert summary["bench_task_coverage"] == 1.0

        package = export(reader, run_ids=[mixed, clean, unsaved])
        assert package["state"] == "ready", package
        assert package["summary"]["missing"] >= 2
        response = reader.get(f"/api/v1/exports/{package['id']}/download", expect=200)
        data = response.content
        assert "sha256:" + hashlib.sha256(data).hexdigest() == package["digest"]
        archive = zipfile.ZipFile(io.BytesIO(data))
        root = f"export-{package['id']}"
        manifest = json.loads(archive.read(f"{root}/manifest.json"))

        # Every listed file is in the package with the stated digest.
        for entry in manifest["files"]:
            content = archive.read(f"{root}/{entry['path']}")
            assert "sha256:" + hashlib.sha256(content).hexdigest() == entry["digest"]
        assert [r["run_id"] for r in manifest["runs"]] == [mixed, clean, unsaved]
        run_entry = manifest["runs"][0]
        config = json.loads(archive.read(f"{root}/{run_entry['path']}/config.json"))
        assert config["snapshot"]["config_digest"] == run_entry["config_digest"]
        exported_summary = json.loads(archive.read(f"{root}/{run_entry['path']}/summary.json"))
        assert exported_summary == summary  # same computation as the API

        # Zero, missing and failed stay distinguishable in each trial's result.json.
        trials = {}
        for name in archive.namelist():
            if name.endswith("result.json") and f"/{mixed}/" in name:
                trial = json.loads(archive.read(name))
                trials[trial["task_key"]] = trial
        assert trials["add-01"]["state"] == "scored" and trials["add-01"]["result"]["score"] == 0.0
        assert trials["add-02"]["state"] == "execution_error"
        assert trials["add-02"]["result"]["score"] is None
        assert trials["add-02"]["reason_code"] == "VERIFIER_FAILED"
        assert trials["mul-01"]["result"]["failure_cause"] == "sandbox_failed"

        reasons = {(m["artifact_id"], m["reason"]) for m in manifest["missing"]}
        assert (stale["id"], "EXPIRED") in reasons
        expired = next(m for m in manifest["missing"] if m["artifact_id"] == stale["id"])
        assert expired["expired_at"] == due  # the manifest says when the file expired
        assert any(
            m["run_id"] == unsaved and m["reason"] == "SAVE_FAILED" for m in manifest["missing"]
        )

        # The ZIP is compressed, so look inside every member (the manifest included), not at
        # the raw bytes; the run id proves the search reads real content.
        members = {name: archive.read(name) for name in archive.namelist()}
        assert any(mixed.encode() in content for content in members.values())
        secrets = (
            agent.token,
            reader.token,
            admin.csrf,
            PASSWORD,
            "password_hash",
            "encrypted_payload",
            "token_hash",
        )
        for secret in secrets:
            assert not [n for n, content in members.items() if secret.encode() in content], secret

        # Exports are personal: another member cannot read this one.
        hidden = alice.get(f"/api/v1/exports/{package['id']}")
        assert hidden.status_code == 404
        assert alice.get("/api/v1/exports").json()["data"] == []


def first_task(api, version_id) -> str:
    return api.get(f"/api/v1/dataset-versions/{version_id}/tasks").json()["data"][0]["id"]


def test_export_without_artifacts_and_unknown_runs(service, version_id):
    with running(service) as client:
        admin = setup_admin(service, client)
        run_id = run_to_end(admin, version_id)
        bad = admin.post("/api/v1/exports", json={"run_ids": [run_id, "run_missing"]})
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "EXPORT_RUNS_UNAVAILABLE"
        package = export(admin, run_ids=[run_id], include_artifacts=False)
        archive = zipfile.ZipFile(
            io.BytesIO(admin.get(f"/api/v1/exports/{package['id']}/download").content)
        )
        assert not any("/artifacts/" in name for name in archive.namelist())
        manifest = json.loads(archive.read(f"export-{package['id']}/manifest.json"))
        assert manifest["missing"] == [] and len(manifest["runs"]) == 1


def test_export_fails_rather_than_holding_pins_too_long(service, version_id, monkeypatch):
    with running(service) as client:
        admin = setup_admin(service, client)
        run_id = run_to_end(admin, version_id)
        monkeypatch.setattr(limits, "EXPORT_PIN_MINUTES", 0)
        package = export(admin, run_ids=[run_id])
        assert package["state"] == "failed"
        assert package["error"]["code"] == "EXPORT_PIN_EXPIRED"
        with service.db.read() as conn:
            assert conn.execute(t.artifact_pins.select()).first() is None
        monkeypatch.setattr(limits, "EXPORT_PIN_MINUTES", 60)
        monkeypatch.setattr(service.exports, "max_bytes", 1)
        too_big = export(admin, run_ids=[run_id])
        assert too_big["error"]["code"] == "EXPORT_TOO_LARGE"


def test_export_packages_expire(service, version_id):
    with running(service) as client:
        admin = setup_admin(service, client)
        run_id = run_to_end(admin, version_id)
        package = export(admin, run_ids=[run_id])
        path = service.settings.exports_dir / package["id"]
        assert path.is_dir()
        with service.db.write() as conn:
            conn.execute(
                update(t.exports)
                .where(t.exports.c.id == package["id"])
                .values(expires_at=clock.to_text(clock.now()))
            )
        time.sleep(0.01)
        assert service.exports.expire() == 1
        assert not path.exists()
        gone = admin.get(f"/api/v1/exports/{package['id']}/download")
        assert gone.status_code == 410 and gone.json()["error"]["code"] == "EXPORT_EXPIRED"


def test_purge_during_a_build_withholds_the_package(service, version_id):
    """A purge cannot expire a package still being written; the build checks at the end."""
    with running(service) as client:
        admin = setup_admin(service, client)
        run_id = run_to_end(admin, version_id)
        artifact = admin.get(f"/api/v1/runs/{run_id}/artifacts").json()["data"][0]
        me = admin.get("/api/v1/me").json()["data"]
    from maestro.domain.states import Role
    from maestro.services.principal import Channel, Principal

    who = Principal(me["id"], "admin", Role.ADMIN, Channel.WEB_SESSION, session_id="s")
    created = service.exports.create(who, {"run_ids": [run_id]})
    write = service.exports._write

    def purge_meanwhile(*args):
        result = write(*args)
        entry = service.recycle.delete(who, "artifact", artifact["id"])
        service.recycle.purge(who, entry["id"])
        return result

    service.exports._write = purge_meanwhile
    service.exports.build(created["id"])
    package = service.exports.get(who, created["id"])
    assert package["state"] == "failed"
    assert package["error"]["code"] == "EXPORT_CONTENT_PURGED"
    assert not (service.settings.exports_dir / created["id"]).exists()


def admin_principal(admin) -> Principal:
    me = admin.get("/api/v1/me").json()["data"]
    return Principal(me["id"], "admin", Role.ADMIN, Channel.WEB_SESSION, session_id="s")


def expire_now(service, artifact_ids) -> None:
    with service.db.write() as conn:
        conn.execute(
            update(t.artifacts)
            .where(t.artifacts.c.id.in_(artifact_ids))
            .values(expires_at=clock.to_text(clock.now() - clock.timedelta(minutes=1)))
        )


def test_a_sweep_during_a_build_cannot_take_the_files_being_packed(service, version_id):
    """The build takes its read pins itself; retention that runs while it packs spares those
    files, reclaims everything else, and gets the pinned ones once the export is done."""
    with running(service) as client:
        admin = setup_admin(service, client)
        packed = run_to_end(admin, version_id)
        elsewhere = run_to_end(admin, version_id)
        who = admin_principal(admin)
    ids = {r: [a["id"] for a in service.artifacts.for_run(r)] for r in (packed, elsewhere)}
    expire_now(service, ids[packed] + ids[elsewhere])
    created = service.exports.create(who, {"run_ids": [packed]})
    write = service.exports._write
    seen = {}

    def sweep_meanwhile(*args):
        with service.db.read() as conn:
            seen["pins"] = {
                r.artifact_id
                for r in conn.execute(
                    select(t.artifact_pins).where(t.artifact_pins.c.holder_id == created["id"])
                )
            }
        seen["swept"] = service.artifacts.sweep()
        return write(*args)

    service.exports._write = sweep_meanwhile
    service.exports.build(created["id"])

    assert seen["pins"] == set(ids[packed])  # taken by the build, not by the test
    assert seen["swept"]["reclaimed"] == len(ids[elsewhere])
    package = service.exports.get(who, created["id"])
    assert package["state"] == "ready", package
    _, path = service.exports.archive(who, created["id"])
    archive = zipfile.ZipFile(path)
    manifest = json.loads(archive.read(f"export-{created['id']}/manifest.json"))
    assert manifest["missing"] == []
    assert {f["artifact_id"] for f in manifest["files"] if "artifact_id" in f} == set(ids[packed])
    with service.db.read() as conn:
        assert conn.execute(select(t.artifact_pins)).first() is None  # released at the end
    assert service.artifacts.sweep()["reclaimed"] == len(ids[packed])


def test_a_purge_in_the_middle_of_packing_fails_the_export_and_leaves_nothing(service, version_id):
    with running(service) as client:
        admin = setup_admin(service, client)
        run_id = run_to_end(admin, version_id)
        who = admin_principal(admin)
    created = service.exports.create(who, {"run_ids": [run_id]})
    add = service.exports._add_artifact
    packed = []

    def purge_the_second(archive, root, directory, item, *rest):
        packed.append(item["id"])
        if len(packed) == 2:
            entry = service.recycle.delete(who, "artifact", item["id"])
            service.recycle.purge(who, entry["id"])
        return add(archive, root, directory, item, *rest)

    service.exports._add_artifact = purge_the_second
    service.exports.build(created["id"])

    package = service.exports.get(who, created["id"])
    assert package["state"] == "failed" and package["error"]["code"] == "EXPORT_CONTENT_PURGED"
    assert not (service.settings.exports_dir / created["id"]).exists()
    assert service.operations.get(created["operation_id"])["status"] == "failed"
    with service.db.read() as conn:
        assert conn.execute(select(t.artifact_pins)).first() is None
    assert len(packed) > 2  # the build went on to the end; the check came afterwards
