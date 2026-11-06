"""Uploaded task trees use the shared content transport and normal admission lifecycle."""

import asyncio
import hashlib
import shutil

import pytest
from maestro_sdk import ApiError
from sqlalchemy import select
from support import (
    FIXTURES,
    create_pat,
    invite_member,
    make_client,
    running,
    setup_admin,
    wait_operation,
)
from support.datasets import import_and_prepare, tasks_by_key
from support.results import purge
from support.sdk import sdk

from maestro.db import schema as t
from maestro.domain import limits
from maestro.execution.attachments import describe


@pytest.fixture
def upload_service(service, runner_python):
    service.datasets.sources["upload"].validator.runner_python = runner_python
    return service


@pytest.fixture
def task_tree(tmp_path):
    tree = tmp_path / "tasks"
    shutil.copytree(FIXTURES / "harbor-tasks", tree)
    return tree


def finish_upload(agent, operation):
    imported = agent.operations.wait(operation.id, interval=0.02)
    assert imported.status == "succeeded", imported
    if imported.result.preparation_operation_id:
        prepared = agent.operations.wait(imported.result.preparation_operation_id, interval=0.02)
        assert prepared.status == "succeeded", prepared
    return imported.result.dataset_version_id


def stage_files(admin, tree):
    files = describe(tree)
    for file in files:
        admin.client.put(
            f"/api/v1/attachment-contents/{file['sha256']}",
            content=(tree / file["path"]).read_bytes(),
            headers=admin._headers(),
        ).raise_for_status()
    return [{k: file[k] for k in ("path", "sha256", "executable")} for file in files]


@pytest.mark.parametrize("existing_kind", ["hub", "upload"])
def test_source_name_conflict_counts_recycled_and_only_dataset_purge_frees_name(
    upload_service, task_tree, source, existing_kind
):
    service = upload_service
    service.datasets.sources["hub"] = source
    service.preparer.sources["hub"] = source
    with running(service) as client:
        admin = setup_admin(service, client)
        agent = sdk(create_pat(admin, ("datasets",)))
        files = stage_files(admin, task_tree)
        if existing_kind == "hub":
            version, _ = import_and_prepare(admin, source_kind="hub")
        else:
            version = finish_upload(
                agent, agent.catalog.upload(task_tree, namespace="demo", name="arith")
            )
        dataset = agent.catalog.version(version).dataset_id
        assert agent.catalog.version(version).source_kind == existing_kind
        assert next(agent.catalog.datasets()).source_kind == existing_kind

        def opposing_request():
            if existing_kind == "hub":
                return admin.post(
                    "/api/v1/dataset-uploads",
                    json={"namespace": "demo", "name": "arith", "files": files},
                )
            return admin.post(
                "/api/v1/admin/dataset-imports",
                json={"namespace": " demo ", "name": " arith ", "ref": "v1"},
            )

        def assert_conflict():
            with service.db.read() as conn:
                before = list(conn.execute(select(t.operations.c.id)))
            response = opposing_request()
            assert response.status_code == 409, response.text
            error = response.json()["error"]
            assert error["code"] == "DATASET_SOURCE_CONFLICT"
            assert existing_kind in error["message"]
            assert error["field_errors"][0]["path"] == "/name"
            with service.db.read() as conn:
                assert list(conn.execute(select(t.operations.c.id))) == before

        assert_conflict()
        entry = admin.delete(f"/api/v1/admin/dataset-versions/{version}", expect=200).json()["data"]
        assert_conflict()
        assert purge(admin, entry).status_code == 200
        # Removing all versions leaves the dataset itself owning the name.
        assert_conflict()
        entry = admin.delete(f"/api/v1/admin/datasets/{dataset}", expect=200).json()["data"]
        assert_conflict()
        assert purge(admin, entry).status_code == 200
        accepted = opposing_request()
        assert accepted.status_code == 202, accepted.text
        done = wait_operation(admin, accepted.json()["data"]["id"])
        assert done["status"] == "succeeded", done
        detail = agent.catalog.version(done["result"]["dataset_version_id"])
        assert detail.source_kind == ("upload" if existing_kind == "hub" else "hub")
        assert detail.dataset_id != dataset


