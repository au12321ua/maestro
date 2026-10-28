"""Harbor executor: one Runner subprocess per run, speaking the Runner protocol (15 section 5).

The service never imports Harbor. The Runner lives in its own uv environment with
the locked Harbor version; this adapter starts it in a new process group, feeds it
the run on stdin, turns its NDJSON events into sink calls, maps Harbor exception
names to the platform policy, and always finishes with label-based cleanup.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import signal
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import ClassVar

from maestro.boundaries.environment import CleanupReport
from maestro.boundaries.executor import (
    STEPS,
    AttemptOutcome,
    CancelToken,
    ExecutionReport,
    ExecutionSink,
    Leftover,
    RunSpec,
)
from maestro.execution import process
from maestro.execution.agents import endpoint_host
from maestro.execution.attachments import AttachmentStore
from maestro.execution.docker import DockerResources
from maestro.execution.materials import TaskMaterials
from maestro.execution.policy import HARBOR_VERSION, MAPPING_ID, harbor_outcome, harbor_retry_config
from maestro.security.redaction import redact

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
EVENT_TYPES = {
    "started",
    "attempt_started",
    "phase_changed",
    "log",
    "attempt_finished",
    "heartbeat",
    "cancel_requested",
    "network_plan",
    "completed",
}
# Inherited by the Runner; nothing else, in particular no provider keys (09 section 5).
ENV_KEEP = (
    "PATH",
    "LANG",
    "LC_ALL",
    "TZ",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
)
STDERR_KEEP = 256 * 1024
# Evidence limits per execution attempt, enforced by the Runner while it archives (the
# same numbers as the configuration's defaults; a test keeps the two in step).
DEFAULT_ARTIFACT_LIMITS = {
    "max_files": 500,
    "max_file_bytes": 64 * 1024 * 1024,
    "max_total_bytes": 1024 * 1024 * 1024,
}


class ProtocolError(Exception):
    pass


def artifact_kind(relative_path: str) -> str:
    top = relative_path.split("/", 1)[0]
    return {"agent": "agent", "verifier": "verifier", "artifacts": "output"}.get(top, "trial")


def _redacted(data: bytes, limit: int, secrets: tuple[str, ...] = ()) -> bytes:
    """Logs and products are shown after basic redaction (U-10, 09 section 5).

    Any file that reads as text is redacted, whatever its name: a NUL byte marks binary,
    which stays untouched. Bytes that are not valid UTF-8 (a stderr tail cut mid-character)
    pass through unchanged around the redacted text. ``limit`` is the per-file artifact
    limit: the Runner collects nothing larger, so no collected text file goes unscanned."""
    if len(data) > limit or b"\0" in data:
        return data
    text = data.decode("utf-8", "surrogateescape")
    return redact(text, secrets).encode("utf-8", "surrogateescape")


@dataclass
class _Session:
    spec: RunSpec
    workdir: Path
    trial_ids: set[str]
    sequence: int = 0
    completed: dict | None = None
    started: dict | None = None
    open_attempts: set[tuple[str, int]] = field(default_factory=set)
    cancel_sent: bool = False
    failure: str | None = None
    secrets: tuple[str, ...] = ()
    network_plans: dict = field(default_factory=dict)


class _StderrTail:
    """Bounded head and tail; the Runner must never block on a full stderr pipe."""

    def __init__(self):
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = STDERR_KEEP - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        self.tail = (self.tail + chunk)[-STDERR_KEEP:]

    def render(self) -> bytes:
        if self.total <= len(self.head) + len(self.tail):
            return bytes(self.head + self.tail)
        skipped = self.total - len(self.head) - len(self.tail)
        return bytes(self.head) + f"\n[... {skipped} bytes omitted ...]\n".encode() + self.tail


class HarborExecutor:
    executor_id = f"harbor-{HARBOR_VERSION}"
    # The Runner's adapters (maestro_runner.agents.ADAPTERS; a test keeps them in step).
    harness_adapters: ClassVar[dict[str, tuple[str, ...]]] = {
        "terminus-2": ("anthropic", "openai", "openai-compatible"),
        "opencode": ("anthropic", "openai", "openai-compatible"),
        "opencode-2": ("anthropic", "openai", "openai-compatible"),
        # Codex speaks the Responses API only; Claude Code the Messages API only.
        "codex": ("openai",),
        "claude-code": ("anthropic",),
    }
    harness_parameters: ClassVar[dict[str, frozenset[str]]] = {
        "terminus-2": frozenset(
            {
                "max_turns",
                "parser_name",
                "enable_summarize",
                "proactive_summarization_threshold",
                "record_terminal_session",
                "tmux_pane_width",
                "tmux_pane_height",
                "interleaved_thinking",
            }
        ),
        "opencode": frozenset(),
        "opencode-2": frozenset(),
        "codex": frozenset(),
        "claude-code": frozenset({"max_turns"}),
    }
    # Model configuration parameters an adapter can pass on; one not listed takes any.
    harness_model_parameters: ClassVar[dict[str, frozenset[str]]] = {
        "codex": frozenset({"reasoning_effort", "reasoning_summary"}),
        "claude-code": frozenset({"reasoning_effort", "max_thinking_tokens"}),
    }
    # Adapters that install a program in the task container at a harness version's
    # upstream version: its npm package and the version last smoke-tested with a real
    # model (maestro_runner.agents.PACKAGES and TESTED).
    harness_upstream: ClassVar[dict[str, dict[str, str]]] = {
        "opencode": {"package": "opencode-ai", "tested": "1.18.35"},
        "opencode-2": {"package": "@opencode/cli", "tested": "2.2.0"},
        "codex": {"package": "@openai/codex", "tested": "0.165.0"},
        "claude-code": {"package": "@anthropic-ai/claude-code", "tested": "2.1.308"},
    }
    # Where each adapter calls the model from. Terminus-2 calls it from the Runner, so the
    # task container needs no network at all while the agent works.
    model_called_from: ClassVar[dict[str, str]] = {
        "terminus-2": "runner",
        "opencode": "container",
        "opencode-2": "container",
        "codex": "container",
        "claude-code": "container",
    }

    def __init__(
        self,
        runner_python: Path,
        work_root: Path,
        materials: TaskMaterials,
        docker: DockerResources | None = None,
        *,
        term_grace: float = 30.0,
        silence_limit: float = 120.0,
        heartbeat_sec: float = 10.0,
        tail_interval_sec: float = 0.5,
        log_cap_bytes: int = 1024 * 1024,
        artifact_limits: dict[str, int] = DEFAULT_ARTIFACT_LIMITS,
        attachments: AttachmentStore | None = None,
    ):
        self.artifact_limits = dict(artifact_limits)
        self.attachments = attachments
        self.runner_python = runner_python
        self.work_root = work_root
        self.materials = materials
        self.docker = docker or DockerResources()
        self.term_grace = term_grace
        self.silence_limit = silence_limit
        self.heartbeat_sec = heartbeat_sec
        self.tail_interval_sec = tail_interval_sec
        self.log_cap_bytes = log_cap_bytes
        self.network_restriction: bool | None = None

    @property
    def runner_id(self) -> str:
        return f"{self.executor_id}/{MAPPING_ID}"

    async def probe(self, timeout: float = 120.0) -> None:
        """Ask the Runner whether this host can restrict task networks. A probe that fails
        leaves the answer unknown: model runs are then still accepted, and the Runner's
        own check at the start of each run stays the last line (it refuses, never
        relaxes)."""
        env = {k: v for k, v in os.environ.items() if k in ENV_KEEP}
        env.setdefault("PATH", os.defpath)
        request = {"protocol_version": PROTOCOL_VERSION, "command": "probe"}
        try:
            proc = await asyncio.create_subprocess_exec(
                str(self.runner_python),
                "-I",
                "-m",
                "maestro_runner",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env | {"PYTHONDONTWRITEBYTECODE": "1"},
                start_new_session=True,
            )
            try:
                out, _err = await asyncio.wait_for(
                    proc.communicate(json.dumps(request).encode()), timeout
                )
            except TimeoutError:
                process.signal_group(proc.pid, signal.SIGKILL)
                await proc.wait()
                raise
        except (OSError, TimeoutError) as error:
            log.warning("network capability probe failed: %r", error)
            return
        for line in out.splitlines():
            with contextlib.suppress(ValueError):
                event = json.loads(line)
                payload = event.get("payload") or {}
                if event.get("type") == "result" and payload.get("status") == "ok":
                    self.network_restriction = bool(payload["data"]["network_restriction"])
        if self.network_restriction is None:
            log.warning("network capability probe gave no answer (exit %s)", proc.returncode)
        elif not self.network_restriction:
            log.warning(
                "this host cannot restrict task networks (nftables): model runs are refused"
            )

    def _workdir(self, run_id: str) -> Path:
        return self.work_root / run_id.lower()

    def _environment(self, workdir: Path) -> dict:
        env = {k: v for k, v in os.environ.items() if k in ENV_KEEP}
        env.setdefault("PATH", os.defpath)
        home = workdir / "home"
        tmp = workdir / "tmp"
        home.mkdir(exist_ok=True)
        tmp.mkdir(exist_ok=True)
        env.update(
            HOME=str(home),
            TMPDIR=str(tmp),
            PYTHONDONTWRITEBYTECODE="1",
            # LiteLLM would otherwise fetch a cost map from GitHub at import (M1).
            LITELLM_LOCAL_MODEL_COST_MAP="True",
        )
        return env

    def _agent(self, spec: RunSpec) -> dict:
        effective = spec.effective_config
        mode = effective.get("execution_mode")
        if mode == "oracle":
            return {"name": "oracle"}
        if mode != "agent":
            # Never let Harbor fall back to its default agent (15 section 6).
            raise ValueError(f"unsupported execution mode: {mode!r}")
        model, harness = effective["model"], effective["harness"]
        params = dict(harness["params"])
        version = harness.get("upstream_version")
        if version is None and harness["agent"] == "opencode":
            # Accepted before OpenCode's version was part of the harness version (N-09).
            version = params.pop("version", None) or "1.18.35"
        agent = {
            "adapter": harness["agent"],
            "model": {k: model[k] for k in ("protocol", "endpoint", "model_id", "params")},
            "params": params,
        }
        return agent if version is None else {**agent, "version": version}

    def _network(self, spec: RunSpec) -> dict | None:
        """Platform phase policy (U-18): model runs get the stricter of the author's and
        this; Oracle is a test mode and keeps the author's settings. Verification never
        needs the model; the agent phase needs it only when the harness calls the model
        from inside the container."""
        effective = spec.effective_config
        if effective.get("execution_mode") != "agent":
            return None
        agent = effective["harness"]["agent"]
        if self.model_called_from.get(agent) == "runner":
            phase = {"mode": "no-network"}
        else:
            phase = {"mode": "allowlist", "hosts": [endpoint_host(effective["model"]["endpoint"])]}
        return {"agent": phase, "verifier": {"mode": "no-network"}}

    async def _attachments(self, spec: RunSpec) -> tuple[list[dict], str | None]:
        """Stored packages in load order, each checked against its digest just before the
        Runner copies it; the name of the first one missing or changed, if any."""
        found = []
        for entry in spec.effective_config.get("attachments") or []:
            path = (
                await asyncio.to_thread(self.attachments.verified, entry["content_digest"])
                if self.attachments is not None
                else None
            )
            label = f"{entry['name']} {entry['version']}"
            if path is None:
                return [], label
            found.append(
                {
                    "name": entry["name"],
                    "version": entry["version"],
                    "content_digest": entry["content_digest"],
                    "path": str(path),
                    "bootstrap": entry["has_bootstrap"],
                }
            )
        return found, None

    @staticmethod
    def _task_env(spec: RunSpec) -> dict[str, str]:
        """Author parameter values; secret ones were read from their credential."""
        env = {}
        for name, value in (spec.effective_config.get("parameters") or {}).items():
            env[name] = value["value"] if "value" in value else spec.secrets[f"param:{name}"]
        return env

    # --- execution ------------------------------------------------------------------

    async def execute(
        self, spec: RunSpec, sink: ExecutionSink, cancel: CancelToken
    ) -> ExecutionReport:
        workdir = self._workdir(spec.run_id)
        shutil.rmtree(workdir, ignore_errors=True)
        workdir.mkdir(parents=True)
        tasks: dict[str, dict] = {}
        missing = []
        for trial in spec.trials:
            path = (
                self.materials.verified(trial.content_digest, trial.material_digest)
                if trial.material_digest
                else self.materials.path_for(trial.content_digest)
            )
            if path is None:
                missing.append(trial)
                continue
            entry = tasks.setdefault(
                trial.task_key,
                {
                    "task_key": trial.task_key,
                    "path": str(path),
                    "docker_image": trial.prepared_image,
                    "trial_ids": [],
                },
            )
            entry["trial_ids"].append(trial.trial_id)
        attachments, lost = await self._attachments(spec)
        if lost:
            # A purged or damaged package: never install other content in its place.
            missing = list(spec.trials)
            tasks = {}
        for trial in missing:
            # The worker restores material before execution; losing it after that
            # fails the trial instead of running unverified files.
            await sink.attempt_started(trial.trial_id, 1, {"runner": None})
            await sink.attempt_finished(
                trial.trial_id,
                1,
                AttemptOutcome(
                    raw_score=None,
                    reward_valid=False,
                    failure_cause="attachment_failed" if lost else "sandbox_failed",
                    exception_type="AttachmentContentMissing" if lost else "TaskMaterialMissing",
                    message=(
                        f"Attachment {lost} is no longer stored with its published content"
                        if lost
                        else "Task content is not in the local cache"
                    ),
                    grade_requested=False,
                    verifier_status="not_run",
                ),
                final=True,
            )
        if not tasks:
            shutil.rmtree(workdir, ignore_errors=True)
            return ExecutionReport(cleanup_complete=True)
        harness = bool(spec.effective_config.get("harness"))
        request = {
            "protocol_version": PROTOCOL_VERSION,
            "command": "execute",
            "run_id": spec.run_id,
            "output_dir": str(workdir),
            "agent": self._agent(spec),
            "network": self._network(spec),
            "overrides": spec.effective_config.get("overrides") or {},
            "secrets": (
                {"model_api_key": spec.secrets["model_api_key"]}
                if "model_api_key" in spec.secrets
                else {}
            ),
            "task_env": self._task_env(spec),
            "attachments": attachments,
            "bootstrap_timeout_sec": spec.effective_config.get("bootstrap_timeout_sec"),
            # Setup work only (harness install, attachment scripts); Oracle has none.
            "setup_env": spec.setup_network.container_env() if harness else {},
            "harness_setup_timeout_sec": (
                spec.setup_network.harness_setup_timeout_sec if harness else None
            ),
            "n_concurrent_trials": spec.concurrency,
            "retry": harbor_retry_config(),
            "tasks": list(tasks.values()),
            "labels": spec.labels,
            "log_cap_bytes": self.log_cap_bytes,
            "artifact_limits": self.artifact_limits,
            "heartbeat_sec": self.heartbeat_sec,
            "tail_interval_sec": self.tail_interval_sec,
        }
        session = _Session(spec, workdir, {t for task in tasks.values() for t in task["trial_ids"]})
        # A proxy address with a password in it is a secret too.
        session.secrets = tuple(v for v in spec.secrets.values() if v) + tuple(
            {v for v in spec.setup_network.container_env().values() if "@" in v}
        )
        proc = await asyncio.create_subprocess_exec(
            str(self.runner_python),
            "-I",
            "-m",
            "maestro_runner",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
            env=self._environment(workdir),
            start_new_session=True,
            limit=16 * 1024 * 1024,
        )
        stderr = _StderrTail()
        drain = asyncio.create_task(self._drain(proc, stderr))
        stopper = asyncio.create_task(self._stop_on_cancel(proc, cancel, session))
        try:
            await sink.runner_started(
                process.identity(proc.pid)
                | {"runner_id": self.runner_id, "protocol_version": PROTOCOL_VERSION}
            )
            proc.stdin.write(json.dumps(request).encode())
            await proc.stdin.drain()
            proc.stdin.close()
            await self._read(proc, sink, session)
        except ProtocolError as error:
            log.warning("run %s: runner protocol error: %s", spec.run_id, error)
            session.failure = "RUNNER_PROTOCOL_ERROR"
            await self._terminate(proc, graceful=False)
        except TimeoutError:
            log.warning("run %s: runner silent for %ss", spec.run_id, self.silence_limit)
            session.failure = "RUNNER_UNRESPONSIVE"
            await self._terminate(proc, graceful=False)
        except asyncio.CancelledError:
            # Service shutdown: stop the Runner now; Docker cleanup still runs below.
            await self._terminate(proc, graceful=False)
            with contextlib.suppress(Exception):
                await asyncio.shield(self.docker.cleanup(spec.labels))
            raise
        except BaseException:
            # The sink refused (e.g. a stale lease): stop executing before propagating.
            await self._terminate(proc, graceful=True)
            await self.docker.cleanup(spec.labels)
            raise
        finally:
            stopper.cancel()
            if proc.returncode is None:
                await self._terminate(proc, graceful=False)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await drain
        return await self._finish(proc, sink, session, stderr)

    async def _finish(self, proc, sink, session: _Session, stderr: _StderrTail) -> ExecutionReport:
        spec = session.spec
        if stderr.total:
            with contextlib.suppress(Exception):
                shown = await asyncio.to_thread(
                    _redacted,
                    stderr.render(),
                    self.artifact_limits["max_file_bytes"],
                    session.secrets,
                )
                await sink.run_artifact("runner", "runner/stderr.log", shown)
        if session.network_plans:
            # Evidence of what each phase was allowed: author's, platform's, effective.
            with contextlib.suppress(Exception):
                await sink.run_artifact(
                    "runner",
                    "runner/network-plan.json",
                    json.dumps(session.network_plans, indent=2, sort_keys=True).encode(),
                )
        completed = session.completed or {}
        failure = session.failure
        if failure is None and completed.get("status") in {"rejected", "runner_error"}:
            failure = completed.get("code") or "RUNNER_FAILED"
        if failure is None and not session.completed and not session.cancel_sent:
            failure = "RUNNER_EXITED"
        report = await self.docker.cleanup(spec.labels)
        if report.released:
            shutil.rmtree(session.workdir, ignore_errors=True)
        return ExecutionReport(
            cleanup_complete=report.released,
            failure_code=failure,
            detail={
                "exit_code": proc.returncode,
                "runner": session.started,
                "completed": completed,
                "cleanup_remaining": report.remaining,
            },
        )

    async def _drain(self, proc, stderr: _StderrTail) -> None:
        while chunk := await proc.stderr.read(65536):
            stderr.add(chunk)

    async def _stop_on_cancel(self, proc, cancel: CancelToken, session: _Session) -> None:
        await cancel.wait()
        session.cancel_sent = True
        await self._terminate(proc, graceful=True)

    async def _terminate(self, proc, graceful: bool) -> None:
        """Cooperative SIGTERM, then SIGKILL after the grace period (15 section 7)."""
        if proc.returncode is not None:
            return
        if graceful:
            process.signal_group(proc.pid, signal.SIGTERM)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), self.term_grace)
                return
        process.signal_group(proc.pid, signal.SIGKILL)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), 10)

    # --- events -----------------------------------------------------------------------

    async def _read(self, proc, sink: ExecutionSink, session: _Session) -> None:
        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), self.silence_limit)
            except ValueError as error:  # longer than the stream limit
                raise ProtocolError(str(error)) from None
            if not line:
                return
            event = self._parse(line, session)
            await self._dispatch(event, sink, session)
            if event["type"] == "completed":
                session.completed = event["payload"]

    def _parse(self, line: bytes, session: _Session) -> dict:
        try:
            event = json.loads(line)
        except ValueError:
            raise ProtocolError("not JSON") from None
        if not isinstance(event, dict) or not isinstance(event.get("payload"), dict):
            raise ProtocolError("malformed envelope")
        if event.get("protocol_version") != PROTOCOL_VERSION:
            raise ProtocolError("protocol version mismatch")
        if event.get("run_id") != session.spec.run_id:
            raise ProtocolError("event for another run")
        if event.get("sequence") != session.sequence + 1:
            raise ProtocolError(f"sequence {event.get('sequence')} after {session.sequence}")
        if event.get("type") not in EVENT_TYPES:
            raise ProtocolError(f"unknown event type {event.get('type')!r}")
        payload = event["payload"]
        if "trial_id" in payload and payload["trial_id"] not in session.trial_ids:
            raise ProtocolError("event for an unknown trial")
        if "attempt" in payload and not (
            isinstance(payload["attempt"], int) and payload["attempt"] >= 1
        ):
            raise ProtocolError("invalid attempt number")
        session.sequence += 1
        return event

    async def _dispatch(self, event: dict, sink: ExecutionSink, session: _Session) -> None:
        kind, p = event["type"], event["payload"]
        if kind == "started":
            session.started = {
                k: p.get(k) for k in ("harbor_version", "runner_version", "n_trials")
            }
            if p.get("harbor_version") != HARBOR_VERSION:
                raise ProtocolError(f"Runner has Harbor {p.get('harbor_version')}")
        elif kind == "heartbeat":
            await sink.heartbeat()
        elif kind == "network_plan":
            session.network_plans[str(p.get("task_key"))] = p.get("plan")
        elif kind == "attempt_started":
            session.open_attempts.add((p["trial_id"], p["attempt"]))
            await sink.attempt_started(
                p["trial_id"],
                p["attempt"],
                {
                    "runner_id": self.runner_id,
                    "harbor_trial_name": p.get("harbor_trial_name"),
                    "harbor_trial_id": p.get("harbor_trial_id"),
                },
            )
        elif kind == "phase_changed":
            if p.get("step") not in STEPS:
                raise ProtocolError(f"unknown step {p.get('step')!r}")
            await sink.phase_changed(p["trial_id"], p["attempt"], p["step"])
        elif kind == "log":
            await sink.log(
                p["trial_id"],
                p["attempt"],
                str(p.get("source")),
                redact(str(p.get("text")), session.secrets),
            )
        elif kind == "attempt_finished":
            await self._attempt_finished(p, sink, session)

    async def _attempt_finished(self, p: dict, sink: ExecutionSink, session: _Session) -> None:
        trial_id, attempt = p["trial_id"], p["attempt"]
        archive = p.get("archive")
        if archive:
            root = (session.workdir / archive).resolve()
            if not root.is_relative_to(session.workdir.resolve()):
                raise ProtocolError("archive outside the work directory")
            for entry in p.get("files") or []:
                if entry.get("skipped"):
                    continue
                path = (root / entry["path"]).resolve()
                if not path.is_relative_to(root):
                    raise ProtocolError("artifact path escapes the archive")
                data = await asyncio.to_thread(path.read_bytes)
                if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
                    log.warning("run %s: %s changed after the manifest", trial_id, entry["path"])
                    continue
                await sink.artifact(
                    trial_id,
                    attempt,
                    artifact_kind(entry["path"]),
                    entry["path"],
                    await asyncio.to_thread(
                        _redacted, data, self.artifact_limits["max_file_bytes"], session.secrets
                    ),
                )
        outcome = harbor_outcome(
            p.get("exception_type"),
            redact(p.get("exception_message") or "", session.secrets) or None,
            p.get("rewards"),
            bool(p.get("verifier_ran")),
            session.spec.metric_id,
        )
        if isinstance(p.get("usage"), dict):
            outcome = replace(outcome, usage=p["usage"])
        installs = p.get("attachments")
        if isinstance(installs, list) and installs:
            # The Runner's own record (not a file the container could have written).
            await sink.artifact(
                trial_id,
                attempt,
                "trial",
                "maestro/attachments.json",
                json.dumps(installs, indent=2, sort_keys=True).encode(),
            )
            failed = next(
                (i for i in installs if isinstance(i, dict) and i.get("status") != "ok"), None
            )
            if failed is not None:
                outcome = replace(
                    outcome,
                    detail={
                        "attachment": failed.get("name"),
                        "version": failed.get("version"),
                        "status": failed.get("status"),
                        "exit_code": failed.get("exit_code"),
                    },
                )
        session.open_attempts.discard((trial_id, attempt))
        await sink.attempt_finished(trial_id, attempt, outcome, final=not p.get("will_retry"))

    # --- cleanup and reconciliation -------------------------------------------------

    async def cleanup(self, labels: dict[str, str]) -> CleanupReport:
        return await self.docker.cleanup(labels)

    async def reconcile(self, leftover: Leftover) -> CleanupReport:
        if process.kill_recorded(leftover.runner):
            log.warning("run %s: killed leftover Runner group", leftover.run_id)
        report = await self.docker.cleanup(leftover.labels)
        if report.released:
            shutil.rmtree(self._workdir(leftover.run_id), ignore_errors=True)
        return report
