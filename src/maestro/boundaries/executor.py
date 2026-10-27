"""Experiment executor boundary.

Per D-020 one call executes a whole run (Harbor: one Job). The executor reports
per-trial attempts, steps, results and artifacts through ``ExecutionSink``;
the platform owns trial state, scoring decisions, retry policy and leases.
"""

from dataclasses import dataclass, field
from typing import Protocol

from maestro.boundaries.environment import CleanupReport
from maestro.domain.setup_network import BUILT_IN, SetupNetwork

# The platform default scoring contract's metric (domain.scoring.DEFAULT_CONTRACT).
DEFAULT_METRIC_ID = "reward"

# Steps an attempt reports, and the trial phase each one implies (05 section 4).
# Collection has no upstream event in Harbor 0.24.0: it spans agent end to verification.
STEPS = {
    # Platform cache restore before the executor starts (02 section 7); never from Harbor.
    "cache": "preparing",
    "environment": "preparing",
    # Attachments copied in and installed during agent setup (03 section 7).
    "installing": "preparing",
    "agent": "executing",
    "collecting": "executing",
    "verifying": "verifying",
}


@dataclass(frozen=True)
class TrialSpec:
    trial_id: str
    task_key: str
    task_definition: dict
    repetition_index: int
    content_digest: str = ""
    material_digest: str = ""
    task_id: str = ""
    prepared_image: str | None = None
    """Local image prepared by the platform (02); the executor must not build or pull."""


@dataclass(frozen=True)
class RunSpec:
    run_id: str
    trials: tuple[TrialSpec, ...]
    concurrency: int
    effective_config: dict
    deadline_at: str
    labels: dict[str, str] = field(default_factory=dict)
    metric_id: str = DEFAULT_METRIC_ID
    """The reward the run's frozen scoring contract scores by (06 section 2); the executor
    maps its raw reward for ``raw_score`` from this key and reports the rest as metrics."""
    secrets: dict[str, str] = field(default_factory=dict, repr=False)
    """Credential values read at execution (03 section 5: the current value of the bound
    credential): ``model_api_key`` and ``param:<NAME>`` for secret author parameters.
    Never persisted, logged or put in configuration."""
    setup_network: SetupNetwork = BUILT_IN
    """Read when execution starts (N-08): proxy and variables for a model run's setup
    (harness install, attachment scripts) and the harness's setup time limit."""


@dataclass(frozen=True)
class AttemptOutcome:
    """Executor-native outcome mapped to platform policy fields (06 section 3)."""

    raw_score: float | None
    reward_valid: bool
    failure_cause: str | None = None
    exception_type: str | None = None
    message: str | None = None
    retry_recommended: bool = False
    grade_requested: bool = True
    verifier_status: str | None = None
    metrics: dict = field(default_factory=dict)
    usage: dict | None = None
    actual_model: dict | None = None
    detail: dict | None = None
    """Platform error detail, e.g. expected_digest of unavailable content (06 section 3)."""


class ExecutionSink(Protocol):
    async def runner_started(self, identity: dict) -> None:
        """Persist the process identity before anything else, for restart reconciliation."""
        ...

    async def heartbeat(self) -> None: ...

    async def attempt_started(
        self, trial_id: str, attempt_index: int, detail: dict | None = None
    ) -> None: ...

    async def phase_changed(self, trial_id: str, attempt_index: int, step: str) -> None:
        """``step`` is a key of ``STEPS``."""
        ...

    async def log(self, trial_id: str, attempt_index: int, source: str, text: str) -> None: ...

    async def artifact(
        self, trial_id: str, attempt_index: int, kind: str, relative_path: str, data: bytes
    ) -> None: ...

    async def run_artifact(self, kind: str, relative_path: str, data: bytes) -> None: ...

    async def attempt_finished(
        self, trial_id: str, attempt_index: int, outcome: AttemptOutcome, final: bool
    ) -> None:
        """``final=False`` means the executor will retry this trial."""
        ...


class CancelToken(Protocol):
    @property
    def cancelled(self) -> bool: ...

    async def wait(self) -> None: ...


@dataclass
class ExecutionReport:
    cleanup_complete: bool
    failure_code: str | None = None
    """Why trials still without a result ended, e.g. RUNNER_PROTOCOL_ERROR."""
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Leftover:
    """A run an earlier service process left claimed (05 section 8)."""

    run_id: str
    labels: dict[str, str]
    runner: dict | None


class Executor(Protocol):
    executor_id: str
    harness_adapters: dict[str, tuple[str, ...]]
    """Harness adapters this executor runs, each with the model protocols it can call
    (03 section 2). Oracle is the test mode and needs no adapter."""
    harness_parameters: dict[str, frozenset[str]]
    """Parameters each adapter accepts; a harness version may only declare these. An
    adapter missing here accepts any (test doubles)."""
    network_restriction: bool | None
    """Whether this host can restrict task networks, which every model run needs (U-18):
    None until probed. When False, model runs are refused at acceptance."""

    async def probe(self) -> None:
        """Learn host capabilities (``network_restriction``) before the worker starts."""
        ...

    async def execute(
        self, spec: RunSpec, sink: ExecutionSink, cancel: CancelToken
    ) -> ExecutionReport: ...

    async def cleanup(self, labels: dict[str, str]) -> CleanupReport:
        """Remove every execution resource carrying these labels and confirm release."""
        ...

    async def reconcile(self, leftover: Leftover) -> CleanupReport:
        """Stop a leftover Runner if it is provably ours, then clean its resources."""
        ...