@pytest.mark.parametrize("winner", ["hub", "upload"])
def test_import_recording_guards_two_requests_accepted_before_a_dataset_exists(
    upload_service, task_tree, source, monkeypatch, winner
):
    service = upload_service
    service.datasets.sources["hub"] = source
    service.preparer.sources["hub"] = source

    async def pause_imports():
        pass

    monkeypatch.setattr(service.preparer, "tick", pause_imports)
    with running(service) as client:
        admin = setup_admin(service, client)
        files = stage_files(admin, task_tree)
        pending = {
            "upload": admin.post(
                "/api/v1/dataset-uploads",
                json={"namespace": "demo", "name": "arith", "files": files},
                expect=202,
            ).json()["data"]["id"],
            "hub": admin.post(
                "/api/v1/admin/dataset-imports",
                json={"namespace": "demo", "name": "arith", "ref": "v1"},
                expect=202,
            ).json()["data"]["id"],
        }
        loser = "upload" if winner == "hub" else "hub"
        for kind in (winner, loser):
            asyncio.run_coroutine_threadsafe(
                service.preparer._job(pending[kind], service.preparer._import),
                service.worker._loop,
            ).result(timeout=10)
        assert wait_operation(admin, pending[winner])["status"] == "succeeded"
        failed = wait_operation(admin, pending[loser])
        assert failed["status"] == "failed"
        assert failed["error"]["code"] == "DATASET_SOURCE_CONFLICT"
        assert winner in failed["error"]["message"]
        with service.db.read() as conn:
            assert list(conn.execute(select(t.datasets.c.source_kind)).scalars()) == [winner]
            assert len(list(conn.execute(select(t.dataset_versions.c.id)))) == 1


def test_version_files_are_original_sorted_metadata_for_all_readers_without_task_reads(
    upload_service, task_tree
):
    service = upload_service
    script = min(task_tree.iterdir()) / "environment" / "生成 data.sh"
    script.write_bytes(b"#!/bin/sh\necho uploaded\n")
    script.chmod(0o755)
    expected = [
        {
            "path": f["path"],
            "sha256": f["sha256"],
            "executable": f["executable"],
            "size_bytes": f["size"],
        }
        for f in describe(task_tree)
    ]
    with running(service) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "member")
        reader = create_pat(member, ())
        agent = sdk(create_pat(admin, ("datasets",)))
        version = finish_upload(agent, agent.catalog.upload(task_tree, namespace="n", name="one"))
        assert (
            finish_upload(agent, agent.catalog.upload(task_tree, namespace="n", name="one"))
            == version
        )
        second = finish_upload(agent, agent.catalog.upload(task_tree, namespace="n", name="two"))
        url = f"/api/v1/dataset-versions/{version}/files"
        assert make_client(service).get(url).status_code == 401
        for caller in (admin, member, reader):
            assert caller.get(url, expect=200).json()["data"] == expected
        assert [f.model_dump() for f in sdk(reader).catalog.files(version)] == expected
        caps = reader.get("/api/v1/capabilities").json()["data"]["limits"]
        assert caps["dataset_upload_max_files"] == limits.DATASET_UPLOAD_MAX_FILES
        assert caps["dataset_upload_max_bytes"] == limits.DATASET_UPLOAD_MAX_BYTES
        assert reader.get("/api/v1/dataset-versions/missing/files").status_code == 404
        entry = admin.delete(f"/api/v1/admin/dataset-versions/{version}", expect=200).json()["data"]
        assert reader.get(url, expect=200).json()["data"] == expected
        assert purge(admin, entry).status_code == 200
        response = reader.get(url)
        assert response.status_code == 410
        assert response.json()["error"]["code"] == "OBJECT_PURGED"
        # A shared digest must not expose another version's files after purge.
        assert [f.model_dump() for f in sdk(reader).catalog.files(second)] == expected
        dataset = agent.catalog.version(second).dataset_id
        entry = admin.delete(f"/api/v1/admin/datasets/{dataset}", expect=200).json()["data"]
        assert [f.model_dump() for f in sdk(reader).catalog.files(second)] == expected
        assert purge(admin, entry).status_code == 200
        with pytest.raises(ApiError) as error:
            sdk(reader).catalog.files(second)
        assert error.value.status == 410 and error.value.code == "OBJECT_PURGED"
        with service.db.read() as conn:
            assert conn.execute(select(t.task_reads)).first() is None


def test_hub_version_file_list_explains_upstream_contents(service, source):
    service.datasets.sources["hub"] = source
    service.preparer.sources["hub"] = source
    with running(service) as client:
        admin = setup_admin(service, client)
        version, _ = import_and_prepare(admin, source_kind="hub")
        reader = sdk(create_pat(admin, ()))
        with pytest.raises(ApiError) as error:
            reader.catalog.files(version)
        assert error.value.status == 409
        assert error.value.code == "DATASET_VERSION_NOT_UPLOADED"
        assert "upstream" in str(error.value)


