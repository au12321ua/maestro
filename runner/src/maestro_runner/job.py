"""Execute one platform run as one Harbor Job (D-020) and report it over the protocol.

Harbor's trial hooks give real-time phases; per-attempt evidence is archived in the
END hook because Harbor deletes a failed attempt's directory before an in-job retry
(M1, V-12). Live logs come from the trial directories Docker mounts on the host.

Those mounted directories are writable from inside the container (C-07 / U-16), so
everything under them is untrusted: links are never followed (a link could point at any
host file), only regular files are read, and the size limits apply while copying.
"""

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import traceback
from collections import defaultdict
from importlib.metadata import version
from pathlib import Path

from maestro_runner import agents, network, protocol
from maestro_runner.protocol import emit

# Harbor hook -> platform step. Collection has no hook: it is the span between
# agent end and verification start (M1).
STEPS = {
    "environment-start": "environment",
    "agent-start": "agent",
    "agent-end": "collecting",
    "verification-start": "verifying",
}
LOG_DIRS = ("agent", "verifier")
LOG_SUFFIXES = (".txt", ".log")
# The per-attempt file count, per-file size and total size limits come with every run
# (``artifact_limits`` in the request, from the service's configuration).
# Skipped entries are listed so the evidence gap is visible, but a runaway task cannot
# grow the manifest (one protocol line) without bound; the rest is only counted.
MAX_LISTED_SKIPS = 200
MESSAGE_LIMIT = 4000
# Agents that keep an ATIF trajectory (Terminus-2 rewrites it after every turn) are
# followed live by turning new steps into readable log text (U-07).
TRAJECTORY = "agent/trajectory.json"
# OpenCode streams JSON events to this file; they are shown as readable text instead.
EVENTS = "agent/opencode.txt"
TRAJECTORY_MAX_BYTES = 32 * 1024 * 1024
STEP_PART_CHARS = 20000


def _text(value) -> str:
    """ATIF content is a string or a list of parts; only the text is shown."""
    if isinstance(value, list):
        return "\n".join(str(part.get("text", "")) for part in value if isinstance(part, dict))
    return "" if value is None else str(value)


def _render_event(event: dict, state: dict) -> str:
    """One OpenCode event as text (``state`` counts the steps of this attempt)."""

    def cut(text: str) -> str:
        if len(text) <= STEP_PART_CHARS:
            return text
        return text[:STEP_PART_CHARS] + f"\n[... {len(text) - STEP_PART_CHARS} characters ...]"

    kind = event.get("type")
    part = event.get("part") if isinstance(event.get("part"), dict) else {}
    if kind == "step_start":
        state["step"] = state.get("step", 0) + 1
        return f"--- step {state['step']} · agent ---\n"
    if kind in {"text", "reasoning"} and (text := _text(part.get("text")).strip()):
        return cut(text if kind == "text" else f"(thinking) {text}") + "\n"
    if kind == "tool_use":
        tool = part.get("state") if isinstance(part.get("state"), dict) else {}
        given = tool.get("input") if isinstance(tool.get("input"), dict) else {}
        command = given.get("command")
        lines = [
            cut(f"$ {command.rstrip()}")
            if isinstance(command, str)
            else cut(f"> {part.get('tool')} {json.dumps(given, ensure_ascii=False)}")
        ]
        if output := _text(tool.get("output")).strip():
            lines.append(cut(output))
        return "\n".join(lines) + "\n"
    if kind == "error":
        return cut(f"! error: {json.dumps(event.get('error'), ensure_ascii=False)}") + "\n"
    return ""


def _render_step(step: dict) -> str:
    def cut(text: str) -> str:
        if len(text) <= STEP_PART_CHARS:
            return text
        return text[:STEP_PART_CHARS] + f"\n[... {len(text) - STEP_PART_CHARS} characters ...]"

    lines = [f"--- step {step.get('step_id')} · {step.get('source')} ---"]
    if message := _text(step.get("message")).strip():
        lines.append(cut(message))
    for call in step.get("tool_calls") or []:
        arguments = call.get("arguments") if isinstance(call, dict) else None
        arguments = arguments if isinstance(arguments, dict) else {}
        keys = arguments.get("keystrokes")
        if isinstance(keys, str):
            lines.append(cut("$ " + keys.rstrip()))
        else:
            lines.append(cut(f"> {call.get('function_name')} {arguments}"))
    for result in (step.get("observation") or {}).get("results") or []:
        if content := _text(result.get("content") if isinstance(result, dict) else None).strip():
            lines.append(cut(content))
    return "\n".join(lines) + "\n"


