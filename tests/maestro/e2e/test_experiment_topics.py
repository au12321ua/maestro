"""A topic is an explicit, shared view of accepted experiments and handoffs."""

from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from maestro_sdk import ApiError
from sqlalchemy import func, select
from support import create_pat, invite_member, running, setup_admin
from support.attachments import attach
from support.configuration import prepare_model
from support.sdk import LoseFirstAnswer, sdk

from maestro.core import clock
from maestro.core.ids import new_id
from maestro.db import schema as t


def ref(kind, identity):
    return {"kind": kind, "id": identity}


def test_a_new_agent_resumes_from_a_shared_link(service, version_id, executor):
    with running(service) as client:
        admin = setup_admin(service, client)
        model_id, harness_id = prepare_model(admin)
        old = attach(admin, label="original", bootstrap=False)
        agent = sdk(create_pat(invite_member(service, admin, "organizer")))
        config = {
            "dataset_version_id": version_id,
            "execution_mode": "agent",
            "model_config_id": model_id,
            "harness_version_id": harness_id,
            "attachments": [old],
        }

        created = agent.topics.create("Verifier check", "Compare verifier stability")
        organized = agent.topics.open(created.id)
        resolved = agent.configs.resolve(config).raise_for_blockers()
        first = organized.runs.submit(name="baseline", resolution_id=resolved.resolution_id)
        assert agent.operations.wait(first.operation_id, interval=0.02).status == "succeeded"
        detail = agent.runs.get(first.run_id)
        assert detail.snapshot.config_digest == resolved.config_digest
        collection = agent.collections.create(
            "Verifier comparison", "models", base_run_id=detail.id, run_ids=[detail.id]
        )
        version = agent.catalog.version(version_id)
        agent.topics.change_references(
            created.id, add=[ref("dataset", version.dataset_id), ref("collection", collection.id)]
        )
        executor.script = {"add-01": ["hang"]}
        second = organized.runs.submit(config, name="follow-up still running")
        evidence = agent.runs.artifacts(first.run_id)[0]
        note = agent.records.create(
            {"type": "topic", "id": created.id},
            "handoff",
            "Check timeout first",
            text="The timeout needs diagnosis before another rerun.",
            references=[
                {"type": "artifact", "id": evidence.id},
                {"type": "collection", "id": collection.id},
            ],
        )
        new = attach(admin, label="replacement", bootstrap=False, extra_files={"new": b"v2"})
        agent.topics.update(organized.url, name="Renamed check")
        (matched,) = agent.topics.list(q="Renamed")
        assert (matched.id, matched.purpose) == (created.id, created.purpose)

        # The next agent starts with a different PAT and only the old link.
        next_agent = sdk(create_pat(invite_member(service, admin, "successor")))
        resumed = next_agent.topics.open(organized.url)
        overview = resumed.overview()
        assert overview.topic.name == "Renamed check"
        assert overview.active_run_count == 1
        assert [run.id for run in overview.active_runs] == [second.run_id]
        assert (
            next(
                run for run in overview.recent_runs if run.id == first.run_id
            ).scoring.scored_trials
            == 3
        )
        assert overview.latest_handoff.id == note.id
        assert overview.latest_handoff.author_username == "organizer"
        assert overview.latest_handoff.reference_locations[0].run_id == first.run_id
        assert [r.id for r in resumed.handoffs(limit=1)] == [note.id]
        assert [r.id for r in overview.collections] == [collection.id]
        assert [r.id for r in overview.datasets] == [version.dataset_id]
        assert [v.id for v in overview.used_dataset_versions] == [version_id]
        (snapshot,) = overview.used_attachment_snapshots
        assert snapshot.id == old != new
        assert snapshot.source_run_count == 2
        assert set(snapshot.source_run_ids) == {first.run_id, second.run_id}
        assert next_agent.attachments.get(snapshot.id).label == "original"
        same = admin.get(f"/api/v1/topics/{created.id}/overview", expect=200).json()["data"]
        assert same["latest_handoff"]["id"] == note.id
        assert same["used_attachment_snapshots"][0]["id"] == old

        rerun = resumed.runs.rerun(
            first.run_id,
            name="targeted retry",
            mode="explicit",
            task_ids=[agent.runs.trials(first.run_id)[-1].task_id],
        )
        modified = resumed.runs.submit(config, name="modified retry", source_run_id=first.run_id)
        assert {run.id for run in resumed.runs.list()} == {
            first.run_id,
            second.run_id,
            rerun.run_id,
            modified.run_id,
        }
        assert next_agent.runs.get(rerun.run_id).owner.username == "successor"
        reader = sdk(create_pat(admin, ()))
        assert reader.topics.overview(resumed.url).latest_handoff.id == note.id
        with pytest.raises(ApiError, match="EXPERIMENT|PAT_PERMISSION") as denied:
            reader.topics.change_references(resumed.id, remove=[ref("run", rerun.run_id)])
        assert denied.value.status == 403
        with pytest.raises(ValueError, match="this Maestro"):
            next_agent.topics.open(organized.url.replace("testserver", "other-host"))


