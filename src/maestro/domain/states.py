"""Independent state dimensions (section 4).

Trial states are the stored source; run-level summaries are derived with the
versioned rule below and never used for authorization or acceptance.
"""

from collections import Counter
from enum import StrEnum

AGGREGATION_VERSION = 1


class Role(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"


class UserStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class PatPermission(StrEnum):
    """What a PAT may do beyond reading team data, which every PAT may.

    A PAT's effective permissions are those it was granted that its owner's role allows.
    """

    EXPERIMENT = "experiment"  # own plans, attachments, runs, batches, collections
    DATASETS = "datasets"  # search, import, first preparation, admissions, caches
    MODELS = "models"  # model configurations and harness versions
    OPERATIONS = "operations"  # team policy, capacity, install network, deployment view
    OVERRIDE = "override"  # act on other members' objects: cancel, bin, see exports
    AUDIT = "audit"  # audit events and the recycle bin listing


ADMIN_PERMISSIONS = frozenset(
    {
        PatPermission.DATASETS,
        PatPermission.MODELS,
        PatPermission.OPERATIONS,
        PatPermission.OVERRIDE,
        PatPermission.AUDIT,
    }
)


class RunState(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    FINALIZING = "finalizing"
    FINISHED = "finished"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


RUN_TERMINAL = {RunState.FINISHED, RunState.CANCELLED, RunState.INTERRUPTED}


class TrialState(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    SCORED = "scored"
    EXECUTION_ERROR = "execution_error"
    NOT_STARTED = "not_started"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


TRIAL_TERMINAL = {
    TrialState.SCORED,
    TrialState.EXECUTION_ERROR,
    TrialState.NOT_STARTED,
    TrialState.CANCELLED,
    TrialState.INTERRUPTED,
}
TRIAL_ACTIVE = {TrialState.PREPARING, TrialState.EXECUTING, TrialState.VERIFYING}

# Trials may only move forward; terminal states are final (first accepted wins).
TRIAL_TRANSITIONS = {
    TrialState.QUEUED: {TrialState.PREPARING, TrialState.EXECUTING} | TRIAL_TERMINAL,
    TrialState.PREPARING: {TrialState.EXECUTING, TrialState.QUEUED} | TRIAL_TERMINAL,
    TrialState.EXECUTING: {TrialState.VERIFYING, TrialState.QUEUED} | TRIAL_TERMINAL,
    TrialState.VERIFYING: {TrialState.QUEUED} | TRIAL_TERMINAL,
}


def trial_can_move(current: str, target: str) -> bool:
    return TrialState(target) in TRIAL_TRANSITIONS.get(TrialState(current), set())


class AttemptState(StrEnum):
    STARTING = "starting"
    PREPARING = "preparing"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class ResultCompleteness(StrEnum):
    NONE = "none"
    PARTIAL = "partial"
    COMPLETE = "complete"


class ArtifactState(StrEnum):
    PENDING = "pending"
    SAVING = "saving"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    EXPIRED = "expired"


class CleanupState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    NOT_REQUIRED = "not_required"


class OperationStatus(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


OPERATION_TERMINAL = {
    OperationStatus.SUCCEEDED,
    OperationStatus.FAILED,
    OperationStatus.CANCELLED,
    OperationStatus.INTERRUPTED,
}


def trial_counts(states: list[str]) -> dict[str, int]:
    counts = Counter(TrialState(s) for s in states)
    terminal = sum(counts[s] for s in TRIAL_TERMINAL)
    return {
        "planned": len(states),
        "terminal": terminal,
        "scored": counts[TrialState.SCORED],
        "errors": counts[TrialState.EXECUTION_ERROR],
        "not_started": counts[TrialState.NOT_STARTED],
        "cancelled": counts[TrialState.CANCELLED],
        "interrupted": counts[TrialState.INTERRUPTED],
        "active": sum(counts[s] for s in TRIAL_ACTIVE),
        "queued": counts[TrialState.QUEUED],
    }


def result_completeness(counts: dict[str, int]) -> ResultCompleteness:
    if counts["scored"] == 0:
        return ResultCompleteness.NONE
    if counts["scored"] == counts["planned"]:
        return ResultCompleteness.COMPLETE
    return ResultCompleteness.PARTIAL


def display_phase(counts: dict[str, int], states: list[str]) -> RunState:
    """'Running once any trial executes; preparing if only preparation'."""
    present = set(states)
    if present & {TrialState.EXECUTING, TrialState.VERIFYING}:
        return RunState.RUNNING
    if TrialState.PREPARING in present:
        return RunState.PREPARING
    return RunState.QUEUED


def terminal_run_state(counts: dict[str, int], stop_reason: str | None) -> RunState:
    if stop_reason == "SERVICE_RESTARTED" or counts["interrupted"]:
        return RunState.INTERRUPTED
    if stop_reason in {"USER_CANCELLED", "ADMIN_CANCELLED"}:
        return RunState.CANCELLED
    return RunState.FINISHED


def operation_status_for(run_state: RunState, counts: dict[str, int]) -> OperationStatus:
    """Natural completion with any valid result succeeds; all errors fail."""
    if run_state == RunState.CANCELLED:
        return OperationStatus.CANCELLED
    if run_state == RunState.INTERRUPTED:
        return OperationStatus.INTERRUPTED
    if run_state == RunState.FINISHED:
        return OperationStatus.SUCCEEDED if counts["scored"] else OperationStatus.FAILED
    if run_state == RunState.QUEUED:
        return OperationStatus.ACCEPTED
    return OperationStatus.RUNNING