# The platform's list of names a task parameter may not take (maestro.execution.materials).
RESERVED_ENV = re.compile(
    r"(PATH|HOME|USER|SHELL|PWD|TMPDIR|TZ|LANG|LC_\w+|LD_\w+|PYTHON\w*|DOCKER_\w+|COMPOSE_\w+"
    r"|HARBOR_\w+|MAESTRO_\w+|LITELLM_\w+|SSL_CERT_\w+|(?i:https?_proxy|no_proxy|all_proxy))"
)


class Rejected(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _labels_override(
    path: Path, labels: dict[str, str], label_build: bool, prepared: bool, sidecar: bool = False
) -> Path:
    """Platform labels on the main service, its network, the egress-control sidecar when
    the network is restricted, and (when Harbor builds it) its image.

    Cleanup after a killed Runner finds every resource by these labels (05 section 7).
    Prepared images are never pulled: a missing one must fail, not reach a registry.
    """
    body = "".join(f"      {key}: {value}\n" for key, value in sorted(labels.items()))
    text = "services:\n  main:\n    init: true\n"
    if prepared:
        text += "    pull_policy: never\n"
    text += "    labels:\n" + body
    if label_build:
        text += "    build:\n      labels:\n" + body.replace("      ", "        ")
    if sidecar:
        text += f"  {network.SIDECAR}:\n    labels:\n" + body
    text += "networks:\n  default:\n    labels:\n" + body
    path.write_text(text)
    return path


def _builds_image(task_dir: Path) -> bool:
    from harbor.models.task.config import TaskConfig as TaskFile

    config = TaskFile.model_validate_toml((task_dir / "task.toml").read_text())
    return not config.environment.docker_image


def _with_image(copy: Path, image: str) -> None:
    """Point the task copy at the platform-prepared image, so Harbor neither builds nor
    pulls it (M1 TECH-03)."""
    from harbor.models.task.config import TaskConfig as TaskFile

    config = TaskFile.model_validate_toml((copy / "task.toml").read_text())
    config.environment.docker_image = image
    (copy / "task.toml").write_text(config.model_dump_toml())


def _entries(top: Path) -> list[tuple[Path, os.stat_result]]:
    """Regular files and links below ``top`` in path order, with their own (lstat) data.

    Directories are entered only when they really are directories: a link to one is
    reported, never followed. Devices, pipes and sockets are left out.
    """
    if top.is_symlink() or not top.is_dir():
        return []
    found = []
    pending = [top]
    while pending:
        try:
            with os.scandir(pending.pop()) as scan:
                entries = list(scan)
        except OSError:
            continue
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode):
                pending.append(Path(entry.path))
            elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                found.append((Path(entry.path), info))
    return sorted(found, key=lambda item: item[0])