def test_sdk_upload_reupload_incremental_content_admission_and_cache_recovery(
    upload_service, task_tree, monkeypatch
):
    service = upload_service
    with running(service) as client:
        admin = setup_admin(service, client)
        agent = sdk(create_pat(admin, ("datasets",)))
        sent = []
        original = agent._call

        def record(method, path, **kwargs):
            if method == "PUT":
                sent.append(path)
            return original(method, path, **kwargs)

        monkeypatch.setattr(agent, "_call", record)
        operation = agent.catalog.upload(task_tree, namespace="local", name="benchmark")
        first = finish_upload(agent, operation)
        assert sent and len(sent) == len({f["sha256"] for f in describe(task_tree)})
        assert agent.catalog.version(first).admitted_task_count == len(list(task_tree.iterdir()))
        assert agent.catalog.version(first).source_kind == "upload"
        sent.clear()
        assert (
            finish_upload(
                agent, agent.catalog.upload(task_tree, namespace="local", name="benchmark")
            )
            == first
        )
        assert sent == []
        task = next(task_tree.iterdir())
        (task / "instruction.md").write_text("A changed task instruction")
        second = finish_upload(
            agent, agent.catalog.upload(task_tree, namespace="local", name="benchmark")
        )
        assert first != second and len(sent) == 1
        # Original uploaded bytes remain available after transport staging expires.
        with service.db.write() as conn:
            conn.execute(t.attachment_uploads.delete())
        service.attachments.sweep_uploads()
        source = service.datasets.sources["upload"]
        with service.db.read() as conn:
            row = (
                conn.execute(select(t.tasks).where(t.tasks.c.dataset_version_id == first))
                .mappings()
                .first()
            )
        service.preparer.materials.remove(row["content_digest"])
        from maestro.services.preparation import load_context

        with service.db.read() as conn:
            context = load_context(conn, row["id"])
        import asyncio

        async def quiet(*args, **kwargs):
            pass

        path = asyncio.run(service.preparer._material(context, quiet))
        assert path.is_dir()
        assert source.kind == "upload"


def test_invalid_tasks_are_reported_together_without_creating_a_version(upload_service, task_tree):
    service = upload_service
    tasks = sorted(task_tree.iterdir())
    (tasks[0] / "task.toml").write_text("[broken")
    (tasks[1] / "instruction.md").unlink()
    with running(service) as client:
        admin = setup_admin(service, client)
        agent = sdk(create_pat(admin, ("datasets",)))
        op = agent.catalog.upload(task_tree, namespace="local", name="bad")
        failed = agent.operations.wait(op.id, interval=0.02)
        assert failed.status == "failed"
        errors = failed.error.model_dump()["evidence"]["task_errors"]
        assert {e["task_key"] for e in errors} == {tasks[0].name, tasks[1].name}
        assert all(e["message"] for e in errors)
        assert list(agent.catalog.datasets()) == []
        service.attachments.sweep_uploads()
        with service.db.read() as conn:
            assert conn.execute(select(t.dataset_files)).first() is None


@pytest.mark.parametrize(
    "path",
    ["/absolute/task.toml", "a/../task.toml", "../task.toml", "a//task.toml", "a\\task.toml"],
)
def test_upload_rejects_unsafe_paths_before_an_operation(upload_service, path):
    with running(upload_service) as client:
        admin = setup_admin(upload_service, client)
        raw = b"example"
        sha = hashlib.sha256(raw).hexdigest()
        admin.client.put(
            f"/api/v1/attachment-contents/{sha}", content=raw, headers=admin._headers()
        )
        response = admin.post(
            "/api/v1/dataset-uploads",
            json={
                "namespace": "local",
                "name": "bad",
                "files": [{"path": path, "sha256": sha, "executable": False}],
            },
        )
        assert response.status_code == 422
        assert response.json()["error"]["field_errors"][0]["code"] == "PATH"


def test_upload_permissions_limits_links_and_upstream_reasons(
    upload_service, task_tree, tmp_path, monkeypatch
):
    service = upload_service
    with running(service) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "member")
        for caller in (
            member,
            create_pat(member, ()),
            create_pat(member, ("experiment",)),
            create_pat(admin, ("experiment",)),
        ):
            assert (
                caller.post(
                    "/api/v1/dataset-uploads", json={"namespace": "n", "name": "n", "files": []}
                ).status_code
                == 403
            )
        reader = create_pat(admin, ())
        assert (
            reader.post("/api/v1/attachment-contents/missing", json={"sha256": []}).status_code
            == 403
        )
        agent = sdk(create_pat(admin, ("datasets",)))
        for field, value in (("DATASET_UPLOAD_MAX_FILES", 1), ("DATASET_UPLOAD_MAX_BYTES", 1)):
            with monkeypatch.context() as patch:
                patch.setattr(limits, field, value)
                with pytest.raises(ApiError) as error:
                    agent.catalog.upload(task_tree, namespace="local", name="limit")
                assert error.value.code == "DATASET_UPLOAD_INVALID"
        (task_tree / "link").symlink_to(tmp_path)
        with pytest.raises(ValueError, match="links"):
            agent.catalog.upload(task_tree, namespace="local", name="links")
        for url in (
            "/api/v1/dataset-search?q=local&source_kind=upload",
            "/api/v1/dataset-search/versions?namespace=local&name=n&source_kind=upload",
        ):
            response = admin.get(url)
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "SOURCE_OPERATION_UNAVAILABLE"
        response = admin.post(
            "/api/v1/admin/dataset-imports",
            json={
                "source_kind": "upload",
                "namespace": "n",
                "name": "n",
                "ref": "latest",
                "credential_id": "unused",
            },
        )
        assert response.status_code == 422 and "credentials" in response.json()["error"]["message"]


