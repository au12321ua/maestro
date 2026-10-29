"""The deployment limits of config.toml reach the components that enforce them."""

import asyncio
import inspect
import json
import stat
import sys

from test_harbor_executor import Cancel, RecordingDocker, RecordingSink, make_spec

from maestro import cli
from maestro.application import Maestro
from maestro.config import MIB, Settings
from maestro.db import migrate
from maestro.execution import harbor
from maestro.execution.harbor import HarborExecutor
from maestro.execution.hub import HubSource
from maestro.execution.materials import TaskMaterials
from maestro.execution.preparation import DockerEnvironment
from maestro.services.preparation import Preparer


def settings_with(tmp_path, **extra) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        master_key_dir=tmp_path / "keys",
        **extra,
    )


def default(function, name):
    return inspect.signature(function).parameters[name].default


def test_component_defaults_match_the_configuration_defaults(tmp_path):
    """The numbers exist twice (the settings and the component's own default for tests and
    code that builds one directly); they must never drift apart."""
    s = settings_with(tmp_path)
    assert default(HarborExecutor.__init__, "artifact_limits") == s.artifact_limits()
    assert default(HarborExecutor.__init__, "silence_limit") == s.runner_silence_sec
    assert default(HarborExecutor.__init__, "heartbeat_sec") == s.runner_heartbeat_sec
    assert default(DockerEnvironment.__init__, "build_timeout") == s.build_timeout_default_sec
    assert default(DockerEnvironment.__init__, "build_timeout_max") == s.build_timeout_max_sec
    assert default(DockerEnvironment.__init__, "pull_timeout") == s.pull_timeout_sec
    assert default(DockerEnvironment.__init__, "push_timeout") == s.push_timeout_sec
    assert default(DockerEnvironment.__init__, "registry") == s.image_registry
    assert default(Preparer.__init__, "restore_slots") == s.restore_slots
    assert default(Preparer.__init__, "build_timeout_max") == s.build_timeout_max_sec
    assert default(HubSource.__init__, "max_runners") == s.hub_runner_slots


STUB = """
import json, sys
spec = json.loads(sys.stdin.read())
with open({capture!r}, "w") as handle:
    json.dump(spec, handle)
def emit(sequence, kind, **payload):
    print(json.dumps({{"protocol_version": 2, "run_id": spec["run_id"], "sequence": sequence,
                      "type": kind, "at": "x", "payload": payload}}), flush=True)
emit(1, "started", harbor_version="0.24.0", runner_version="1", n_trials=0)
emit(2, "completed", status="completed")
"""


def test_the_executor_sends_the_configured_artifact_limits_to_the_runner(tmp_path):
    capture = tmp_path / "request.json"
    script = tmp_path / "stub.py"
    script.write_text(STUB.format(capture=str(capture)))
    wrapper = tmp_path / "runner-python"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{script}"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    materials = TaskMaterials(tmp_path / "cache")
    limits = {"max_files": 7, "max_file_bytes": 3 * MIB, "max_total_bytes": 9 * MIB}
    executor = HarborExecutor(
        wrapper,
        tmp_path / "work",
        materials,
        RecordingDocker(),
        silence_limit=44,
        heartbeat_sec=4,
        artifact_limits=limits,
    )
    spec = make_spec(materials, tmp_path, "ok", trials=1)

    asyncio.run(executor.execute(spec, RecordingSink(), Cancel()))

    request = json.loads(capture.read_text())
    assert request["artifact_limits"] == limits
    assert request["heartbeat_sec"] == 4
    assert executor.silence_limit == 44


def test_the_redaction_ceiling_follows_the_per_file_limit():
    secret = b"sk-abcdefghijklmnopqrstuvwx"
    text = secret + b"\n" + b"x" * 100
    assert secret not in harbor._redacted(text, limit=1000)
    # A file above the limit is never collected by the Runner, so none goes unscanned;
    # the helper itself leaves anything above the limit it was given untouched.
    assert harbor._redacted(text, limit=50) == text


def test_the_command_line_builds_the_executor_from_the_settings(tmp_path):
    runner = tmp_path / "python"
    runner.write_text("")
    (tmp_path / "data").mkdir()  # the service creates its work directory inside it
    settings = settings_with(
        tmp_path,
        runner_python=runner,
        runner_heartbeat_sec=5,
        runner_silence_sec=50,
        artifact_max_files=11,
        artifact_file_max_mb=2,
        artifact_attempt_max_mb=20,
    )
    executor = cli._harbor_executor(settings)
    assert executor.heartbeat_sec == 5 and executor.silence_limit == 50
    assert executor.artifact_limits == {
        "max_files": 11,
        "max_file_bytes": 2 * MIB,
        "max_total_bytes": 20 * MIB,
    }


def test_the_command_line_builds_the_preparation_side_from_the_settings(tmp_path):
    runner = tmp_path / "python"
    runner.write_text("")
    settings = settings_with(
        tmp_path,
        runner_python=runner,
        build_timeout_default_sec=100,
        build_timeout_max_sec=200,
        pull_timeout_sec=300,
        push_timeout_sec=400,
        image_registry="127.0.0.1:9999",
        hub_runner_slots=3,
    )
    environment, sources = cli._preparation(settings)
    assert (environment.build_timeout, environment.build_timeout_max) == (100, 200)
    assert (environment.pull_timeout, environment.push_timeout) == (300, 400)
    assert environment.registry == "127.0.0.1:9999"
    assert environment.legacy_dir == settings.legacy_preparation_dir
    assert sources["hub"].max_runners == 3


def test_the_service_hands_the_settings_to_the_preparer_and_exports(tmp_path, environment):
    settings = settings_with(tmp_path, restore_slots=3, build_timeout_max_sec=900, export_max_gb=2)
    for name in ("data", "cache", "keys"):
        (tmp_path / name).mkdir(mode=0o700)
    service = Maestro.build(settings, environment=environment)
    try:
        assert service.preparer.restore_slot_count == 3
        assert service.preparer.build_timeout_max == 900
        assert service.exports.max_bytes == 2 * 1024**3
    finally:
        service.db.dispose()


def test_capabilities_report_the_limits_in_force(tmp_path, environment):
    from support import running

    settings = settings_with(
        tmp_path,
        export_max_gb=2,
        artifact_max_files=50,
        artifact_file_max_mb=8,
        artifact_attempt_max_mb=80,
        build_timeout_max_sec=900,
        hub_runner_slots=3,
    )
    for name in ("data", "cache", "keys"):
        (tmp_path / name).mkdir(mode=0o700)
    service = Maestro.build(settings, environment=environment)
    migrate.upgrade(service.db)
    try:
        with running(service, run_worker=False) as client:
            limits = client.get("/api/v1/capabilities").json()["data"]["limits"]
    finally:
        service.db.dispose()
    assert limits["export_max_bytes"] == 2 * 1024**3
    assert limits["artifact_max_files"] == 50
    assert limits["artifact_file_max_bytes"] == 8 * MIB
    assert limits["artifact_attempt_max_bytes"] == 80 * MIB
    assert limits["build_timeout_max_sec"] == 900 and limits["hub_runner_slots"] == 3
    # The team policy in force (revision 0, the built-in values), next to the deployment limits.
    assert limits["run_max_tasks"] == 1000 and limits["artifact_ttl_days"] == 7
