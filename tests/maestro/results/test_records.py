"""Workflow content stays opaque while identity, evidence and edit history are checked."""

import json
from dataclasses import replace

import pytest
from maestro_sdk import ApiError
from sqlalchemy import select, update
from support import create_pat, invite_member, running, setup_admin
from support.attachments import attach
from support.configuration import agent_body, prepare_model
from support.results import finished_run, purge, run
from support.sdk import sdk

from maestro.core.errors import MaestroError
from maestro.db import schema as t
from maestro.domain import limits
from maestro.domain.states import Role


def record_body(run_id, **changes):
    return {
        "target": {"type": "run", "id": run_id},
        "kind": "attachment-candidate",
        "title": "Candidate",
        "body": {"arbitrary": [None, False, 0, {"x": "value"}]},
        **changes,
    }


def test_service_keeps_opaque_content_and_every_attributed_version(service, owner, version_id):
    run_id = run(service, owner, version_id)
    body = record_body(run_id, excerpts=[{"text": "api_key=private-value\nexit=1"}])
    first = service.records.create(owner, body)
    assert first["revision"] == 1 and first["body"] == body["body"]
    assert first["excerpts"] == [{"text": "api_key=[REDACTED]\nexit=1", "reference": None}]
    changed = service.records.update(
        owner, first["id"], {"expected_revision": 1, "body": {"free": "shape"}}
    )
    assert changed["revision"] == 2 and changed["created_at"] == first["created_at"]
    versions = service.records.history(owner, first["id"])
    assert [v["revision"] for v in versions] == [1, 2]
    assert [v["body"] for v in versions] == [first["body"], changed["body"]]
    assert all(v["actor_id"] == owner.user_id for v in versions)
    with pytest.raises(MaestroError) as stale:
        service.records.update(owner, first["id"], {"expected_revision": 1, "title": "lost update"})
    assert stale.value.status == 409
    assert len(service.records.history(owner, first["id"])) == 2
    with service.db.read() as conn:
        stored = conn.execute(select(t.workflow_record_revisions)).mappings().all()
        assert "private-value" not in str(stored)
    other_admin = replace(owner, user_id="usr_someone_else", role=Role.ADMIN)
    with pytest.raises(MaestroError) as forbidden:
        service.records.update(
            other_admin, first["id"], {"expected_revision": 2, "title": "rewrite"}
        )
    assert forbidden.value.status == 403


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "Attachment-Candidate"},
        {"kind": "has_underscore"},
        {"kind": "x" * (limits.RECORD_KIND_MAX + 1)},
        {"title": " "},
        {"title": "x" * (limits.RECORD_TITLE_MAX + 1)},
        {"body": []},
        {"body": {"x": "x" * limits.RECORD_BODY_MAX_BYTES}},
        {"excerpts": [{"text": "é" * (limits.RECORD_EXCERPT_MAX_BYTES // 2 + 1)}]},
        {"excerpts": [{"text": "x" * limits.RECORD_EXCERPT_MAX_BYTES}] * 5},
        {"excerpts": [{"text": "x"}] * (limits.RECORD_MAX_EXCERPTS + 1)},
        {"references": [{"type": "run", "id": "absent"}] * (limits.RECORD_MAX_REFERENCES + 1)},
        {"excerpts": [{"text": "hi", "reference": {"type": "run", "id": "absent"}}]},
    ],
)
def test_record_limits_are_enforced_before_any_version_is_committed(
    service, owner, version_id, change
):
    run_id = run(service, owner, version_id)
    first = service.records.create(owner, record_body(run_id))
    for operation in (
        lambda: service.records.create(owner, record_body(run_id, **change)),
        lambda: service.records.update(owner, first["id"], {"expected_revision": 1, **change}),
    ):
        with pytest.raises(MaestroError) as invalid:
            operation()
        assert invalid.value.code == "RECORD_INVALID"
    assert service.records.get(owner, first["id"])["revision"] == 1
    assert len(service.records.history(owner, first["id"])) == 1
    assert len(service.records.list(owner, {"type": "run", "id": run_id})) == 1


def test_record_limits_count_compact_utf8_and_redacted_storage(service, owner, version_id):
    run_id = run(service, owner, version_id)
    body = {
        "x": "x"
        * (limits.RECORD_BODY_MAX_BYTES - len(json.dumps({"x": ""}, separators=(",", ":"))))
    }
    texts = [{"text": "é" * (limits.RECORD_EXCERPT_MAX_BYTES // 2)}] * 4
    first = service.records.create(owner, record_body(run_id, body=body, excerpts=texts))
    assert first["body"] == body and first["excerpts"][0]["text"] == texts[0]["text"]
    # Replacing very short secrets can make the stored text larger than the input.
    with pytest.raises(MaestroError):
        service.records.create(owner, record_body(run_id, excerpts=[{"text": "api_key=a " * 400}]))


def test_api_checks_all_reference_types_and_retains_excerpts_after_expiry(service, version_id):
    with running(service) as client:
        admin = setup_admin(service, client)
        model, harness = prepare_model(admin)
        attachment = attach(admin)
        run_id = finished_run(
            admin, version_id, **agent_body(model, harness), attachments=[attachment]
        )
        collection = admin.post(
            "/api/v1/collections",
            json={
                "name": "c",
                "scenario": "attachments",
                "base": {"run_id": run_id},
                "run_ids": [run_id],
            },
            expect=201,
        ).json()["data"]
        snapshot = admin.post(
            "/api/v1/analysis-snapshots",
            json={"name": "s", "collection_id": collection["id"]},
            expect=201,
        ).json()["data"]
        trial = admin.get(f"/api/v1/runs/{run_id}/trials").json()["data"][0]
        artifact = admin.get(f"/api/v1/runs/{run_id}/artifacts").json()["data"][0]
        refs = [
            {"type": kind, "id": identity}
            for kind, identity in (
                ("run", run_id),
                ("trial", trial["id"]),
                ("artifact", artifact["id"]),
                ("attachment_snapshot", attachment),
                ("collection", collection["id"]),
                ("analysis_snapshot", snapshot["id"]),
            )
        ]
        pat = create_pat(admin)
        payload = record_body(
            run_id,
            references=refs,
            excerpts=[{"text": "Authorization: Bearer private-value", "reference": refs[2]}],
        )
        record = pat.post("/api/v1/records", json=payload, expect=201).json()["data"]
        assert (
            pat.post("/api/v1/records", json={**payload, "author_id": "forged"}).status_code == 422
        )
        assert (
            pat.put(
                f"/api/v1/records/{record['id']}",
                json={
                    "expected_revision": 1,
                    "target": {"type": "collection", "id": collection["id"]},
                },
            ).status_code
            == 422
        )
        assert record["pat_id"] and record["references"] == refs
        assert record["excerpts"][0]["text"] == "Authorization: [REDACTED]"
        reader = create_pat(admin, ())
        assert reader.get(f"/api/v1/records/{record['id']}", expect=200).json()[
            "data"
        ] == record | {"allowed_actions": ["view"]}
        for ref in refs:
            response = pat.post(
                "/api/v1/records", json=record_body(run_id, references=[{**ref, "id": "absent"}])
            )
            assert response.status_code == 404
            response = pat.put(
                f"/api/v1/records/{record['id']}",
                json={"expected_revision": 1, "references": [{**ref, "id": "absent"}]},
            )
            assert response.status_code == 404
        with service.db.write() as conn:
            conn.execute(
                update(t.artifacts)
                .where(t.artifacts.c.id == artifact["id"])
                .values(status="expired")
            )
        again = pat.put(
            f"/api/v1/records/{record['id']}",
            json={"expected_revision": 1, "title": "Evidence retained"},
            expect=200,
        ).json()["data"]
        assert again["excerpts"] == record["excerpts"]
        assert (
            len(reader.get(f"/api/v1/records/{record['id']}/history", expect=200).json()["data"])
            == 2
        )
        # A new reference can name an expired artifact's retained metadata.
        pat.post("/api/v1/records", json=payload, expect=201)
        for target in (
            {"type": "collection", "id": collection["id"]},
            {"type": "attachment", "id": "tools"},
            {"type": "attachment", "id": "tools", "snapshot_id": attachment},
        ):
            pat.post("/api/v1/records", json={**payload, "target": target}, expect=201)
        records = reader.get(
            "/api/v1/records?target_type=attachment&target_id=tools&kind=attachment-candidate",
            expect=200,
        ).json()["data"]
        assert len(records) == 2
        assert (
            len(
                reader.get(
                    f"/api/v1/records?target_type=attachment&target_id=tools&snapshot_id={attachment}",
                    expect=200,
                ).json()["data"]
            )
            == 1
        )
        assert (
            reader.get(
                "/api/v1/records?target_type=attachment&target_id=tools&kind=other", expect=200
            ).json()["data"]
            == []
        )
        for target in (
            {"type": "attachment", "id": "other", "snapshot_id": attachment},
            {"type": "attachment", "id": "tools", "snapshot_id": "missing"},
        ):
            assert (
                pat.post("/api/v1/records", json={**payload, "target": target}).status_code == 404
            )
        assert (
            pat.post(
                "/api/v1/records",
                json={
                    **payload,
                    "target": {"type": "run", "id": run_id, "snapshot_id": attachment},
                },
            ).status_code
            == 422
        )


def test_records_have_independent_recycle_lifecycle_and_author_only_edits(service, version_id):
    with running(service) as client:
        admin = setup_admin(service, client)
        alice = invite_member(service, admin, "alice")
        bob = invite_member(service, admin, "bob")
        run_id = finished_run(alice, version_id)
        pat = create_pat(alice)
        record = pat.post("/api/v1/records", json=record_body(run_id), expect=201).json()["data"]
        path = f"/api/v1/records/{record['id']}"
        for caller in (admin, bob, create_pat(admin, ("override", "experiment"))):
            assert (
                caller.put(path, json={"expected_revision": 1, "title": "forged"}).status_code
                == 403
            )
        assert bob.delete(path).status_code == 403
        alice.put(path, json={"expected_revision": 1, "body": {"changed": True}}, expect=200)
        versions = alice.get(path + "/history").json()["data"]
        assert [v["pat_id"] for v in versions] == [record["pat_id"], None]
        entry = create_pat(admin, ("override",)).delete(path, expect=200).json()["data"]
        assert entry["object_type"] == "workflow_record"
        assert alice.get(f"/api/v1/records?target_type=run&target_id={run_id}").json()["data"] == []
        assert alice.get(path, expect=200).json()["data"]["deletion"]["state"] == "recycled"
        assert alice.put(path, json={"expected_revision": 2, "title": "blocked"}).status_code == 409
        admin.post(f"/api/v1/admin/recycle-bin/{entry['id']}/restore", expect=200)
        assert len(alice.get(path + "/history").json()["data"]) == 2
        entry = pat.delete(path, expect=200).json()["data"]
        assert pat.delete(path, expect=200).json()["data"]["id"] == entry["id"]
        purge(admin, entry).raise_for_status()
        assert (
            alice.get(path).status_code == 410 and alice.get(path + "/history").status_code == 410
        )
        with service.db.read() as conn:
            assert (
                conn.execute(
                    select(t.workflow_record_revisions).where(
                        t.workflow_record_revisions.c.record_id == record["id"]
                    )
                ).first()
                is None
            )
            row = (
                conn.execute(
                    select(t.workflow_records).where(t.workflow_records.c.id == record["id"])
                )
                .mappings()
                .one()
            )
            assert (
                row["body_json"] == {}
                and row["excerpts_json"] == []
                and row["references_json"] == []
            )


def test_recycled_targets_are_readable_and_purged_targets_are_set_aside(service, version_id):
    with running(service) as client:
        admin = setup_admin(service, client)
        run_id = finished_run(admin, version_id)
        record = admin.post("/api/v1/records", json=record_body(run_id), expect=201).json()["data"]
        path = f"/api/v1/records/{record['id']}"
        listing = f"/api/v1/records?target_type=run&target_id={run_id}"
        entry = admin.delete(f"/api/v1/runs/{run_id}", expect=200).json()["data"]
        assert (
            admin.get(listing, expect=200).json()["data"][0]["target_deletion"]["state"]
            == "recycled"
        )
        assert admin.get(path, expect=200).json()["data"]["allowed_actions"] == ["view", "delete"]
        assert admin.put(path, json={"expected_revision": 1, "title": "x"}).status_code == 409
        assert admin.post("/api/v1/records", json=record_body(run_id)).status_code == 409
        admin.post(f"/api/v1/admin/recycle-bin/{entry['id']}/restore", expect=200)
        admin.put(path, json={"expected_revision": 1, "title": "restored"}, expect=200)
        entry = admin.delete(f"/api/v1/runs/{run_id}", expect=200).json()["data"]
        other = finished_run(admin, version_id)
        reference_body = record_body(other, references=[{"type": "run", "id": run_id}])
        referring = admin.post("/api/v1/records", json=reference_body, expect=201).json()["data"]
        purge(admin, entry).raise_for_status()
        # A purged reference does not freeze the record: edits that keep the stored
        # references go through, re-supplying the purged one is refused.
        referring_path = f"/api/v1/records/{referring['id']}"
        admin.put(referring_path, json={"expected_revision": 1, "title": "kept"}, expect=200)
        resupplied = {"expected_revision": 2, "references": reference_body["references"]}
        assert admin.put(referring_path, json=resupplied).status_code == 410
        assert admin.get(listing, expect=200).json()["data"] == []
        assert admin.get(path).status_code == 410
        assert admin.get(path + "/history").status_code == 410
        assert admin.post("/api/v1/records", json=reference_body).status_code == 410
        assert admin.post("/api/v1/records", json=record_body(run_id)).status_code == 410
        admin.delete(path, expect=200)


def test_dataset_version_records_share_revision_and_visibility_rules(service, version_id):
    with running(service) as client:
        admin = setup_admin(service, client)
        writer = sdk(create_pat(admin, ("experiment",)))
        reader = sdk(create_pat(admin, ()))
        target = {"type": "dataset_version", "id": version_id}
        record = writer.records.create(
            target=target,
            kind="dataset-provenance",
            title="Conversion",
            body={"repository": "local benchmark", "revision": "fixed", "converter": "v1"},
        )
        assert (
            reader.records.list(target_type="dataset_version", target_id=version_id)[0].id
            == record.id
        )
        changed = writer.records.update(
            record.id, expected_revision=record.revision, title="Updated"
        )
        assert changed.revision == 2 and len(reader.records.history(record.id)) == 2
        with pytest.raises(ApiError):
            writer.records.create(
                target={"type": "dataset_version", "id": "missing"},
                kind="dataset-provenance",
                title="Missing",
                body={},
            )
        admin.delete(f"/api/v1/admin/dataset-versions/{version_id}", expect=200)
        assert reader.records.list(target_type="dataset_version", target_id=version_id)[
            0
        ].target_deletion
        with pytest.raises(ApiError):
            writer.records.create(
                target=target, kind="dataset-provenance", title="Deleted", body={}
            )