def test_uploaded_version_first_preparation_failure_and_retry(
    upload_service, task_tree, environment
):
    key = min(task_tree.iterdir()).name
    environment.fail_prepare[key] = ["transient"]
    with running(upload_service) as client:
        admin = setup_admin(upload_service, client)
        agent = sdk(create_pat(admin, ("datasets",)))
        imported = agent.operations.wait(
            agent.catalog.upload(task_tree, namespace="n", name="n").id, interval=0.02
        )
        assert imported.status == "succeeded"
        first = agent.operations.wait(imported.result.preparation_operation_id, interval=0.02)
        assert first.status == "failed"
        tasks = tasks_by_key(admin, imported.result.dataset_version_id)
        assert tasks[key]["admission"]["state"] == "first_failed"
        retry = admin.post(
            f"/api/v1/admin/dataset-versions/{imported.result.dataset_version_id}/preparations",
            json={},
            expect=202,
        ).json()["data"]
        assert wait_operation(admin, retry["id"])["status"] == "succeeded"


def test_dataset_originals_are_shared_until_the_last_version_is_purged(upload_service, task_tree):
    from support.results import purge

    service = upload_service
    with running(service) as client:
        admin = setup_admin(service, client)
        agent = sdk(create_pat(admin, ("datasets",)))
        first = finish_upload(agent, agent.catalog.upload(task_tree, namespace="n", name="one"))
        assert (
            finish_upload(agent, agent.catalog.upload(task_tree, namespace="n", name="one"))
            == first
        )
        with service.db.read() as conn:
            assert len(list(conn.execute(select(t.dataset_files)))) == len(describe(task_tree))
        second = finish_upload(agent, agent.catalog.upload(task_tree, namespace="n", name="two"))
        shas = {f["sha256"] for f in describe(task_tree)}
        with service.db.write() as conn:
            conn.execute(t.attachment_uploads.delete())
        first_entry = admin.delete(f"/api/v1/admin/dataset-versions/{first}", expect=200).json()[
            "data"
        ]
        assert purge(admin, first_entry).status_code == 200
        assert all(service.attachments.store.blob(s) is not None for s in shas)
        second_entry = admin.delete(f"/api/v1/admin/dataset-versions/{second}", expect=200).json()[
            "data"
        ]
        assert purge(admin, second_entry).status_code == 200
        assert all(service.attachments.store.blob(s) is None for s in shas)


def test_task_files_preserve_unicode_spaces_modes_and_reject_transport_oversize(
    upload_service, task_tree, monkeypatch
):
    service = upload_service
    task = min(task_tree.iterdir())
    script = task / "environment" / "生成 data.sh"
    script.write_text("#!/bin/sh\necho data\n")
    script.chmod(0o755)
    with running(service) as client:
        admin = setup_admin(service, client)
        agent = sdk(create_pat(admin, ("datasets",)))
        with monkeypatch.context() as patch:
            patch.setattr(limits, "ATTACHMENT_FILE_MAX_BYTES", 1)
            with pytest.raises(ApiError) as too_large:
                agent.catalog.upload(task_tree, namespace="n", name="oversize")
            assert too_large.value.code == "ATTACHMENT_TOO_LARGE"
        version = finish_upload(
            agent, agent.catalog.upload(task_tree, namespace="n", name="unicode")
        )
        with service.db.read() as conn:
            task_row = (
                conn.execute(
                    select(t.tasks).where(
                        t.tasks.c.dataset_version_id == version, t.tasks.c.task_key == task.name
                    )
                )
                .mappings()
                .one()
            )
        restored = (
            service.preparer.materials.path_for(task_row["content_digest"])
            / "environment"
            / script.name
        )
        assert restored.read_bytes() == script.read_bytes() and restored.stat().st_mode & 0o100


def test_image_only_tasks_need_no_environment_files(upload_service, task_tree):
    task = min(task_tree.iterdir())
    shutil.rmtree(task / "environment")
    with (task / "task.toml").open("a") as config:
        config.write('docker_image = "example.test/prepared:fixed"\n')
    with running(upload_service) as client:
        admin = setup_admin(upload_service, client)
        agent = sdk(create_pat(admin, ("datasets",)))
        version = finish_upload(agent, agent.catalog.upload(task_tree, namespace="n", name="image"))
        assert all(t.can_submit for t in agent.catalog.tasks(version))
