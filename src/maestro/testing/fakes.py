"""Fakes for the four boundaries (C4). Used by tests and local demonstrations only."""

import asyncio
import hashlib
import io
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from maestro.boundaries.dataset_source import (
    MISMATCH,
    NOT_FOUND,
    FetchedTask,
    SourceError,
    SourceTask,
    SourceVersion,
)
from maestro.boundaries.environment import (
    MISMATCH as PREP_MISMATCH,
)
from maestro.boundaries.environment import (
    TRANSIENT,
    CleanupReport,
    PreparationError,
    PreparationSpec,
    PreparedImage,
    PreparedOutput,
)
from maestro.boundaries.executor import (
    AttemptOutcome,
    CancelToken,
    ExecutionReport,
    ExecutionSink,
    Leftover,
    RunSpec,
)
from maestro.core.digests import digest_json
from maestro.execution.materials import image_key
from maestro.execution.policy import harbor_outcome


class FakeDatasetSource:
    """In-memory source. Each task is a small Harbor task directory; failures are
    injected per content digest as a queue of kinds consumed one call at a time."""

    kind = "fake"

    def __init__(self):
        self.versions: dict[tuple[str, str, str], SourceVersion] = {}
        self.files: dict[str, dict[str, str]] = {}
        self.fail_fetch: dict[str, list[str]] = {}
        self.gone: set[str] = set()
        self.altered: set[str] = set()
        self.fetches: list[str] = []
        self.credentials: list[str | None] = []

    @staticmethod
    def task_files(namespace: str, name: str, key: str, revision: str = "") -> dict[str, str]:
        return {
            "task.toml": (
                "[agent]\ntimeout_sec = 60.0\n[verifier]\ntimeout_sec = 60.0\n"
                "[environment]\ncpus = 1\nmemory_mb = 2048\n"
            ),
            "instruction.md": f"Solve {key}{revision}\n",
            "environment/Dockerfile": f"FROM scratch\n# {namespace}/{name}/{key}\n",
            "solution/solve.sh": "echo ok\n",
            "tests/test.sh": "echo 1 > /logs/verifier/reward.txt\n",
        }

    def add(
        self,
        namespace: str,
        name: str,
        ref: str,
        task_keys: list[str],
        *,
        revision: str = "",
        files: dict[str, dict[str, str]] | None = None,
    ) -> SourceVersion:
        """``revision`` changes every task's content: the same ref then moves (F-11)."""
        tasks = []
        for key in task_keys:
            content = (files or {}).get(key) or self.task_files(namespace, name, key, revision)
            digest = digest_json({"files": content})
            self.files[digest] = content
            tasks.append(SourceTask(key, digest, {"key": key}))
        version = SourceVersion(
            namespace=namespace,
            name=name,
            upstream_ref=ref,
            ref_kind="digest" if ref.startswith("sha256:") else "tag",
            resolved_digest=digest_json([t.content_digest for t in tasks]),
            tasks=tuple(tasks),
            description=f"Fake dataset {namespace}/{name}",
        )
        self.versions[(namespace, name, ref)] = version
        return version

    async def search(self, query: str, credential: str | None) -> list[dict]:
        self.credentials.append(credential)
        seen = {(ns, n) for (ns, n, _) in self.versions if query in f"{ns}/{n}"}
        return [{"namespace": ns, "name": n, "visibility": "public"} for ns, n in sorted(seen)]

    async def list_versions(self, namespace: str, name: str, credential: str | None) -> list:
        return [
            {"ref": r, "tags": [r], "resolved_digest": v.resolved_digest, "yanked": False}
            for (ns, n, r), v in self.versions.items()
            if (ns, n) == (namespace, name)
        ]

    async def describe(self, namespace: str, name: str, ref: str, credential: str | None):
        self.credentials.append(credential)
        version = self.versions.get((namespace, name, ref))
        if version is None:
            for (ns, n, _), candidate in self.versions.items():
                if (ns, n) == (namespace, name) and candidate.resolved_digest == ref:
                    return candidate
            raise SourceError(NOT_FOUND, f"{namespace}/{name}@{ref} not found")
        return version

    async def fetch(self, task: SourceTask, destination: Path, credential: str | None):
        self.fetches.append(task.content_digest)
        self.credentials.append(credential)
        queue = self.fail_fetch.get(task.content_digest)
        if queue:
            kind = queue.pop(0)
            raise SourceError(kind, f"fake {kind} failure", {"request_id": "fake-req"})
        if task.content_digest in self.gone:
            raise SourceError(NOT_FOUND, "task version not found")
        content = dict(self.files[task.content_digest])
        if task.content_digest in self.altered:
            content["instruction.md"] += "tampered\n"
        destination.mkdir(parents=True)
        for relative, text in content.items():
            path = destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        if task.content_digest in self.altered:
            raise SourceError(
                MISMATCH,
                "downloaded task differs from the fixed digest",
                {"expected_digest": task.content_digest, "actual_digest": "sha256:" + "0" * 64},
            )
        return FetchedTask(destination, task.content_digest)