def test_topics_bind_parallel_submissions_and_retry_a_lost_response(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        pat = create_pat(admin, run_limit=10)
        agent = sdk(pat)
        config = {"dataset_version_id": version_id, "execution_mode": "oracle"}
        a, b = [agent.topics.open(agent.topics.create(name).id) for name in ("A", "B")]
        transport = LoseFirstAnswer(pat.client._transport, "POST", "/api/v1/runs")
        with httpx.Client(base_url="http://testserver", transport=transport) as http:
            a_retry = sdk(pat, http=http).topics.open(a.url)
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(
                    a_retry.runs.submit, config, name="in A", idempotency_key="topic-a-key"
                )
                second = pool.submit(
                    b.runs.submit, config, name="in B", idempotency_key="topic-b-key"
                )
                in_a = first.result()
                in_b = second.result()
        assert transport.lost == 1 and in_a.replayed
        assert [r.id for r in a.references("run")] == [in_a.run_id]
        assert [r.id for r in b.references("run")] == [in_b.run_id]
        with pytest.raises(ValueError, match="different experiment topic"):
            a.runs.submit(config, name="mixed", topic_id=b.id)
        with pytest.raises(ApiError) as mismatch:
            b.runs.submit(config, name="in A", idempotency_key="topic-a-key")
        assert mismatch.value.code == "IDEMPOTENCY_CONFLICT"

        ordinary = agent.runs.submit(config, name="unscoped")
        assert {r.id for r in a.runs.list()} == {in_a.run_id}
        added = agent.topics.change_references(a.id, add=[ref("run", ordinary.run_id)] * 2)
        assert added.added == 1
        assert agent.topics.change_references(a.id, add=[ref("run", ordinary.run_id)]).added == 0
        bad = new_id("top")
        draft = [
            {"name": "valid", **config, "topic_id": a.id},
            {"name": "invalid", **config, "topic_id": bad},
        ]
        preview = agent.batches.preview("bad batch", draft)
        assert not preview.can_submit
        with pytest.raises(ApiError) as rejected:
            agent.batches.submit("bad batch", draft)
        assert rejected.value.code == "TOPIC_NOT_FOUND"
        assert rejected.value.field_errors[0]["path"] == "/runs/1/topic_id"
        assert len(list(agent.runs.list())) == 3
        assert len(list(a.references("run"))) == 2
        assert not list(agent.batches.list())

        draft[1].pop("topic_id")
        assert a.batches.preview("in topic", draft).can_submit
        batch = a.batches.submit("in topic", draft, idempotency_key="batch-key")
        repeat = a.batches.submit("in topic", draft, idempotency_key="batch-key")
        assert repeat.replayed and repeat.accepted == batch.accepted
        assert len(list(a.references("run"))) == 4
        assert len(list(b.references("run"))) == 1

        agent.topics.delete(a.id)
        assert a.runs.submit(config, name="in A", idempotency_key="topic-a-key").replayed
        with pytest.raises(ApiError) as deleted:
            a.runs.submit(config, name="after deletion")
        assert deleted.value.code == "TOPIC_DELETED"


def test_topic_membership_recycling_and_purging_preserve_originals(service, version_id):
    with running(service) as client:
        admin = setup_admin(service, client)
        agent = sdk(create_pat(admin))
        model_id, harness_id = prepare_model(admin)
        a, b = [agent.topics.open(agent.topics.create(name).id) for name in ("A", "B")]
        config = {
            "dataset_version_id": version_id,
            "execution_mode": "agent",
            "model_config_id": model_id,
            "harness_version_id": harness_id,
        }
        submitted = a.runs.submit(config, name="shared")
        assert agent.operations.wait(submitted.operation_id, interval=0.02).status == "succeeded"
        outside = agent.runs.submit(config, name="outside")
        run_id = submitted.run_id
        dataset = agent.catalog.version(version_id).dataset_id
        collection = agent.collections.create(
            "Shared", "models", base_run_id=run_id, run_ids=[run_id]
        )
        picked = [ref("run", run_id), ref("dataset", dataset), ref("collection", collection.id)]
        agent.topics.change_references(b.id, add=picked)
        assert [r.id for r in b.runs.list(dataset_version_id=version_id)] == [run_id]
        assert agent.runs.get(outside.run_id).dataset_version_id == version_id
        assert [c.id for c in agent.collections.list(topic_id=b.id)] == [collection.id]
        assert [d.id for d in agent.catalog.datasets(topic_id=b.id)] == [dataset]
        assert agent.collections.compare(collection.id).runs[0].run_id == run_id

        note = agent.records.create(
            {"type": "topic", "id": b.id}, "handoff", "Later", text="Check results"
        )
        note_entry = agent.records.delete(note.id)
        artifact = agent.runs.artifacts(run_id)[0]
        with service.db.read() as conn:
            before = (
                conn.execute(select(t.artifacts).where(t.artifacts.c.id == artifact.id))
                .mappings()
                .one()
            )
        assert agent.topics.change_references(a.id, remove=[ref("run", run_id)]).removed == 1
        assert not list(a.runs.list()) and len(list(b.runs.list())) == 1
        a_ref = agent.topics.change_references(a.id, add=[ref("run", run_id)])
        assert a_ref.added == 1
        entry = agent.topics.delete(b.id)
        assert b.overview().topic.deletion.state == "recycled"
        assert b.overview().collections[0].id == collection.id
        assert [r.id for r in a.runs.list()] == [run_id]
        admin.post(f"/api/v1/admin/recycle-bin/{entry.id}/restore", expect=200)
        assert b.overview().topic.deletion is None
        entry = agent.topics.delete(b.id)
        admin.post(
            f"/api/v1/admin/recycle-bin/{entry.id}/purge", json={"confirm": "DELETE"}, expect=200
        )
        with pytest.raises(ApiError) as purged:
            b.overview()
        assert purged.value.status == 410
        admin.post(f"/api/v1/admin/recycle-bin/{note_entry.id}/restore", expect=409)
        assert list(b.handoffs()) == []
        assert [r.id for r in a.runs.list()] == [run_id]
        assert agent.collections.compare(collection.id).runs[0].run_id == run_id
        with service.db.read() as conn:
            after = (
                conn.execute(select(t.artifacts).where(t.artifacts.c.id == artifact.id))
                .mappings()
                .one()
            )
            assert before == after
            assert conn.scalar(select(func.count()).select_from(t.workflow_records)) == 0

        deleted_run = agent.runs.delete(run_id)
        (reflected,) = a.references("run")
        assert reflected.deletion.state == deleted_run.state == "recycled"
        assert list(a.runs.list()) == []
        assert agent.topics.change_references(a.id, remove=[ref("run", run_id)]).removed == 1


def test_large_scoped_directories_keep_overview_bounded_and_page_filtered_results(
    service, version_id
):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        agent = sdk(create_pat(admin, run_limit=70))
        topic = agent.topics.open(agent.topics.create("Large history").id)
        other = agent.topics.open(agent.topics.create("Other").id)
        config = {"dataset_version_id": version_id, "execution_mode": "oracle"}
        ids = [topic.runs.submit(config, name=f"case {number:03}").run_id for number in range(29)]
        other.runs.submit(config, name="case 000")
        count = 61
        at = clock.to_text(clock.now())
        rows = [
            {
                "id": new_id("ds"),
                "source_kind": "fake",
                "namespace": "scope",
                "name": f"library-{number:03}",
                "created_at": at,
                "enabled": True,
                "description": "",
            }
            for number in range(count)
        ]
        with service.db.write() as conn:
            conn.execute(t.datasets.insert(), rows)
        agent.topics.change_references(topic.id, add=[ref("dataset", d["id"]) for d in rows[:-1]])
        overview = topic.overview()
        assert overview.active_run_count == len(ids)
        for preview in (overview.active_runs, overview.recent_runs, overview.datasets):
            assert len(preview) == 8
        (version,) = overview.used_dataset_versions
        assert version.source_run_count == len(ids)
        assert len(version.source_run_ids) == 8
        assert {r.id for r in topic.runs.list(limit=7, state="queued", q="case")} == set(ids)
        assert [r.id for r in topic.runs.list(q="case 000")] == [ids[0]]
        assert not list(topic.runs.list(state="running"))
        found = list(
            agent.catalog.datasets(topic_id=topic.id, q="library", limit=7, include_versions=False)
        )
        assert {d.id for d in found} == {d["id"] for d in rows[:-1]}
        assert next(topic.materials("dataset_version", limit=1)).id == version_id
        with service.db.read() as conn:
            assert conn.scalar(select(func.count()).select_from(t.task_reads)) == 0
        path = f"/api/v1/topics/{topic.id}/references"
        cursor = admin.get(f"{path}?kind=dataset&limit=1", expect=200).json()["meta"]["next_cursor"]
        admin.get(f"{path}?kind=run&cursor={cursor}", expect=400)
