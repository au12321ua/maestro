"""Isolated instances: fresh data, cache and master-key directories per test."""

import asyncio
import socket
import subprocess
import time
import urllib.request

import pytest

from maestro.application import Maestro
from maestro.config import Settings
from maestro.db import migrate
from maestro.testing.fakes import (
    FakeDatasetSource,
    FakeEnvironment,
    FakeExecutor,
    MemoryArtifactStore,
)

# The registry ``maestroctl`` runs for prepared images (N-01), without its digest pin.
REGISTRY_IMAGE = "registry:3.0.0"


@pytest.fixture(scope="module")
def local_registry(tmp_path_factory):
    """A throwaway image registry for real-Docker tests: ``host:port``. Its data lives in
    the test's temporary directory and the container is removed afterwards."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    data = tmp_path_factory.mktemp("registry")
    name = f"maestro-test-registry-{port}"
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:5000",
         "-v", f"{data}:/var/lib/registry", "-e", "REGISTRY_STORAGE_DELETE_ENABLED=true",
         REGISTRY_IMAGE],
        capture_output=True, check=True, timeout=600,
    )  # fmt: skip
    address = f"127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while True:
        try:
            with urllib.request.urlopen(f"http://{address}/v2/", timeout=2):
                break
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)
    try:
        yield address
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


@pytest.fixture
def settings(tmp_path):
    data, cache, keys = tmp_path / "data", tmp_path / "cache", tmp_path / "keys"
    for path in (data, cache, keys):
        path.mkdir(mode=0o700)
    # Watermarks off and capacity fixed: tests must not depend on the host's free space,
    # cores or memory (test_disk and test_capacity set their own).
    return Settings(
        data_dir=data,
        cache_dir=cache,
        master_key_dir=keys,
        cookie_secure=False,
        execution_cpus=64,
        execution_memory_mb=1 << 20,
        storage_accept_min_free_gb=0,
        storage_accept_min_free_percent=0,
        storage_dispatch_min_free_gb=0,
        storage_dispatch_min_free_percent=0,
    )


@pytest.fixture
def executor():
    return FakeExecutor()


@pytest.fixture
def store():
    return MemoryArtifactStore()


@pytest.fixture
def source():
    fake = FakeDatasetSource()
    fake.add("demo", "arith", "v1", ["add-01", "add-02", "mul-01"])
    return fake


@pytest.fixture
def environment():
    return FakeEnvironment()


@pytest.fixture
def service(settings, executor, store, source, environment):
    built = Maestro.build(
        settings,
        executor=executor,
        store=store,
        environment=environment,
        sources={"fake": source},
    )
    migrate.upgrade(built.db)
    built.keys.initialize()
    built.worker.poll_seconds = 0.02
    built.worker.retry_waits = (0.01, 1.0, 0.01)
    yield built
    built.db.dispose()


def import_version(service, namespace="demo", name="arith", ref="v1", **request) -> dict:
    """Import and first-prepare through the real Operation code, outside the worker."""
    return asyncio.run(
        service.preparer.import_and_prepare(
            {"source_kind": "fake", "namespace": namespace, "name": name, "ref": ref, **request},
            None,
        )
    )


@pytest.fixture
def version_id(service, source):
    result = import_version(service)
    assert result["preparation"]["status"] == "succeeded", result
    return result["dataset_version_id"]
