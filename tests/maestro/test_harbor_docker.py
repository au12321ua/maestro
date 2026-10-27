"""Real Harbor Runner and Docker, Oracle tasks only (M3 and M4 exit criteria).

Opt-in: ``MAESTRO_DOCKER_TESTS=1``. Needs Docker with buildx, a local ``ubuntu:22.04``
image and the Runner environment (``uv sync --locked`` in ``runner/``, or
``MAESTRO_RUNNER_PYTHON``). No model, Hub or network request is made. Execution
resources carry a per-test instance label, prepared images live in the per-instance
``maestro-prep/<instance>`` repository; both are checked to be gone afterwards.
"""

import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from unittest import mock

import httpx
import pytest
from sqlalchemy import select
from support import Api, create_pat, invite_member, running, setup_admin, wait_operation

from maestro.application import Maestro
from maestro.boundaries.storage import LocalArtifactStore
from maestro.db import migrate
from maestro.db import schema as t
from maestro.domain.setup_network import BUILT_IN, SetupNetwork
from maestro.execution.attachments import AttachmentStore
from maestro.execution.docker import DockerResources
from maestro.execution.harbor import HarborExecutor
from maestro.execution.materials import LocalTaskSource, TaskMaterials
from maestro.execution.preparation import DockerEnvironment
from maestro.services.instance import instance_id as read_instance_id

ROOT = Path(__file__).resolve().parents[2]
TASKS = Path(__file__).with_name("fixtures") / "harbor-tasks"
# Agent tasks need tmux in the image (Terminus-2): build ``maestro-test/ubuntu-tmux:22.04``
# once, see the test docstring.
AGENT_TASKS = Path(__file__).with_name("fixtures") / "agent-tasks"
# OpenCode tasks need Node and the pinned OpenCode in the image: build
# ``maestro-test/ubuntu-opencode:22.04`` from fixtures/images/opencode once.
OPENCODE_TASKS = Path(__file__).with_name("fixtures") / "opencode-tasks"
RUNNER = Path(os.environ.get("MAESTRO_RUNNER_PYTHON", ROOT / "runner/.venv/bin/python"))
# Optional: the deployment's setup proxy (e.g. http://172.17.0.1:7890), for hosts that need
# one to build images and install attachments. Tests then also check its boundaries.
SETUP_PROXY = os.environ.get("MAESTRO_TEST_SETUP_PROXY", "")
# Stands in for the administrator's setup network revision (N-08).
SETUP_NETWORK = SetupNetwork(1, SETUP_PROXY) if SETUP_PROXY else BUILT_IN

pytestmark = pytest.mark.skipif(
    os.environ.get("MAESTRO_DOCKER_TESTS") != "1" or not RUNNER.is_file(),
    reason="set MAESTRO_DOCKER_TESTS=1 and install the Runner environment",
)