def _open_below(path: Path, root: Path):
    """Open a regular file for reading, or None when it is not one that lives under ``root``.

    The container can swap names between listing and opening. O_NOFOLLOW refuses a link as
    the last component, O_NONBLOCK keeps a planted pipe from blocking the Runner, and the
    descriptor's real path catches a link in any directory above the file.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    handle = os.fdopen(fd, "rb")  # owns the descriptor from here on
    try:
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
        inside = Path(os.readlink(f"/proc/self/fd/{fd}")).is_relative_to(root.resolve())
    except OSError:
        regular = inside = False
    if not (regular and inside):
        handle.close()
        return None
    return handle


def _copy(handle, target: Path, limit: int) -> tuple[str, int] | None:
    """Copy at most ``limit`` bytes and return (sha256, size); None, with nothing kept,
    when the file turns out to be longer."""
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    within = True
    with target.open("wb") as sink:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            size += len(chunk)
            if size > limit:
                within = False
                break
            digest.update(chunk)
            sink.write(chunk)
    if not within:
        target.unlink()
        return None
    return digest.hexdigest(), size


def _archive(source: Path, target: Path, limits: dict) -> tuple[list[dict], int]:
    """Copy a trial directory's regular files and describe them: the manifest entries and
    how many skipped entries were left off the list. ``limits`` holds ``max_files``,
    ``max_file_bytes`` and ``max_total_bytes``.

    Nothing is followed or copied as a link, and the limits apply on the way, so a runaway
    task cannot fill the disk a second time. Skipped files stay in the manifest, with a
    reason, so a missing file is never silent.
    """
    target.mkdir(parents=True, exist_ok=True)
    files: list[dict] = []
    skipped = copied = total = 0

    def skip(relative: str, size: int, reason: str) -> None:
        nonlocal skipped
        skipped += 1
        if skipped <= MAX_LISTED_SKIPS:
            files.append({"path": relative, "size": size, "skipped": True, "reason": reason})

    for path, info in _entries(source):
        relative = path.relative_to(source).as_posix()
        if stat.S_ISLNK(info.st_mode):
            skip(relative, 0, "SYMLINK")
        elif copied >= limits["max_files"]:
            skip(relative, info.st_size, "TOO_MANY_FILES")
        elif info.st_size > limits["max_file_bytes"]:
            skip(relative, info.st_size, "TOO_LARGE")
        elif total + info.st_size > limits["max_total_bytes"]:
            skip(relative, info.st_size, "TOTAL_LIMIT")
        elif (handle := _open_below(path, source)) is None:
            skip(relative, info.st_size, "UNREADABLE")
        else:
            try:
                with handle:
                    done = _copy(
                        handle,
                        target / relative,
                        min(limits["max_file_bytes"], limits["max_total_bytes"] - total),
                    )
            except OSError:
                (target / relative).unlink(missing_ok=True)
                skip(relative, info.st_size, "COPY_FAILED")
                continue
            if done is None:
                skip(relative, info.st_size, "TOO_LARGE")
                continue
            digest, size = done
            copied += 1
            total += size
            files.append({"path": relative, "size": size, "sha256": digest})
    return files, max(0, skipped - MAX_LISTED_SKIPS)


def _timing(value) -> dict | None:
    if value is None:
        return None
    return {
        "started_at": value.started_at.isoformat() if value.started_at else None,
        "finished_at": value.finished_at.isoformat() if value.finished_at else None,
    }


class Execution:
    def __init__(self, spec: dict):
        self.spec = spec
        self.out = Path(spec["output_dir"])
        self.jobs_dir = self.out / "jobs"
        self.attempts_dir = self.out / "attempts"
        self.retry = spec["retry"]
        self.limits = spec["artifact_limits"]
        self.cancelling = False
        # Harbor trial name -> platform trial id; retries keep the trial name (V-12).
        self.bound: dict[str, str] = {}
        self.unbound: dict[str, list[str]] = {}
        self.task_by_path: dict[str, str] = {}
        self.attempt: dict[str, int] = defaultdict(int)
        self.live: set[str] = set()
        self.offsets: dict[Path, int] = {}
        self.log_bytes: dict[tuple[str, int], int] = defaultdict(int)
        self.trajectory_steps: dict[Path, int] = {}
        self.trajectory_seen: dict[Path, tuple] = {}
        self.event_state: dict[Path, dict] = {}
        # Harbor trial name -> the current attempt's attachment install records.
        self.installs: dict[str, list] = {}
        self.finished = 0

    # --- configuration -----------------------------------------------------------

    def _config(self):
        from harbor.models.job.config import JobConfig, RetryConfig
        from harbor.models.trial.config import (
            AgentConfig,
            EnvironmentConfig,
            TaskConfig,
            VerifierConfig,
        )

        tasks = self.spec["tasks"]
        counts = {len(task["trial_ids"]) for task in tasks}
        if len(counts) != 1:
            raise Rejected("RUNNER_INPUT_INVALID", "every task needs the same trial count")
        task_dirs = []
        prepared = 0
        platform = self.spec.get("network")
        restricted = False
        for index, task in enumerate(tasks):
            path = Path(task["path"]).resolve()
            if not (path / "task.toml").is_file():
                raise Rejected("TASK_MATERIAL_MISSING", f"no task.toml for {task['task_key']}")
            if task.get("docker_image") or platform:
                # A private copy: the cached material is shared and never changed.
                copy = self.out / "tasks" / str(index)
                shutil.copytree(path, copy, symlinks=True)
                path = copy
            if task.get("docker_image"):
                _with_image(path, task["docker_image"])
                prepared += 1
            if platform:
                plan = network.tighten(path, platform)
                if network.restricted(plan):
                    restricted = True
                    unrouted = network.unrouted_services(path)
                    if unrouted:
                        # Fail closed: these services would bypass the restriction.
                        raise Rejected(
                            "NETWORK_POLICY_UNSUPPORTED",
                            f"{task['task_key']}: compose services {unrouted} set their own "
                            "networks, which the platform network policy cannot restrict",
                        )
                emit("network_plan", task_key=task["task_key"], plan=plan)
            self.task_by_path[str(path)] = task["task_key"]
            self.unbound[task["task_key"]] = list(task["trial_ids"])
            task_dirs.append(path)
        if restricted:
            # Never fall back to public: a host that cannot restrict refuses the run.
            if not network.supported():
                raise Rejected(
                    "NETWORK_POLICY_UNSUPPORTED",
                    "this host cannot enforce the platform network policy (nftables)",
                )
            network.harden()
        label_build = all(_builds_image(path) for path in task_dirs)
        override = _labels_override(
            self.out / "labels.compose.yaml",
            self.spec["labels"],
            label_build,
            prepared == len(tasks),
            sidecar=restricted,
        )
        overrides = self.spec.get("overrides") or {}
        agent = self.spec["agent"]
        if agent.get("adapter"):
            try:
                agent = agents.agent_config(agent)
            except (agents.InputError, KeyError) as error:
                raise Rejected("RUNNER_INPUT_INVALID", f"agent: {error}") from None
        elif agent.get("name") != "oracle":
            # Harbor's default agent is oracle; never fall back to it implicitly (15 section 6).
            raise Rejected("RUNNER_INPUT_INVALID", "agent must be explicit")
        agent = {**agent, "override_timeout_sec": overrides.get("agent_timeout_sec")}
        # The harness's own install and attachment scripts run in setup, with the
        # deployment's setup network (proxy, package mirrors) when it has one.
        agents.INSTALL["env"] = dict(self.spec.get("setup_env") or {})
        if self.spec.get("attachments"):
            if not self.spec["agent"].get("adapter"):
                raise Rejected("RUNNER_INPUT_INVALID", "attachments need a harness agent")
            self._attachments()
        harness_limit = self.spec.get("harness_setup_timeout_sec")
        if self.spec["agent"].get("adapter") and (agents.ATTACHMENTS or harness_limit):
            agent["override_setup_timeout_sec"] = agents.setup_timeout(
                harness_limit
                or agents.agent_class(self.spec["agent"]["adapter"]).DEFAULT_SETUP_TIMEOUT_SEC
            )
        return JobConfig(
            job_name="job",
            jobs_dir=self.jobs_dir,
            n_attempts=counts.pop(),
            n_concurrent_trials=self.spec["n_concurrent_trials"],
            quiet=True,
            retry=RetryConfig(
                max_retries=self.retry["max_retries"],
                include_exceptions=None,
                exclude_exceptions=set(self.retry["exclude_exceptions"]),
                min_wait_sec=self.retry["min_wait_sec"],
                wait_multiplier=self.retry["wait_multiplier"],
                max_wait_sec=self.retry["max_wait_sec"],
            ),
            environment=EnvironmentConfig(
                type="docker",
                delete=True,
                extra_docker_compose=[override],
                override_cpus=overrides.get("cpus"),
                override_memory_mb=overrides.get("memory_mb"),
            ),
            verifier=VerifierConfig(override_timeout_sec=overrides.get("verifier_timeout_sec")),
            agents=[AgentConfig(**agent)],
            tasks=[TaskConfig(path=path) for path in task_dirs],
        )

    def _attachments(self) -> None:
        """Hand the packages to the agent wrapper, each checked against its digest here
        too: the Runner never installs content other than what was published."""
        loaded = []
        for attachment in self.spec["attachments"]:
            try:
                files = agents.describe(Path(attachment["path"]))
            except (OSError, ValueError) as error:
                raise Rejected("ATTACHMENT_CONTENT_MISMATCH", str(error)) from None
            if agents.content_digest(files) != attachment["content_digest"]:
                raise Rejected(
                    "ATTACHMENT_CONTENT_MISMATCH",
                    f"{attachment['name']} differs from its published content",
                )
            loaded.append({**attachment, "files": files})
        agents.ATTACHMENTS[:] = loaded
        agents.INSTALL.update(
            timeout_sec=self.spec.get("bootstrap_timeout_sec"), report=self._installed
        )

    def _installed(self, name: str, records: list | None) -> None:
        """From the agent wrapper: None when installing starts, then the records."""
        trial_id = self.bound.get(name)
        if trial_id is None:
            return
        if records is None:
            emit(
                "phase_changed",
                trial_id=trial_id,
                attempt=self.attempt[name],
                step="installing",
            )
        else:
            self.installs[name] = [dict(r) for r in records]

    def _will_retry(self, exception_type: str | None, attempt: int) -> bool:
        """Mirror of Harbor 0.24.0 TrialQueue retry rules for the settings we pass."""
        if exception_type is None or self.cancelling or exception_type == "CancelledError":
            return False
        if exception_type in self.retry["exclude_exceptions"]:
            return False
        return attempt <= self.retry["max_retries"]

    # --- hooks ---------------------------------------------------------------------

    def _trial_id(self, event) -> str:
        name = event.trial_name
        if name not in self.bound:
            task_key = self.task_by_path[str(Path(event.config.task.path).resolve())]
            self.bound[name] = self.unbound[task_key].pop(0)
        return self.bound[name]

    async def on_event(self, event) -> None:
        try:
            self._handle(event)
        except Exception:  # noqa: BLE001 - a hook failure must not break Harbor's trial
            traceback.print_exc()

    def _handle(self, event) -> None:
        kind = event.event.value
        name = event.trial_name
        trial_id = self._trial_id(event)
        if kind == "start":
            self.attempt[name] += 1
            self.live.add(name)
            self.installs.pop(name, None)
            emit(
                "attempt_started",
                trial_id=trial_id,
                attempt=self.attempt[name],
                harbor_trial_name=name,
                harbor_trial_id=str(event.trial_id),
            )
        elif kind in STEPS:
            emit(
                "phase_changed",
                trial_id=trial_id,
                attempt=self.attempt[name],
                step=STEPS[kind],
                harbor_at=event.timestamp.isoformat(),
            )
        elif kind == "end":
            self._finish(event, trial_id)

    def _finish(self, event, trial_id: str) -> None:
        name = event.trial_name
        attempt = self.attempt[name]
        self.tail(only=name)
        self.live.discard(name)
        result = event.result
        trial_dir = self.jobs_dir / "job" / name
        archive = self.attempts_dir / trial_id / str(attempt)
        archived = trial_dir.is_dir()
        files, unlisted = _archive(trial_dir, archive, self.limits) if archived else ([], 0)
        exc = result.exception_info
        exception_type = exc.exception_type if exc else None
        rewards = result.verifier_result.rewards if result.verifier_result else None
        context = result.agent_result
        self.finished += 1
        emit(
            "attempt_finished",
            trial_id=trial_id,
            attempt=attempt,
            exception_type=exception_type,
            exception_message=(exc.exception_message[:MESSAGE_LIMIT] if exc else None),
            rewards=rewards,
            verifier_ran=result.verifier is not None,
            will_retry=self._will_retry(exception_type, attempt),
            archive=archive.relative_to(self.out).as_posix() if archived else None,
            files=files,
            skipped_unlisted=unlisted,
            attachments=self.installs.pop(name, None),
            usage=(
                {
                    "input_tokens": context.n_input_tokens,
                    "cache_tokens": context.n_cache_tokens,
                    "output_tokens": context.n_output_tokens,
                    "cost_usd": context.cost_usd,
                }
                if context is not None
                else None
            ),
            timing={
                "environment_setup": _timing(result.environment_setup),
                "agent_setup": _timing(result.agent_setup),
                "agent_execution": _timing(result.agent_execution),
                "verifier": _timing(result.verifier),
            },
        )

    # --- live logs -------------------------------------------------------------------

    def tail(self, only: str | None = None) -> None:
        """Forward new bytes of trial log files; a retry recreates files, so offsets reset."""
        cap = self.spec["log_cap_bytes"]
        for name in sorted(self.live if only is None else {only}):
            trial_id = self.bound.get(name)
            if trial_id is None:
                continue
            attempt = self.attempt[name]
            root = self.jobs_dir / "job" / name
            for directory in LOG_DIRS:
                for path, info in _entries(root / directory):
                    if path.suffix not in LOG_SUFFIXES or stat.S_ISLNK(info.st_mode):
                        continue
                    if path == root / EVENTS:
                        continue  # rendered by _tail_events
                    start = self.offsets.get(path, 0)
                    size = info.st_size
                    if size < start:
                        start = 0
                    if size == start:
                        continue
                    self.offsets[path] = size
                    used = self.log_bytes[(trial_id, attempt)]
                    if used >= cap:
                        continue
                    handle = _open_below(path, root)
                    if handle is None:
                        continue
                    with handle:
                        handle.seek(start)
                        data = handle.read(min(size - start, cap - used))
                    self.log_bytes[(trial_id, attempt)] += len(data)
                    emit(
                        "log",
                        trial_id=trial_id,
                        attempt=attempt,
                        source=path.relative_to(root).as_posix(),
                        text=data.decode("utf-8", "replace"),
                        truncated=self.log_bytes[(trial_id, attempt)] >= cap,
                    )
            self._tail_trajectory(root, trial_id, attempt, cap)
            self._tail_events(root, trial_id, attempt, cap)

    def _tail_trajectory(self, root: Path, trial_id: str, attempt: int, cap: int) -> None:
        """New steps of the agent's trajectory as text. The file is rewritten whole each
        turn (and anew on a retry), so steps are counted rather than bytes."""

        path = root / TRAJECTORY
        try:
            info = path.lstat()
        except OSError:
            return
        if not stat.S_ISREG(info.st_mode) or info.st_size > TRAJECTORY_MAX_BYTES:
            return
        key = (path, info.st_size, info.st_mtime_ns)
        if self.trajectory_seen.get(path) == key:
            return
        handle = _open_below(path, root)
        if handle is None:
            return
        with handle:
            raw = handle.read(TRAJECTORY_MAX_BYTES + 1)
        try:
            steps = json.loads(raw).get("steps") or []
        except (ValueError, AttributeError):
            return  # caught mid-write; the next tick reads it again
        self.trajectory_seen[path] = key
        seen = self.trajectory_steps.get(path, 0)
        if len(steps) < seen:
            seen = 0  # a retry started a new trajectory
        self.trajectory_steps[path] = len(steps)
        new = [s for s in steps[seen:] if isinstance(s, dict)]
        used = self.log_bytes[(trial_id, attempt)]
        if not new or used >= cap:
            return
        text = "".join(_render_step(step) for step in new)
        data = text.encode()[: cap - used]
        self.log_bytes[(trial_id, attempt)] += len(data)
        emit(
            "log",
            trial_id=trial_id,
            attempt=attempt,
            source="agent/trajectory",
            text=data.decode("utf-8", "replace"),
            truncated=self.log_bytes[(trial_id, attempt)] >= cap,
        )

    def _tail_events(self, root: Path, trial_id: str, attempt: int, cap: int) -> None:
        """New complete lines of OpenCode's event stream as readable text."""
        path = root / EVENTS
        try:
            info = path.lstat()
        except OSError:
            return
        if not stat.S_ISREG(info.st_mode):
            return
        start = self.offsets.get(path, 0)
        if info.st_size < start:  # a retry started a new stream
            start = 0
            self.event_state.pop(path, None)
        if info.st_size == start:
            return
        handle = _open_below(path, root)
        if handle is None:
            return
        with handle:
            handle.seek(start)
            data = handle.read(min(info.st_size - start, TRAJECTORY_MAX_BYTES))
        complete = data[: data.rfind(b"\n") + 1]
        if not complete:
            return  # a line still being written
        self.offsets[path] = start + len(complete)
        state = self.event_state.setdefault(path, {})
        text = ""
        for line in complete.decode("utf-8", "replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                text += _render_event(event, state)
        used = self.log_bytes[(trial_id, attempt)]
        if not text or used >= cap:
            return
        out = text.encode()[: cap - used]
        self.log_bytes[(trial_id, attempt)] += len(out)
        emit(
            "log",
            trial_id=trial_id,
            attempt=attempt,
            source="agent/opencode",
            text=out.decode("utf-8", "replace"),
            truncated=self.log_bytes[(trial_id, attempt)] >= cap,
        )

    async def _tail_forever(self) -> None:
        while True:
            await asyncio.sleep(self.spec["tail_interval_sec"])
            try:
                self.tail()
            except OSError:
                traceback.print_exc()

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.spec["heartbeat_sec"])
            emit("heartbeat", attempts_finished=self.finished, live=len(self.live))

    # --- run -------------------------------------------------------------------------

    async def run(self) -> str:
        from harbor.job import Job
        from harbor.trial.hooks import TrialEvent

        self.attempts_dir.mkdir(parents=True, exist_ok=True)
        config = self._config()
        job = await Job.create(config)
        for trial_event in TrialEvent:
            job.add_hook(trial_event, self.on_event)
        emit(
            "started",
            harbor_version=version("harbor"),
            runner_version=version("maestro-runner"),
            n_trials=len(job),
            pid=os.getpid(),
        )
        background = [
            asyncio.create_task(self._tail_forever()),
            asyncio.create_task(self._heartbeat()),
        ]
        try:
            await job.run()
            return "completed"
        except asyncio.CancelledError:
            return "cancelled"
        finally:
            for task in background:
                task.cancel()


