"""Isolated instances: fresh data, cache and master-key directories per test."""

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

# The registry ``maestroctl`` runs for prepared images, without its digest pin.
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
        # Never ask GitHub from a test; test_updates turns it on with a fake source.
        update_check=False,
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


@pytest.fixture
def version_id(service, source):
    result = import_version(service)
    assert result["preparation"]["status"] == "succeeded", result
    return result["dataset_version_id"]


import importlib
import stat
import sys

import pytest
from support.datasets import import_version
from support.executor import FAKE_RUNNER
from support.runner import RUNNER_SRC

from maestro.db import schema as t
from maestro.domain.states import Role
from maestro.execution.materials import TaskMaterials
from maestro.services.principal import Channel, Principal


@pytest.fixture
def owner(service):
    with service.db.write() as conn:
        conn.execute(
            t.users.insert().values(
                id="usr_owner",
                username="owner",
                display_name="o",
                role="member",
                status="active",
                password_hash="x",
                revision=1,
                created_at="t",
                updated_at="t",
            )
        )
    return Principal("usr_owner", "owner", Role.MEMBER, Channel.WEB_SESSION, session_id="s")


@pytest.fixture
def agents():
    sys.path.insert(0, str(RUNNER_SRC))
    try:
        module = importlib.import_module("maestro_runner.agents")
        yield module
    finally:
        sys.path.remove(str(RUNNER_SRC))
        module.ATTACHMENTS[:] = []
        module.INSTALL.update(timeout_sec=None, report=None, env={})


@pytest.fixture
def materials(tmp_path):
    return TaskMaterials(tmp_path / "cache")


@pytest.fixture
def runner_python(tmp_path):
    """The executor runs ``<python> -I -m maestro_runner``; this wrapper ignores the
    arguments and runs the scripted Runner instead."""
    wrapper = tmp_path / "runner-python"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{FAKE_RUNNER}"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    return wrapper