def labelled(instance_id: str) -> list[str]:
    found = []
    for kind in (["ps", "-aq"], ["network", "ls", "-q"], ["images", "-q"]):
        out = subprocess.run(
            ["docker", *kind, "--filter", f"label=io.maestro.instance_id={instance_id}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        found += [f"{kind[0]}:{x}" for x in out]
    return found


def prepared_images(instance_id: str) -> list[str]:
    out = subprocess.run(
        [
            "docker",
            "image",
            "ls",
            f"maestro-prep/{instance_id}",
            "--format",
            "{{.Repository}}:{{.Tag}}",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return sorted(out)


def remove_prepared(instance_id: str) -> None:
    refs = prepared_images(instance_id)
    if refs:
        subprocess.run(["docker", "image", "rm", *refs], capture_output=True, check=False)


def image_ids() -> set[str]:
    out = subprocess.run(
        ["docker", "images", "-q", "--no-trunc"], capture_output=True, text=True, check=True
    )
    return set(out.stdout.split())


@contextlib.contextmanager
def harbor(settings, tasks: Path, name: str):
    store = LocalArtifactStore(settings.artifacts_dir)
    executor = HarborExecutor(
        RUNNER,
        settings.work_dir,
        TaskMaterials(settings.cache_dir),
        DockerResources(),
        term_grace=30,
        attachments=AttachmentStore(settings.data_dir),
    )
    environment = DockerEnvironment(settings.preparation_dir)
    network = mock.patch("maestro.services.setup_network.current", lambda conn: SETUP_NETWORK)
    network.start()
    built = Maestro.build(
        settings,
        executor=executor,
        store=store,
        environment=environment,
        sources={"local": LocalTaskSource(tasks)},
    )
    migrate.upgrade(built.db)
    built.keys.initialize()
    built.worker.poll_seconds = 0.1
    instance = read_instance_id(built.db)
    environment.bind(instance)
    try:
        result = asyncio.run(
            built.preparer.import_and_prepare(
                {"source_kind": "local", "namespace": "local", "name": name, "ref": "v1"},
                None,
            )
        )
        assert result["preparation"]["status"] == "succeeded", result
        built.version_id = result["dataset_version_id"]
        yield built
    finally:
        network.stop()
        built.db.dispose()
        leftovers = labelled(instance)
        if leftovers:  # never leave resources behind, even when an assertion failed
            asyncio.run(DockerResources().cleanup({"io.maestro.instance_id": instance}))
        remove_prepared(instance)
        assert leftovers == []
        assert prepared_images(instance) == []


@pytest.fixture
def harbor_service(settings):
    with harbor(settings, TASKS, "oracle") as built:
        yield built


def task_ids(api: Api, version_id: str, *keys: str) -> list[str]:
    tasks = api.get(f"/api/v1/dataset-versions/{version_id}/tasks", expect=200).json()["data"]
    return [t["id"] for t in tasks if t["task_key"] in keys]


def submit(api: Api, version_id: str, keys, **body) -> dict:
    payload = {
        "name": "oracle",
        "dataset_version_id": version_id,
        "execution_mode": "oracle",
        "task_selection": {"mode": "explicit", "task_ids": task_ids(api, version_id, *keys)},
        **body,
    }
    headers = {"Idempotency-Key": uuid.uuid4().hex}
    return api.post("/api/v1/runs", json=payload, headers=headers, expect=202).json()["data"]


def wait_for(predicate, timeout=180.0, interval=0.25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not reached")


def test_oracle_run_from_submission_to_artifacts(harbor_service):
    service = harbor_service
    with running(service) as client:
        pat = create_pat(setup_admin(service, client))
        accepted = submit(pat, service.version_id, ["oracle-pass", "oracle-zero"], repetitions=2)
        operation = wait_operation(pat, accepted["operation_id"], timeout=300)
        assert operation["status"] == "succeeded"
        run_id = accepted["run_id"]
        run = pat.get(f"/api/v1/runs/{run_id}", expect=200).json()["data"]
        assert run["state"] == "finished" and run["cleanup_state"] == "complete"
        assert run["result_completeness"] == "complete"
        assert run["snapshot"]["effective"]["execution_mode"] == "oracle"
        trials = pat.get(f"/api/v1/runs/{run_id}/trials", expect=200).json()["data"]
        scores = sorted((t["task_key"], t["result"]["score"]) for t in trials)
        assert scores == [
            ("oracle-pass", 1.0),
            ("oracle-pass", 1.0),
            ("oracle-zero", 0.0),  # a real zero from the verifier, not a missing score
            ("oracle-zero", 0.0),
        ]
        for trial in trials:
            (attempt,) = trial["attempts"]
            steps = [s["step"] for s in attempt["steps"]]
            assert steps == ["starting", "environment", "agent", "collecting", "verifying"]
            assert attempt["executor_ref"]["runner_id"].startswith("harbor-0.24.0/")
        artifacts = pat.get(f"/api/v1/runs/{run_id}/artifacts", expect=200).json()["data"]
        reward = next(
            a
            for a in artifacts
            if a["relative_path"] == "verifier/reward.txt" and a["trial_id"] == trials[0]["id"]
        )
        body = pat.get(f"/api/v1/artifacts/{reward['id']}/download", expect=200)
        assert body.text.strip() == "1"
        assert {"agent/oracle.txt", "result.json", "runner/stderr.log"} <= {
            a["relative_path"] for a in artifacts
        }
        assert labelled(service.worker.instance_id) == []


def test_prepared_image_is_used_then_restored_from_its_original(harbor_service, settings):
    """M4 on the real host: first preparation is a reproducible build exported as an
    archive (U-17); Harbor runs the prepared image without building or pulling; after
    eviction a run restores exactly that image by loading the archive."""
    service = harbor_service
    instance = service.worker.instance_id or read_instance_id(service.db)
    with running(service) as client:
        admin = setup_admin(service, client)
        pat = create_pat(admin)
        tasks = {
            x["task_key"]: x
            for x in pat.get(f"/api/v1/dataset-versions/{service.version_id}/tasks").json()["data"]
        }
        task = tasks["oracle-pass"]
        image = next(
            r["digest"]
            for r in task["content"]["resources"]
            if r["resource_kind"] == "environment_image"
        )
        assert len(prepared_images(instance)) == 3
        archives = sorted(p.name for p in settings.preparation_dir.iterdir())
        assert len(archives) == 3 and all(a.endswith(".tar") for a in archives)

        before = image_ids()
        accepted = submit(pat, service.version_id, ["oracle-pass"])
        assert wait_operation(pat, accepted["operation_id"], timeout=300)["status"] == "succeeded"
        assert image_ids() == before  # Harbor neither built nor pulled anything

        preparation = task["local_preparation"]["id"]
        ref = f"maestro-prep/{instance}:{task['spec_digest'][7:]}"

        def restore_and_run():
            op = admin.post(f"/api/v1/admin/preparations/{preparation}/evict", expect=202)
            assert wait_operation(admin, op.json()["data"]["id"])["status"] == "succeeded"
            assert ref not in prepared_images(instance)
            again = submit(pat, service.version_id, ["oracle-pass"])
            assert wait_operation(pat, again["operation_id"], timeout=300)["status"] == "succeeded"
            restored = pat.get(f"/api/v1/preparations/{preparation}").json()["data"]
            assert restored["cache_state"] == "ready" and restored["image_id"] == image
            assert ref in prepared_images(instance)
            with service.db.read() as conn:
                from maestro.db import schema as t

                return (
                    conn.execute(
                        t.operations.select()
                        .where(t.operations.c.kind == "preparation_restore")
                        .order_by(t.operations.c.created_at.desc())
                    )
                    .mappings()
                    .first()
                )

        # The original archive is loaded back (U-17).
        assert restore_and_run()["status"] == "succeeded"
        # Without the original, a deterministic Dockerfile rebuilds to the same image id
        # under the reproducible settings, so the restore is accepted and re-exported.
        archive = settings.preparation_dir / f"{task['spec_digest'][7:]}.tar"
        archive.unlink()
        assert restore_and_run()["status"] == "succeeded"
        assert archive.is_file()
        assert labelled(instance) == []


def test_live_steps_and_logs_then_cancel(harbor_service):
    service = harbor_service
    with running(service) as client:
        pat = create_pat(setup_admin(service, client))
        accepted = submit(pat, service.version_id, ["oracle-slow", "oracle-pass"], concurrency=2)
        run_id = accepted["run_id"]

        def slow_trial():
            trials = pat.get(f"/api/v1/runs/{run_id}/trials", expect=200).json()["data"]
            return next(t for t in trials if t["task_key"] == "oracle-slow")

        slow = wait_for(lambda: (t := slow_trial())["phase"] == "agent" and t)
        assert slow["state"] == "executing"

        def live_log():
            artifacts = pat.get(f"/api/v1/runs/{run_id}/artifacts", expect=200).json()["data"]
            log = next(
                (
                    a
                    for a in artifacts
                    if a["trial_id"] == slow["id"] and a["relative_path"] == "agent/oracle.txt"
                ),
                None,
            )
            text = log and pat.get(f"/api/v1/artifacts/{log['id']}/download").text
            return text if text and "tick 2" in text else None

        assert "tick 1" in wait_for(live_log, timeout=60)
        assert pat.get(f"/api/v1/runs/{run_id}").json()["data"]["heartbeat_at"]
        pat.post(f"/api/v1/runs/{run_id}/cancel", expect=200)
        operation = wait_operation(pat, accepted["operation_id"], timeout=120)
        assert operation["status"] == "cancelled"
        trials = pat.get(f"/api/v1/runs/{run_id}/trials", expect=200).json()["data"]
        states = {t["task_key"]: (t["state"], t["reason_code"]) for t in trials}
        assert states == {
            "oracle-slow": ("cancelled", "USER_CANCELLED"),
            "oracle-pass": ("scored", None),
        }
        run = pat.get(f"/api/v1/runs/{run_id}", expect=200).json()["data"]
        assert run["cleanup_state"] == "complete"
        assert labelled(service.worker.instance_id) == []


def test_killed_runner_ends_trials_and_leaves_no_containers(harbor_service):
    service = harbor_service
    with running(service) as client:
        pat = create_pat(setup_admin(service, client))
        accepted = submit(pat, service.version_id, ["oracle-slow"])
        run_id = accepted["run_id"]
        wait_for(
            lambda: any(
                t["phase"] == "agent"
                for t in pat.get(f"/api/v1/runs/{run_id}/trials").json()["data"]
            )
        )
        assert labelled(service.worker.instance_id)  # the trial container is running
        with service.db.read() as conn:
            from maestro.db import schema as t

            runner = (
                conn.execute(t.runs.select().where(t.runs.c.id == run_id))
                .mappings()
                .one()["runner_json"]
            )
        os.killpg(runner["pgid"], signal.SIGKILL)
        wait_operation(pat, accepted["operation_id"], timeout=120)
        (trial,) = pat.get(f"/api/v1/runs/{run_id}/trials", expect=200).json()["data"]
        assert (trial["state"], trial["reason_code"]) == ("execution_error", "RUNNER_EXITED")
        run = pat.get(f"/api/v1/runs/{run_id}", expect=200).json()["data"]
        assert run["cleanup_state"] == "complete"
        assert labelled(service.worker.instance_id) == []


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_service_killed_mid_run_is_reconciled_on_restart(tmp_path):
    """The real failure: ``maestro serve`` dies with SIGKILL while a trial executes."""
    port = _free_port()
    config = tmp_path / "config.toml"
    config.write_text(
        f'data_dir = "{tmp_path}/data"\ncache_dir = "{tmp_path}/cache"\n'
        f'master_key_dir = "{tmp_path}/keys"\nlisten_port = {port}\ncookie_secure = false\n'
        f'runner_python = "{RUNNER}"\n'
    )
    cli = [sys.executable, "-m", "maestro.cli", "--config", str(config)]
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    env["HOME"] = str(tmp_path / "home")
    init = subprocess.run([*cli, "init"], env=env, capture_output=True, text=True, check=True)
    setup_token = init.stdout.strip().splitlines()[-1]
    subprocess.run([*cli, "dev-local-dataset", "oracle", str(TASKS)], env=env, check=True)
    base = f"http://127.0.0.1:{port}"

    def serve():
        with open(tmp_path / "serve.log", "ab") as logs:
            proc = subprocess.Popen(
                [*cli, "serve"], env=env, stdout=logs, stderr=logs, start_new_session=True
            )
        wait_for(lambda: _ready(base), timeout=30)
        return proc

    def instance_id():
        with sqlite3.connect(tmp_path / "data/app.db") as db:
            return db.execute("select instance_id from instance").fetchone()[0]

    first = serve()
    try:
        web = httpx.Client(base_url=base)
        csrf = web.post(
            "/api/v1/setup",
            json={"setup_token": setup_token, "username": "admin", "password": "x" * 16},
        ).json()["data"]["csrf_token"]
        token = web.post(
            "/api/v1/me/pats",
            json={"name": "agent", "scope": "experiment"},
            headers={"X-CSRF-Token": csrf},
        ).json()["data"]["token"]
        pat = Api(httpx.Client(base_url=base), token=token)
        version = pat.get("/api/v1/datasets").json()["data"][0]["versions"][0]["id"]
        accepted = submit(pat, version, ["oracle-slow", "oracle-pass"], concurrency=2)
        run_id = accepted["run_id"]

        def mid_run():
            trials = pat.get(f"/api/v1/runs/{run_id}/trials").json()["data"]
            phases = {t["task_key"]: (t["state"], t["phase"]) for t in trials}
            # One result confirmed, one trial still executing its agent step.
            return phases == {
                "oracle-pass": ("scored", None),
                "oracle-slow": ("executing", "agent"),
            }

        wait_for(mid_run)
        instance = instance_id()
        assert labelled(instance)
        os.killpg(first.pid, signal.SIGKILL)  # the service dies; the Runner has its own group
        first.wait(10)
        # The orphaned Runner stops itself and Harbor removes its containers.
        wait_for(lambda: not labelled(instance), timeout=60)
        second = serve()
        try:
            run = wait_for(
                lambda: (
                    (r := pat.get(f"/api/v1/runs/{run_id}").json()["data"])["state"]
                    == "interrupted"
                    and r["cleanup_state"] == "complete"
                    and r
                )
            )
            assert run["stop_reason"] == "SERVICE_RESTARTED"
            trials = pat.get(f"/api/v1/runs/{run_id}/trials").json()["data"]
            states = {t["task_key"]: (t["state"], t["reason_code"]) for t in trials}
            assert states["oracle-slow"] == ("interrupted", "SERVICE_RESTARTED")
            assert states["oracle-pass"][0] == "scored"
            assert labelled(instance) == []
            runners = subprocess.run(
                ["pgrep", "-f", "maestro_runner"], capture_output=True, text=True, check=False
            ).stdout.split()
            for pid in runners:
                cwd = Path(f"/proc/{pid}/cwd")
                assert not (cwd.exists() and str(cwd.resolve()).startswith(str(tmp_path)))
        finally:
            os.killpg(second.pid, signal.SIGTERM)
            second.wait(30)
    finally:
        if first.poll() is None:
            os.killpg(first.pid, signal.SIGKILL)
        if (tmp_path / "data/app.db").exists():
            remove_prepared(instance_id())


def _ready(base: str) -> bool:
    try:
        return httpx.get(f"{base}/health/ready", timeout=2).json().get("accepting_runs") is True
    except httpx.HTTPError:
        return False


# --- failures with the real Docker client --------------------------------------------
# The error classification reads the Docker client's own words. Unit tests feed it
# samples; these two use what BuildKit and the daemon really print.

BROKEN_BUILD = """FROM ubuntu:22.04
RUN echo "401 Unauthorized: pull access denied, requested access to the resource is denied" && exit 3
"""
MISSING_IMAGE = "maestro-test-nonexistent/no-such-image-7f3a9c:latest"


@contextlib.contextmanager
def prepare_only_service(settings, tmp_path, key: str):
    """An instance with the real Docker environment and one task that cannot be prepared."""
    tasks = tmp_path / "failing-tasks"
    task = tasks / key
    shutil.copytree(TASKS / "oracle-pass", task)
    if key == "build-fails":
        (task / "environment" / "Dockerfile").write_text(BROKEN_BUILD)
    else:  # an image that does not exist: no Dockerfile, the task names the image
        shutil.rmtree(task / "environment")
        toml = (task / "task.toml").read_text()
        named = f'[environment]\ndocker_image = "{MISSING_IMAGE}"\n'
        (task / "task.toml").write_text(toml.replace("[environment]\n", named, 1))
    environment = DockerEnvironment(settings.preparation_dir)
    built = Maestro.build(
        settings, environment=environment, sources={"local": LocalTaskSource(tasks)}
    )
    migrate.upgrade(built.db)
    built.keys.initialize()
    instance = read_instance_id(built.db)
    environment.bind(instance)
    built.instance = instance
    built.images_before = image_ids()
    try:
        yield built
    finally:
        built.db.dispose()
        leftovers = labelled(instance)
        if leftovers:
            asyncio.run(DockerResources().cleanup({"io.maestro.instance_id": instance}))
        remove_prepared(instance)
        assert leftovers == []
        assert prepared_images(instance) == []


def first_preparation(service, key: str):
    """Run the import and first preparation, then read how the one task ended."""
    asyncio.run(
        service.preparer.import_and_prepare(
            {"source_kind": "local", "namespace": "local", "name": "failing", "ref": "v1"}, None
        )
    )
    with service.db.read() as conn:
        row = conn.execute(
            select(t.task_admissions.c.state, t.task_admissions.c.evidence_json)
            .join(t.tasks, t.tasks.c.id == t.task_admissions.c.task_id)
            .where(t.tasks.c.task_key == key)
        ).one()
    return row.state, (row.evidence_json or {}).get("error") or {}


NO_CONNECTION = ("proxyconnect", "dial tcp", "no such host", "i/o timeout", "tls handshake")


def test_a_failed_build_is_not_mistaken_for_a_credential_problem(settings, tmp_path):
    """The RUN step prints what looks like a registry refusal and then fails. That is the
    task's own business: a failed build, not a pointer to the Hub credential, and not a
    transient trouble that would be retried."""
    with prepare_only_service(settings, tmp_path, "build-fails") as service:
        state, error = first_preparation(service, "build-fails")
        assert state == "first_failed", error
        assert error["kind"] == "failed", error
        assert "suggested_actions" not in error
        tail = error["evidence"]["stderr_tail"]
        assert "401 Unauthorized" in tail and "exit code: 3" in tail  # the real output
        assert image_ids() == service.images_before  # nothing half-built was kept


def test_a_pull_the_registry_refuses_is_a_credential_problem(settings, tmp_path):
    """A named image that does not exist: Docker answers "pull access denied", which looks
    the same as a missing login, so the admin is pointed at the credential. A host that
    cannot reach the registry at all gets a connection error instead; that is the only
    outcome that skips, so a wrong classification of the real answer still fails."""
    with prepare_only_service(settings, tmp_path, "image-missing") as service:
        state, error = first_preparation(service, "image-missing")
        tail = str(error.get("evidence", {}).get("stderr_tail", "")).lower()
        if error.get("kind") == "transient" and any(word in tail for word in NO_CONNECTION):
            pytest.skip("the Docker daemon cannot reach the registry from this host")
        assert state == "first_failed", error
        assert error["kind"] == "auth", error
        assert "pull access denied" in tail  # the daemon's real answer, not a guess
        assert error["suggested_actions"] == [{"type": "check_dataset_credential"}]
        assert image_ids() == service.images_before


class ScriptedModel:
    """A local stand-in for an OpenAI-compatible model API, so Terminus-2 runs for real
    without spending anything. It answers Terminus-2's JSON protocol: first a turn that
    solves the task, then completion."""

    KEY = "sk-local-scripted-model-key-0123456789"

    def __init__(self):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        model = self
        self.calls = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                model.calls.append(self.headers.get("Authorization"))
                if self.headers.get("Authorization") != f"Bearer {model.KEY}":
                    self.send_response(401)
                    self.end_headers()
                    return
                turn = sum(1 for m in body["messages"] if m["role"] == "assistant")
                if turn == 0:
                    reply = {
                        "analysis": "Write the answer.",
                        "plan": "One command.",
                        "commands": [
                            {
                                "keystrokes": "echo ok > /app/answer; "
                                # Even told the setup proxy, the agent must not get out.
                                + (
                                    f"echo {SETUP_PROXY} > /app/proxy-url; "
                                    f"http_proxy={SETUP_PROXY} "
                                    if SETUP_PROXY
                                    else ""
                                )
                                + "net-probe > /logs/agent/probe.txt\n",
                                "duration": 9,
                            }
                        ],
                        "task_complete": False,
                    }
                else:
                    reply = {"analysis": "Done.", "plan": "", "commands": [], "task_complete": True}
                data = json.dumps(
                    {
                        "id": f"chatcmpl-{turn}",
                        "object": "chat.completion",
                        "created": 0,
                        "model": body["model"],
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": json.dumps(reply)},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 100,
                            "completion_tokens": 20,
                            "total_tokens": 120,
                        },
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def scripted_model(admin: Api, model: ScriptedModel) -> tuple[str, str]:
    """The administrator's part: the local model behind a credential, and Terminus-2."""
    credential = admin.post(
        "/api/v1/admin/credentials",
        json={"name": "local", "kind": "model_api_key", "provider": "local", "value": model.KEY},
        expect=201,
    ).json()["data"]
    model_id = admin.post(
        "/api/v1/admin/model-configs",
        json={
            "name": "scripted",
            "provider": "local",
            "protocol": "openai-compatible",
            "endpoint": model.endpoint,
            "model_id": "scripted",
            "credential_id": credential["id"],
        },
        expect=201,
    ).json()["data"]["id"]
    harness_id = admin.post(
        "/api/v1/admin/harness-versions",
        json={
            "name": "terminus-2",
            "version": "harbor-0.24.0",
            "agent": "terminus-2",
            "protocols": ["openai-compatible"],
            "parameter_schema": {
                "parameters": {
                    "max_turns": {"type": "integer", "default": 5},
                    # asciinema cannot be installed without a network
                    "record_terminal_session": {"type": "boolean", "default": False},
                }
            },
        },
        expect=201,
    ).json()["data"]["id"]
    return model_id, harness_id


@pytest.fixture
def agent_service(settings):
    present = subprocess.run(
        ["docker", "image", "inspect", "maestro-test/ubuntu-tmux:22.04"],
        capture_output=True,
        check=False,
    )
    if present.returncode:
        pytest.skip("build maestro-test/ubuntu-tmux:22.04 first (ubuntu:22.04 + tmux)")
    with harbor(settings, AGENT_TASKS, "agent") as built:
        yield built


def test_terminus_runs_with_the_container_offline(agent_service, monkeypatch):
    """Terminus-2 calls the model from the Runner, so the task container gets no network
    while the agent works or the verifier runs (U-18), and the key never reaches a file.
    The model is a local stand-in, so nothing is spent. The same task is first run as
    Oracle, which keeps the author's (public) network: its probes must connect, so a
    closed probe in the model run means the platform policy closed it. Needs a host whose
    containers can reach example.com. Terminus-2 needs tmux in the task image: build
    ``maestro-test/ubuntu-tmux:22.04`` once (``FROM ubuntu:22.04`` plus ``apt-get install
    tmux``)."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    service = agent_service
    sidecars = []
    with contextlib.ExitStack() as stack:
        model = ScriptedModel()
        stack.callback(model.close)
        with running(service) as client:
            admin = setup_admin(service, client)
            model_id, harness_id = scripted_model(admin, model)
            pat = create_pat(admin)
            control = submit(pat, service.version_id, ["agent-pass"])
            assert wait_operation(pat, control["operation_id"], timeout=300)["status"] == (
                "succeeded"
            )
            control_files = pat.get(f"/api/v1/runs/{control['run_id']}/artifacts").json()["data"]

            def probe(files, path):
                found = next(a for a in files if a["relative_path"] == path)
                body = pat.get(f"/api/v1/artifacts/{found['id']}/download", expect=200)
                return body.text.split()[0]

            # The control: with the author's network both phases reach the internet.
            assert probe(control_files, "agent/probe.txt") == "NET_OPEN"
            assert probe(control_files, "verifier/probe.txt") == "NET_OPEN"
            accepted = submit(
                pat,
                service.version_id,
                ["agent-pass"],
                execution_mode="agent",
                model_config_id=model_id,
                harness_version_id=harness_id,
            )
            instance = service.worker.instance_id

            def ended():
                # Watch for the sidecar while the run is live: it is gone afterwards.
                sidecars.extend(
                    subprocess.run(
                        ["docker", "ps", "--filter", f"label=io.maestro.instance_id={instance}",
                         "--format", "{{.Names}}"],
                        capture_output=True, text=True, check=True,
                    ).stdout.split()
                )  # fmt: skip
                sidecars[:] = [n for n in sidecars if "egress-control" in n]
                data = pat.get(f"/api/v1/operations/{accepted['operation_id']}").json()["data"]
                return data if data["status"] not in {"accepted", "running"} else None

            operation = wait_for(ended, timeout=600, interval=0.5)
            run_id = accepted["run_id"]
            trials = pat.get(f"/api/v1/runs/{run_id}/trials", expect=200).json()["data"]
            artifacts = pat.get(f"/api/v1/runs/{run_id}/artifacts", expect=200).json()["data"]

            def download(path, trial=True):
                found = next(
                    a
                    for a in artifacts
                    if a["relative_path"] == path and (a["trial_id"] is not None) == trial
                )
                return pat.get(f"/api/v1/artifacts/{found['id']}/download", expect=200).content

            if operation["status"] != "succeeded":
                stderr = download("runner/stderr.log", trial=False).decode(errors="replace")
                errors = [a["error"] for t in trials for a in t["attempts"]]
                raise AssertionError(f"{errors}\n--- runner stderr tail ---\n{stderr[-6000:]}")
            (trial,) = trials
            assert trial["result"]["score"] == 1.0
            plan = json.loads(download("runner/network-plan.json", trial=False))
            effective = {
                role: plan["agent-pass"][role]["effective"]["network_mode"]
                for role in ("agent", "verifier")
            }
            assert effective == {"agent": "no-network", "verifier": "no-network"}
            # ...and the model run really could not get out, while working or verifying.
            assert probe(artifacts, "agent/probe.txt") == "NET_CLOSED"
            assert probe(artifacts, "verifier/probe.txt") == "NET_CLOSED"
            assert sidecars, "the egress-control sidecar never ran"
            paths = {a["relative_path"] for a in artifacts}
            assert "agent/trajectory.json" in paths
            # The trajectory was followed live as readable steps (U-07).
            live = download("agent/trajectory").decode()
            assert "$ echo ok > /app/answer" in live and "--- step" in live
            assert model.calls and set(model.calls) == {f"Bearer {model.KEY}"}
        stored = b"".join(
            p.read_bytes() for p in Path(service.settings.artifacts_dir).rglob("*") if p.is_file()
        )
        assert model.KEY.encode() not in stored
        assert labelled(service.worker.instance_id) == []


# Records, in this trial's container, where it ran, whether setup could reach the
# internet, which install markers exist (only its own) and what is mounted.
PROBE_BOOTSTRAP = """#!/bin/bash
set -e
out=/logs/agent/attachments
net-probe > $out/setup-probe.txt
env | grep -i '_proxy=' > $out/proxy-env.txt || true
touch /opt/maestro/installed-$(hostname)
ls /opt/maestro/installed-* | wc -l > $out/markers.txt
cat /proc/self/mountinfo > $out/mountinfo.txt
echo "dir=$MAESTRO_ATTACHMENT_DIR pwd=$(pwd) host=$(hostname)"
"""
# Kernel and container file systems; anything else in mountinfo comes from the host.
VIRTUAL_FS = {"overlay", "proc", "sysfs", "tmpfs", "devpts", "mqueue", "cgroup", "cgroup2"}
ALLOWED_HOST_MOUNTS = {
    "/etc/hostname",
    "/etc/hosts",
    "/etc/resolv.conf",
    "/logs/agent",
    "/logs/verifier",
    "/logs/artifacts",
    # Docker's init binary, read-only, for the platform's ``init: true`` (zombie reaping).
    "/usr/sbin/docker-init",
    # Rootless Docker cannot create device nodes in its user namespace, so it binds the
    # host's character devices, and masks these /proc files with /dev/null the same way.
    *(f"/dev/{name}" for name in ("null", "zero", "full", "random", "urandom", "tty")),
    *(
        f"/proc/{name}"
        for name in ("interrupts", "kcore", "keys", "latency_stats", "timer_list", "sched_debug")
    ),
}


def test_attachments_install_in_each_trial_container(agent_service, monkeypatch):
    """E2E-RUN-04 / TECH-04: a member's PAT publishes packages and submits; each trial
    installs in its own container during setup (with the setup network), the agent and
    verifier phases are then offline, and a failing or slow install ends only its trials,
    as an attachment failure rather than a score. Uses the local scripted model."""
    from test_attachments import package, publish

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    service = agent_service
    with contextlib.ExitStack() as stack:
        model = ScriptedModel()
        stack.callback(model.close)
        with running(service) as client:
            admin = setup_admin(service, client)
            model_id, harness_id = scripted_model(admin, model)
            member = invite_member(service, admin, "member")
            pat = create_pat(member)

            def published(name, script):
                data = package(
                    name,
                    bootstrap=False,
                    extra_files={"bootstrap.sh": (script.encode(), 0o755)},
                    compatible_harnesses=["terminus-2"],
                )
                return publish(pat, data, expect=201).json()["data"]["id"]

            probe_tools = published("probe-tools", PROBE_BOOTSTRAP)
            broken = published("broken", "#!/bin/bash\necho broken >&2\nexit 3\n")
            slow = published("slow", "#!/bin/bash\nsleep 120\n")

            def run(attachment, **body):
                accepted = submit(
                    pat,
                    service.version_id,
                    ["agent-pass"],
                    execution_mode="agent",
                    model_config_id=model_id,
                    harness_version_id=harness_id,
                    attachments=[attachment],
                    repetitions=2,
                    **body,
                )
                started = time.monotonic()
                operation = wait_operation(pat, accepted["operation_id"], timeout=600)
                run_id = accepted["run_id"]
                trials = pat.get(f"/api/v1/runs/{run_id}/trials", expect=200).json()["data"]
                files = pat.get(f"/api/v1/runs/{run_id}/artifacts", expect=200).json()["data"]
                return operation, trials, files, time.monotonic() - started

            def read(files, trial_id, path):
                found = next(
                    a for a in files if a["trial_id"] == trial_id and a["relative_path"] == path
                )
                return pat.get(f"/api/v1/artifacts/{found['id']}/download", expect=200).text

            operation, trials, files, _ = run(probe_tools)
            if operation["status"] != "succeeded":
                raise AssertionError([a["error"] for t in trials for a in t["attempts"]])
            assert [t["result"]["score"] for t in trials] == [1.0, 1.0]
            hosts = set()
            for trial in trials:
                tid = trial["id"]
                records = json.loads(read(files, tid, "maestro/attachments.json"))
                assert [(r["name"], r["status"], r["exit_code"]) for r in records] == [
                    ("probe-tools", "ok", 0)
                ]
                assert "installing" in [s["step"] for s in trial["attempts"][0]["steps"]]
                log = read(files, tid, "agent/attachments/00-probe-tools.log")
                assert "dir=/opt/maestro/attachments/00-probe-tools" in log
                # Setup had the author's network; the agent and verifier phases had none.
                assert (
                    read(files, tid, "agent/attachments/setup-probe.txt").split()[0] == "NET_OPEN"
                )
                assert read(files, tid, "agent/probe.txt").split()[0] == "NET_CLOSED"
                assert read(files, tid, "verifier/probe.txt").split()[0] == "NET_CLOSED"
                # Only this trial's own install marker: no shared install layer.
                assert read(files, tid, "agent/attachments/markers.txt").strip() == "1"
                # Install scripts get the setup proxy when one is configured, and nothing
                # else does: the host's own Docker client proxy never reaches a container.
                seen = read(files, tid, "agent/attachments/proxy-env.txt")
                assert (f"HTTP_PROXY={SETUP_PROXY}" in seen) if SETUP_PROXY else seen.strip() == ""
                assert records[0]["proxy"] is bool(SETUP_PROXY)
                mounts = set()
                for line in read(files, tid, "agent/attachments/mountinfo.txt").splitlines():
                    fields, _, rest = line.partition(" - ")
                    if rest.split()[0] not in VIRTUAL_FS:
                        mounts.add(fields.split()[4])
                assert mounts <= ALLOWED_HOST_MOUNTS, mounts
                assert not any(m.startswith("/opt") for m in mounts)
                hosts.add(log)
            assert len(hosts) == 2

            calls = len(model.calls)
            operation, trials, files, _ = run(broken)
            assert operation["status"] == "failed"
            for trial in trials:
                (attempt,) = trial["attempts"]
                assert trial["state"] == "execution_error"
                assert (trial["result"] or {}).get("score") is None
                assert attempt["error"]["failure_cause"] == "attachment_failed"
                assert attempt["error"]["detail"]["exit_code"] == 3
                log = read(files, trial["id"], "agent/attachments/00-broken.log")
                assert log.strip() == "broken"
            assert len(model.calls) == calls, "the agent must not start after a failed install"

            operation, trials, files, seconds = run(slow, timeouts={"bootstrap_sec": 5})
            assert operation["status"] == "failed" and seconds < 240
            for trial in trials:
                error = trial["attempts"][0]["error"]
                assert error["failure_cause"] == "attachment_failed"
                assert error["exception_type"] == "AttachmentInstallTimeout"
            assert len(model.calls) == calls
        assert labelled(service.worker.instance_id) == []


MODEL_ENV = ("MAESTRO_MODEL_ENDPOINT", "MAESTRO_MODEL_ID", "MAESTRO_MODEL_KEY_FILE")


@pytest.mark.skipif(
    os.environ.get("MAESTRO_MODEL_TESTS") != "1" or not all(map(os.environ.get, MODEL_ENV)),
    reason="real model smoke: set MAESTRO_MODEL_TESTS=1 and " + ", ".join(MODEL_ENV),
)
def test_terminus_with_a_real_model(agent_service):
    """One trial of a trivial task against a real OpenAI-compatible endpoint, at most five
    turns: the credential reaches the model, usage is recorded, the key stays out of every
    stored file. This spends real tokens; run it deliberately."""
    service = agent_service
    key = Path(os.environ["MAESTRO_MODEL_KEY_FILE"]).expanduser().read_text().strip()
    with running(service) as client:
        admin = setup_admin(service, client)
        credential = admin.post(
            "/api/v1/admin/credentials",
            json={"name": "live", "kind": "model_api_key", "provider": "live", "value": key},
            expect=201,
        ).json()["data"]
        model_id = admin.post(
            "/api/v1/admin/model-configs",
            json={
                "name": "live",
                "provider": "live",
                "protocol": "openai-compatible",
                "endpoint": os.environ["MAESTRO_MODEL_ENDPOINT"],
                "model_id": os.environ["MAESTRO_MODEL_ID"],
                "credential_id": credential["id"],
            },
            expect=201,
        ).json()["data"]["id"]
        harness_id = admin.post(
            "/api/v1/admin/harness-versions",
            json={
                "name": "terminus-2",
                "version": "harbor-0.24.0",
                "agent": "terminus-2",
                "protocols": ["openai-compatible"],
                "parameter_schema": {
                    "parameters": {
                        "max_turns": {"type": "integer", "default": 5, "maximum": 5},
                        "record_terminal_session": {"type": "boolean", "default": False},
                    }
                },
            },
            expect=201,
        ).json()["data"]["id"]
        pat = create_pat(admin)
        accepted = submit(
            pat,
            service.version_id,
            ["agent-pass"],
            execution_mode="agent",
            model_config_id=model_id,
            harness_version_id=harness_id,
        )
        operation = wait_operation(pat, accepted["operation_id"], timeout=900)
        run_id = accepted["run_id"]
        trials = pat.get(f"/api/v1/runs/{run_id}/trials", expect=200).json()["data"]
        summary = pat.get(f"/api/v1/runs/{run_id}/summary", expect=200).json()["data"]
        artifacts = pat.get(f"/api/v1/runs/{run_id}/artifacts", expect=200).json()["data"]
        live = next(a for a in artifacts if a["relative_path"] == "agent/trajectory")
        text = pat.get(f"/api/v1/artifacts/{live['id']}/download", expect=200).text
        print("\nOPERATION", operation["status"])
        print("TRIALS", [(t["state"], t["result"]) for t in trials])
        print("USAGE", summary.get("usage"))
        print("LIVE TRAJECTORY (head)\n" + text[:3000])
    stored = b"".join(
        p.read_bytes() for p in Path(service.settings.artifacts_dir).rglob("*") if p.is_file()
    )
    assert key.encode() not in stored
    assert operation["status"] == "succeeded", trials
    assert labelled(service.worker.instance_id) == []


@pytest.mark.skipif(
    os.environ.get("MAESTRO_MODEL_TESTS") != "1" or not all(map(os.environ.get, MODEL_ENV)),
    reason="real model smoke: set MAESTRO_MODEL_TESTS=1 and " + ", ".join(MODEL_ENV),
)
def test_opencode_with_a_real_model(settings):
    """OpenCode calls the model from inside the container: the agent phase may reach the
    model's host and nothing else, the verifier nothing; the key is a file only while the
    agent runs and is in no stored file; the event stream reads as steps. One trial, a
    trivial task: this spends real tokens, run it deliberately."""
    present = subprocess.run(
        ["docker", "image", "inspect", "maestro-test/ubuntu-opencode:22.04"],
        capture_output=True,
        check=False,
    )
    if present.returncode:
        pytest.skip("build maestro-test/ubuntu-opencode:22.04 first (fixtures/images/opencode)")
    key = Path(os.environ["MAESTRO_MODEL_KEY_FILE"]).expanduser().read_text().strip()
    endpoint = os.environ["MAESTRO_MODEL_ENDPOINT"]
    with harbor(settings, OPENCODE_TASKS, "opencode") as service, running(service) as client:
        admin = setup_admin(service, client)
        credential = admin.post(
            "/api/v1/admin/credentials",
            json={"name": "live", "kind": "model_api_key", "provider": "live", "value": key},
            expect=201,
        ).json()["data"]
        model_id = admin.post(
            "/api/v1/admin/model-configs",
            json={
                "name": "live",
                "provider": "live",
                "protocol": "openai-compatible",
                "endpoint": endpoint,
                "model_id": os.environ["MAESTRO_MODEL_ID"],
                "credential_id": credential["id"],
            },
            expect=201,
        ).json()["data"]["id"]
        harness_id = admin.post(
            "/api/v1/admin/harness-versions",
            json={
                "name": "opencode",
                "version": "1.18.35",
                "agent": "opencode",
                "protocols": ["openai-compatible"],
                "parameter_schema": {
                    "parameters": {"version": {"type": "string", "default": "1.18.35"}}
                },
            },
            expect=201,
        ).json()["data"]["id"]
        pat = create_pat(admin)
        accepted = submit(
            pat,
            service.version_id,
            ["opencode-pass"],
            execution_mode="agent",
            model_config_id=model_id,
            harness_version_id=harness_id,
            timeouts={"agent_sec": 300},
        )
        operation = wait_operation(pat, accepted["operation_id"], timeout=900)
        run_id = accepted["run_id"]
        trials = pat.get(f"/api/v1/runs/{run_id}/trials", expect=200).json()["data"]
        artifacts = pat.get(f"/api/v1/runs/{run_id}/artifacts", expect=200).json()["data"]
        summary = pat.get(f"/api/v1/runs/{run_id}/summary", expect=200).json()["data"]

        def download(path, trial=True):
            found = next(
                a
                for a in artifacts
                if a["relative_path"] == path and (a["trial_id"] is not None) == trial
            )
            return pat.get(f"/api/v1/artifacts/{found['id']}/download", expect=200).text

        print("\nOPERATION", operation["status"], "USAGE", summary.get("usage"))
        print(
            "TRIALS",
            [(t["state"], t["result"], [a["error"] for a in t["attempts"]]) for t in trials],
        )
        if any(a["relative_path"] == "agent/opencode" for a in artifacts):
            print("LIVE (head)\n" + download("agent/opencode")[:3000])
        if operation["status"] != "succeeded":
            print(download("runner/stderr.log", trial=False)[-4000:])
        assert operation["status"] == "succeeded", trials
        plan = json.loads(download("runner/network-plan.json", trial=False))
        host = endpoint.split("://", 1)[1].split("/", 1)[0]
        assert plan["opencode-pass"]["agent"]["effective"] == {
            "network_mode": "allowlist",
            "allowed_hosts": [host],
        }
        assert plan["opencode-pass"]["verifier"]["effective"]["network_mode"] == "no-network"
        # The model's host was reachable (the run scored); everything else was not.
        assert download("agent/probe.txt").split()[0] == "NET_CLOSED"
        assert download("verifier/probe.txt").split()[0] == "NET_CLOSED"
        assert "$ echo ok > /app/answer" in download("agent/opencode")
    stored = b"".join(
        p.read_bytes() for p in Path(service.settings.artifacts_dir).rglob("*") if p.is_file()
    )
    assert key.encode() not in stored
    assert labelled(service.worker.instance_id) == []