def runner_version() -> str:
    return version("maestro-runner")


def check_spec(spec: dict) -> None:
    if spec.get("protocol_version") != protocol.PROTOCOL_VERSION:
        raise Rejected("PROTOCOL_VERSION_UNSUPPORTED", "unsupported protocol version")
    if spec.get("command") != "execute":
        raise Rejected("RUNNER_INPUT_INVALID", "unknown command")
    required = (
        "run_id",
        "output_dir",
        "agent",
        "tasks",
        "labels",
        "retry",
        "n_concurrent_trials",
        "log_cap_bytes",
        "heartbeat_sec",
        "tail_interval_sec",
        "artifact_limits",
    )
    missing = [key for key in required if key not in spec]
    if missing:
        raise Rejected("RUNNER_INPUT_INVALID", f"missing fields: {missing}")
    limits = spec["artifact_limits"]
    names = ("max_files", "max_file_bytes", "max_total_bytes")
    if not isinstance(limits, dict) or set(limits) != set(names):
        raise Rejected("RUNNER_INPUT_INVALID", f"artifact_limits needs exactly {list(names)}")
    for name in names:
        value = limits[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise Rejected(
                "RUNNER_INPUT_INVALID", f"artifact_limits.{name} must be a positive integer"
            )
    if limits["max_file_bytes"] > limits["max_total_bytes"]:
        raise Rejected(
            "RUNNER_INPUT_INVALID", "a single file cannot be allowed more than the total"
        )
    try:
        network.check_platform(spec.get("network"))
    except ValueError as error:
        raise Rejected("RUNNER_INPUT_INVALID", str(error)) from None
    for name in ("secrets", "task_env", "setup_env"):
        value = spec.get(name) or {}
        if not isinstance(value, dict) or not all(
            isinstance(k, str) and k.isidentifier() and isinstance(v, str) for k, v in value.items()
        ):
            raise Rejected("RUNNER_INPUT_INVALID", f"{name} must map names to strings")
    # Author parameter values join this process's environment: never one it relies on.
    reserved = sorted(k for k in spec.get("task_env") or {} if RESERVED_ENV.fullmatch(k))
    if reserved:
        raise Rejected("RUNNER_INPUT_INVALID", f"task_env holds reserved names: {reserved}")
    attachments = spec.get("attachments") or []
    fields = {"name": str, "version": str, "content_digest": str, "path": str, "bootstrap": bool}
    if not isinstance(attachments, list) or not all(
        isinstance(a, dict)
        and set(a) == set(fields)
        and all(isinstance(a[k], kind) for k, kind in fields.items())
        for a in attachments
    ):
        raise Rejected("RUNNER_INPUT_INVALID", f"attachments need exactly {sorted(fields)}")
    harness_limit = spec.get("harness_setup_timeout_sec")
    if harness_limit is not None and (
        isinstance(harness_limit, bool)
        or not isinstance(harness_limit, int | float)
        or harness_limit <= 0
    ):
        raise Rejected(
            "RUNNER_INPUT_INVALID", "harness_setup_timeout_sec must be a positive number"
        )
    limit = spec.get("bootstrap_timeout_sec")
    if any(a["bootstrap"] for a in attachments) and (
        isinstance(limit, bool) or not isinstance(limit, int | float) or limit <= 0
    ):
        raise Rejected("RUNNER_INPUT_INVALID", "bootstrap_timeout_sec must be a positive number")
    overrides = spec.get("overrides") or {}
    kinds = {
        "cpus": int,
        "memory_mb": int,
        "agent_timeout_sec": (int, float),
        "verifier_timeout_sec": (int, float),
    }
    for name, value in overrides.items():
        if name not in kinds or isinstance(value, bool) or not isinstance(value, kinds[name]):
            raise Rejected("RUNNER_INPUT_INVALID", f"overrides.{name} is not valid")
