"""Real Harbor Hub through the Runner, anonymous and public data only (M4).

Opt-in: ``MAESTRO_HUB_TESTS=1`` with network access to the Hub (proxy variables are
passed to the Runner). The import test also needs Docker with buildx: the public
``harbor/hello-world`` task builds ``FROM ubuntu:24.04``, which Docker may pull.
No credential is used; no model is called; prepared images are removed afterwards.
"""

import asyncio
import os
import subprocess
from pathlib import Path

import pytest
from support import running, setup_admin, wait_operation

from maestro.application import Maestro
from maestro.boundaries.dataset_source import SourceError
from maestro.db import migrate
from maestro.execution.hub import HubSource
from maestro.execution.preparation import DockerEnvironment
from maestro.services.instance import instance_id as read_instance_id
from maestro.testing.fakes import FakeExecutor

ROOT = Path(__file__).resolve().parents[2]
RUNNER = Path(os.environ.get("MAESTRO_RUNNER_PYTHON", ROOT / "runner/.venv/bin/python"))
DATASET = ("harbor", "hello-world")

pytestmark = pytest.mark.skipif(
    os.environ.get("MAESTRO_HUB_TESTS") != "1" or not RUNNER.is_file(),
    reason="set MAESTRO_HUB_TESTS=1 and install the Runner environment",
)


def test_catalog_calls_and_classified_failures(tmp_path):
    hub = HubSource(RUNNER.absolute(), tmp_path / "work")

    async def run():
        found = await hub.search("hello-world", None)
        assert {"namespace": "harbor", "name": "hello-world", "visibility": "public"} in found
        versions = await hub.list_versions(*DATASET, None)
        first = next(v for v in versions if v["revision"] == 1)
        by_tag = await hub.describe(*DATASET, "latest", None)
        by_digest = await hub.describe(*DATASET, first["resolved_digest"], None)
        assert by_tag.ref_kind == "tag" and by_digest.ref_kind == "digest"
        assert by_digest.resolved_digest == first["resolved_digest"]
        with pytest.raises(SourceError) as missing:
            await hub.describe(*DATASET, "999999", None)
        assert missing.value.kind == "not_found"
        with pytest.raises(SourceError) as rejected:
            await hub.search("hello-world", "maestro-test-invalid-key")
        assert rejected.value.kind == "auth"

    asyncio.run(run())


@pytest.fixture
def hub_service(settings, local_registry):
    environment = DockerEnvironment(local_registry)
    service = Maestro.build(
        settings,
        executor=FakeExecutor(),
        environment=environment,
        sources={"hub": HubSource(RUNNER.absolute(), settings.work_dir)},
    )
    migrate.upgrade(service.db)
    service.keys.initialize()
    service.worker.poll_seconds = 0.1
    instance = read_instance_id(service.db)
    try:
        yield service
    finally:
        service.db.dispose()
        refs = subprocess.run(
            [
                "docker",
                "image",
                "ls",
                f"maestro-prep/{instance}",
                "--format",
                "{{.Repository}}:{{.Tag}}",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        if refs:
            subprocess.run(["docker", "image", "rm", *refs], capture_output=True, check=False)


def test_import_and_first_preparation_from_the_hub(hub_service):
    service = hub_service
    with running(service) as client:
        admin = setup_admin(service, client)
        body = {"source_kind": "hub", "namespace": "harbor", "name": "hello-world", "ref": "1"}
        op = admin.post("/api/v1/admin/dataset-imports", json=body, expect=202).json()["data"]
        imported = wait_operation(admin, op["id"], timeout=300)
        assert imported["status"] == "succeeded", imported
        version_id = imported["result"]["dataset_version_id"]
        prepared = wait_operation(
            admin, imported["result"]["preparation_operation_id"], timeout=1200
        )
        assert prepared["status"] == "succeeded", prepared
        detail = admin.get(f"/api/v1/dataset-versions/{version_id}").json()["data"]
        assert detail["ref_kind"] == "revision" and detail["resolved_digest"].startswith("sha256:")
        assert detail["admitted_task_count"] == detail["task_count"] == 1
        (task,) = admin.get(f"/api/v1/dataset-versions/{version_id}/tasks").json()["data"]
        assert task["task_key"] == "hello-world/hello-world"
        assert task["admission"]["state"] == "admitted"
        assert task["local_preparation"]["cache_state"] == "ready"
        again = admin.post("/api/v1/admin/dataset-imports", json=body, expect=202).json()["data"]
        assert wait_operation(admin, again["id"], timeout=300)["result"]["existing"] is True