def scored(score: float) -> AttemptOutcome:
    return AttemptOutcome(raw_score=score, reward_valid=True, verifier_status="ok")


def failed(cause: str, *, retry: bool = False, exception: str = "FakeError") -> AttemptOutcome:
    return AttemptOutcome(
        raw_score=None,
        reward_valid=False,
        failure_cause=cause,
        exception_type=exception,
        message=f"fake {cause}",
        retry_recommended=retry,
    )


@dataclass
class FakeExecutor:
    """Scripted per task key: a list of outcomes, one per attempt.

    A dict entry is a verifier's rewards, scored like Harbor's by the run's metric
    (``RunSpec.metric_id``). A ``"hang"`` entry blocks until cancellation. Retries
    happen when an outcome recommends it and attempts remain, mirroring Job-internal
    retries (D-020). Cleanup and reconciliation calls are recorded instead of
    touching Docker.
    """

    script: dict[str, list] = field(default_factory=dict)
    default: AttemptOutcome = field(default_factory=lambda: scored(1.0))
    max_attempts: int = 3
    step_delay: float = 0.0
    executor_id: str = "fake"
    # A stand-in harness that speaks every model protocol and calls nothing.
    harness_adapters: dict = field(
        default_factory=lambda: {"fake-agent": ("anthropic", "openai", "openai-compatible")}
    )
    harness_model_parameters: dict = field(default_factory=dict)
    harness_upstream: dict = field(default_factory=dict)
    started: asyncio.Event = field(default_factory=asyncio.Event)
    network_restriction: bool | None = True
    on_trial: Callable | None = None
    cleaned: list = field(default_factory=list)
    reconciled: list = field(default_factory=list)
    release: bool = True
    specs: list = field(default_factory=list)

    async def probe(self) -> None:
        pass

    async def execute(self, spec: RunSpec, sink: ExecutionSink, cancel: CancelToken):
        self.specs.append(spec)
        semaphore = asyncio.Semaphore(spec.concurrency)
        await sink.runner_started({"pid": 0, "runner_id": self.executor_id})

        async def run_trial(trial):
            async with semaphore:
                plan = list(self.script.get(trial.task_key, [self.default]))
                for attempt in range(1, self.max_attempts + 1):
                    if cancel.cancelled:
                        return
                    outcome = plan[min(attempt - 1, len(plan) - 1)]
                    if isinstance(outcome, dict):
                        outcome = harbor_outcome(None, None, outcome, True, spec.metric_id)
                    await sink.attempt_started(trial.trial_id, attempt)
                    self.started.set()
                    for step in ("environment", "agent", "verifying"):
                        await sink.phase_changed(trial.trial_id, attempt, step)
                        if outcome == "hang" and step == "agent":
                            await cancel.wait()
                            return
                        if self.step_delay:
                            await asyncio.sleep(self.step_delay)
                        if cancel.cancelled:
                            return
                    await sink.log(
                        trial.trial_id,
                        attempt,
                        "agent/fake.txt",
                        f"attempt {attempt} of {trial.task_key}\n",
                    )
                    await sink.artifact(
                        trial.trial_id,
                        attempt,
                        "output",
                        "result.txt",
                        f"{trial.task_key}:{outcome.raw_score}".encode(),
                    )
                    retry = outcome.retry_recommended and attempt < self.max_attempts
                    await sink.attempt_finished(trial.trial_id, attempt, outcome, final=not retry)
                    if not retry:
                        return

        await asyncio.gather(*(run_trial(trial) for trial in spec.trials))
        report = await self.cleanup(spec.labels)
        return ExecutionReport(cleanup_complete=report.released)

    async def cleanup(self, labels: dict[str, str]) -> CleanupReport:
        self.cleaned.append(labels)
        return CleanupReport(released=self.release)

    async def reconcile(self, leftover: Leftover) -> CleanupReport:
        self.reconciled.append(leftover)
        return await self.cleanup(leftover.labels)


