"""Shared executor helpers for tests."""

import asyncio
from dataclasses import replace

from maestro.boundaries.environment import CleanupReport
from maestro.boundaries.executor import RunSpec, TrialSpec
from maestro.execution.harbor import HarborExecutor
from maestro.execution.materials import tree_digest
from support import TESTS

FAKE_RUNNER = TESTS / "fake_runner.py"


class RecordingDocker:
    def __init__(self, released=True):
        self.calls = []
        self.released = released

    async def cleanup(self, labels):
        self.calls.append(labels)
        return CleanupReport(released=self.released)


class RecordingSink:
    def __init__(self, refuse_after: int | None = None):
        self.events = []
        self.files = {}
        self.texts = []
        self.refuse_after = refuse_after

    def _record(self, *event):
        self.events.append(event)
        if self.refuse_after is not None and len(self.events) > self.refuse_after:
            raise RuntimeError("lease lost")

    async def runner_started(self, identity):
        self._record("runner_started", identity["pid"] > 0, identity["start_ticks"] is not None)

    async def heartbeat(self):
        self._record("heartbeat")

    async def attempt_started(self, trial_id, attempt, detail=None):
        self._record("attempt_started", trial_id, attempt)

    async def phase_changed(self, trial_id, attempt, step):
        self._record("step", trial_id, attempt, step)

    async def log(self, trial_id, attempt, source, text):
        self.texts.append(text)
        self._record("log", trial_id, source)

    async def artifact(self, trial_id, attempt, kind, relative_path, data):
        self.files[(trial_id, attempt, relative_path)] = (kind, data)

    async def artifact_failed(self, trial_id, attempt, kind, relative_path, reason):
        self._record("artifact_failed", trial_id, attempt, relative_path, reason)

    async def run_artifact(self, kind, relative_path, data):
        self.files[("run", relative_path)] = (kind, data)

    async def attempt_finished(self, trial_id, attempt, outcome, final):
        self._record("finished", trial_id, attempt, outcome, final)


class Cancel:
    def __init__(self):
        self.event = asyncio.Event()

    @property
    def cancelled(self):
        return self.event.is_set()

    async def wait(self):
        await self.event.wait()


KEY = "sk-test-0123456789abcdefghijklmnop"


def make_executor(runner_python, materials, tmp_path, docker=None, **kw):
    kw.setdefault("term_grace", 1.0)
    kw.setdefault("silence_limit", 5.0)
    return HarborExecutor(
        runner_python, tmp_path / "work", materials, docker or RecordingDocker(), **kw
    )


def make_spec(materials, tmp_path, mode: str, trials: int = 2) -> RunSpec:
    task = tmp_path / "tasks" / mode
    task.mkdir(parents=True, exist_ok=True)
    (task / "task.toml").write_text(f'# {mode}\nschema_version = "1.4"\n')
    digest = tree_digest(task)
    material = materials.install(task, digest)
    return RunSpec(
        run_id=f"run_{mode.replace('-', '')}",
        trials=tuple(
            TrialSpec(f"trl_{i}", mode, {}, i, digest, material) for i in range(1, trials + 1)
        ),
        concurrency=2,
        effective_config={"execution_mode": "oracle"},
        deadline_at="2099-01-01T00:00:00Z",
        labels={"io.maestro.instance_id": "inst_test", "io.maestro.run_id": mode},
    )


def execute(executor, spec, sink, cancel=None):
    return asyncio.run(executor.execute(spec, sink, cancel or Cancel()))


def finished(sink):
    return {e[1]: e for e in sink.events if e[0] == "finished" and e[4]}


def agent_spec(materials, tmp_path) -> RunSpec:
    spec = make_spec(materials, tmp_path, "agent", trials=1)
    return replace(
        spec,
        effective_config={
            "execution_mode": "agent",
            "model": {
                "config_id": "mdl_1",
                "revision": 3,
                "provider": "relay",
                "protocol": "openai-compatible",
                "endpoint": "https://relay.example.test/v1",
                "model_id": "gpt-test",
                "params": {"temperature": 0},
                "credential_id": "cred_1",
            },
            "harness": {
                "version_id": "hrn_1",
                "name": "terminus",
                "version": "harbor-0.24.0",
                "agent": "terminus-2",
                "content_digest": "sha256:x",
                "params": {"max_turns": 5},
            },
            "parameters": {"REGION": {"value": "eu"}, "TOKEN": {"credential_id": "cred_2"}},
            "overrides": {"agent_timeout_sec": 120},
        },
        secrets={"model_api_key": KEY, "param:TOKEN": "grader-secret-value"},
    )


def attachment_spec(materials, tmp_path, store) -> RunSpec:
    from support.attachments import package, store_package

    digest = store_package(store, package())
    spec = make_spec(materials, tmp_path, "attachments")
    return replace(
        spec,
        effective_config={
            **agent_spec(materials, tmp_path).effective_config,
            "attachments": [
                {
                    "version_id": "att_1",
                    "name": "tools",
                    "label": "v1",
                    "content_digest": digest,
                    "effects": ["agent_tools"],
                    "provides": {"tools": [], "env": []},
                    "has_bootstrap": True,
                }
            ],
            "bootstrap_timeout_sec": 90,
            "parameters": {},
        },
        secrets={"model_api_key": KEY},
    )