class FakeEnvironment:
    """Images in memory: ``registry`` holds what was pushed (by digest), ``images`` what is
    present locally. Per task key, ``fail_prepare`` / ``fail_restore`` queue failure kinds;
    ``gate`` blocks preparation until set; ``nondeterministic`` tasks rebuild to a
    different digest; ``lose`` drops a preparation's images from the registry."""

    location_id = "fake-host"

    def __init__(self):
        self.images: set[str] = set()
        self.registry: set[str] = set()
        self.fail_prepare: dict[str, list[str]] = {}
        self.fail_restore: dict[str, list[str]] = {}
        self.nondeterministic: set[str] = set()
        self.gate: asyncio.Event | None = None
        self.prepares: list[str] = []
        self.restores: list[str] = []
        self.loads: list[str] = []
        self.reused: list[str] = []
        self.produced: dict[str, PreparedImage] = {}
        self.outputs: dict[str, PreparedOutput] = {}  # by spec digest, for tests
        self.started = asyncio.Event()
        self.builds = 0

    def ref(self, digest: str) -> str:
        return f"fake-registry/prep@{digest}"

    async def _work(self, spec: PreparationSpec, progress, queue: dict[str, list[str]]) -> int:
        self.started.set()
        await progress("building")
        if self.gate is not None:
            await self.gate.wait()
        kinds = queue.get(spec.task_key)
        if kinds:
            kind = kinds.pop(0)
            raise PreparationError(kind, f"fake {kind} preparation failure", {"stage": "build"})
        if not (spec.task_dir / "task.toml").is_file():
            raise PreparationError(TRANSIENT, "task material missing")
        self.builds += 1
        return self.builds if spec.task_key in self.nondeterministic else 0

    @staticmethod
    def _entries(spec: PreparationSpec) -> list[dict]:
        return (spec.spec or {}).get("images") or [
            {"role": "environment/main", "source": "build", "definition_digest": spec.spec_digest}
        ]

    def _digest(self, entry: dict, salt: int) -> str:
        return digest_json({"key": image_key(entry), "salt": salt})

    async def prepare(self, spec: PreparationSpec, progress) -> PreparedOutput:
        self.prepares.append(spec.task_key)
        salt = await self._work(spec, progress, self.fail_prepare)
        images = []
        for entry in self._entries(spec):
            key = image_key(entry)
            known = spec.known.get(key) or self.produced.get(key)
            if known is not None and known.digest in self.registry:
                self.reused.append(entry["role"])
                digest, detail = known.digest, known.detail
            else:
                digest, detail = self._digest(entry, salt), {"method": entry["source"]}
            self.registry.add(digest)
            self.images.add(digest)
            image = PreparedImage(entry["role"], digest, self.ref(digest), key, detail)
            self.produced.setdefault(key, image)
            images.append(image)
        output = PreparedOutput(
            tuple(images), {"setup_network_revision": spec.setup_network.revision}
        )
        self.outputs[spec.spec_digest] = output
        return output

    async def restore(self, spec: PreparationSpec, expected: PreparedOutput, progress):
        self.restores.append(spec.task_key)
        if await self.check(expected):
            return expected
        digests = [i.digest for i in expected.images]
        if all(d in self.registry for d in digests) and not self.fail_restore.get(spec.task_key):
            self.started.set()
            await progress("pulling")
            if self.gate is not None:
                await self.gate.wait()
            self.loads.append(spec.task_key)
            self.images.update(digests)
            return expected
        salt = await self._work(spec, progress, self.fail_restore)
        entries = {e["role"]: e for e in self._entries(spec)}
        for image in expected.images:
            if image.digest in self.registry:
                continue
            actual = self._digest(entries[image.role], salt)
            if actual != image.digest:
                raise PreparationError(
                    PREP_MISMATCH,
                    "rebuilt image differs",
                    {"expected_digest": image.digest, "actual_digest": actual},
                )
            self.registry.add(actual)
        self.images.update(digests)
        return expected

    async def check(self, expected: PreparedOutput) -> bool:
        return bool(expected.images) and all(i.digest in self.images for i in expected.images)

    async def evict(self, expected: PreparedOutput, keep: frozenset[str] = frozenset()) -> None:
        for image in expected.images:
            if image.digest not in keep:
                self.images.discard(image.digest)

    async def discard(self, expected: PreparedOutput, keep: frozenset[str] = frozenset()) -> None:
        await self.evict(expected, keep)
        for image in expected.images:
            if image.digest not in keep:
                self.registry.discard(image.digest)

    def present(self, spec_digest: str) -> bool:
        """Are the images first prepared for this spec all local?"""
        output = self.outputs.get(spec_digest)
        return output is not None and all(i.digest in self.images for i in output.images)

    def lose(self, spec_digest: str) -> None:
        """The registry loses the images first prepared for this spec."""
        for image in self.outputs[spec_digest].images:
            self.registry.discard(image.digest)


class MemoryArtifactStore:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.written: dict[str, float] = {}  # when each key was last put; tests may backdate
        self.fail_put = False

    def put(self, key: str, data: bytes) -> tuple[str, int]:
        if self.fail_put:
            raise OSError("fake storage failure")
        self.files[key] = data
        self.written[key] = time.time()
        return "sha256:" + hashlib.sha256(data).hexdigest(), len(data)

    def get(self, key: str) -> bytes:
        return self.files[key]

    def delete(self, key: str) -> None:
        self.files.pop(key, None)

    def exists(self, key: str) -> bool:
        return key in self.files

    def open(self, key: str):
        return io.BytesIO(self.files[key])

    def keys(self):
        return iter(list(self.files))

    def modified_at(self, key: str) -> float | None:
        return self.written.get(key, 0.0) if key in self.files else None

    def discard_partial(self) -> int:
        return 0
