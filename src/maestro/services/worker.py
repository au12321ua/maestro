"""In-process trial scheduler with durable stage permits and run leases."""

import asyncio
import contextlib
import hashlib
import logging
import os
import secrets
from dataclasses import replace
from datetime import timedelta

from sqlalchemy import func, insert, null, select, update

from maestro.boundaries.executor import (
    STEPS,
    AttemptOutcome,
    Executor,
    Leftover,
    RunSpec,
    TrialSpec,
)
from maestro.boundaries.storage import ArtifactStore
from maestro.core import clock
from maestro.core.digests import digest_json
from maestro.core.errors import MaestroError
from maestro.core.ids import new_id
from maestro.db import schema as t
from maestro.db.engine import Database
from maestro.domain.scoring import snapshot_contract
from maestro.domain.states import (
    RUN_TERMINAL,
    TRIAL_ACTIVE,
    TRIAL_TERMINAL,
    ArtifactState,
    AttemptState,
    CleanupState,
    RunState,
    TrialState,
    trial_can_move,
)
from maestro.execution.attachments import installed_packages
from maestro.execution.docker import LABEL_ATTEMPT, LABEL_INSTANCE, LABEL_RUN, LABEL_TRIAL
from maestro.execution.policy import (
    MAX_RETRIES,
    POLICY_VERSION,
    RETRY_MAX_WAIT_SEC,
    RETRY_MIN_WAIT_SEC,
    RETRY_WAIT_MULTIPLIER,
    harbor_retry_config,
    legal_score,
)
from maestro.execution.resources import stage_resources
from maestro.security.redaction import redact
from maestro.services import scheduling, setup_network, updates
from maestro.services.artifacts import expiry_for, stamp_expiry
from maestro.services.capacity import (
    HostCapacity,
    in_use,
    provider_limits,
    provider_of,
    waiting_reason,
)
from maestro.services.instance import instance_id
from maestro.services.preparation import (
    PreparationFailure,
    Preparer,
    gather_or_cancel,
    load_context,
)
from maestro.services.runs import finalize, finish_unstarted, refresh_operation

log = logging.getLogger(__name__)
CLAIM_SCAN = 200
CLEANUP_RETRY_SECONDS = 300.0


class StaleLease(MaestroError):
    def __init__(self):
        super().__init__("STALE_EXECUTION_LEASE", "Execution lease is no longer valid", 409)


class CredentialUnavailable(Exception):
    def __init__(self, credential_id: str):
        super().__init__(credential_id)
        self.credential_id = credential_id


class _Cancel:
    def __init__(self):
        self.event = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()

    async def wait(self) -> None:
        await self.event.wait()


def trial_outcome(outcome: AttemptOutcome) -> tuple[TrialState, str | None]:
    """Scored needs a legal number and reward_valid; 0 is a real score."""
    if outcome.reward_valid and legal_score(outcome.raw_score):
        return TrialState.SCORED, outcome.failure_cause
    return TrialState.EXECUTION_ERROR, outcome.failure_cause or "verifier_failed"


def storage_key(run_id: str, owner: str, relative_path: str) -> str:
    """Opaque and stable per path, so a final copy replaces the live log it continues."""
    name = hashlib.sha256(relative_path.encode()).hexdigest()[:32]
    return f"runs/{run_id.lower()}/{owner.lower()}/{name}"


class RunSink:
    """Commits executor events for one claimed run, each in a short transaction."""

    def __init__(self, worker: "Worker", run_id: str, lease: str):
        self.worker = worker
        self.db = worker.db
        self.run_id = run_id
        self.lease = lease
        self.attempt_ids: dict[tuple[str, int], str] = {}
        # The setup network revision the executor's attempts are given.
        self.setup_network_revision: int | None = None
        # Attempts the platform recorded itself (failed cache restores) before the
        # executor started; executor attempt numbers continue after them.
        self.platform_attempts: dict[str, int] = {}

    def _index(self, trial_id: str, attempt_index: int) -> int:
        return self.platform_attempts.get(trial_id, 0) + attempt_index

    def _check_run(self, conn):
        run = conn.execute(
            select(t.runs.c.lease_token, t.runs.c.state).where(t.runs.c.id == self.run_id)
        ).one()
        if run.lease_token != self.lease or run.state in RUN_TERMINAL:
            raise StaleLease()

    def _check(self, conn, trial_id: str):
        self._check_run(conn)
        trial = (
            conn.execute(
                select(t.trials).where(t.trials.c.id == trial_id, t.trials.c.run_id == self.run_id)
            )
            .mappings()
            .first()
        )
        if trial is None:
            raise MaestroError("UNKNOWN_TRIAL", "Trial does not belong to this run", 409)
        return trial

    def _move(self, conn, trial, target: TrialState, **values) -> bool:
        if not trial_can_move(trial["state"], target):
            return False  # First accepted terminal state wins; late events are ignored.
        conn.execute(
            update(t.trials).where(t.trials.c.id == trial["id"]).values(state=target, **values)
        )
        return True

    def _attempt(self, conn, trial_id: str, attempt_index: int) -> str:
        key = (trial_id, attempt_index)
        if key not in self.attempt_ids:
            found = conn.execute(
                select(t.execution_attempts.c.id).where(
                    t.execution_attempts.c.trial_id == trial_id,
                    t.execution_attempts.c.attempt_index == attempt_index,
                )
            ).scalar()
            if found is None:
                raise MaestroError("UNKNOWN_ATTEMPT", "Attempt was never started", 409)
            self.attempt_ids[key] = found
        return self.attempt_ids[key]

    async def runner_started(self, identity: dict) -> None:
        with self.db.write() as conn:
            self._check_run(conn)
            conn.execute(
                update(t.runs)
                .where(t.runs.c.id == self.run_id)
                .values(runner_json=identity, heartbeat_at=clock.to_text(clock.now()))
            )

    async def heartbeat(self) -> None:
        with self.db.write() as conn:
            self._check_run(conn)
            conn.execute(
                update(t.runs)
                .where(t.runs.c.id == self.run_id)
                .values(heartbeat_at=clock.to_text(clock.now()))
            )

    async def attempt_started(
        self, trial_id: str, attempt_index: int, detail: dict | None = None
    ) -> None:
        attempt_index = self._index(trial_id, attempt_index)
        with self.db.write() as conn:
            trial = self._check(conn, trial_id)
            if trial["state"] in TRIAL_TERMINAL:
                return
            attempt_id = new_id("att")
            at = clock.to_text(clock.now())
            conn.execute(
                insert(t.execution_attempts).values(
                    id=attempt_id,
                    trial_id=trial_id,
                    attempt_index=attempt_index,
                    worker_id=self.worker.worker_id,
                    service_epoch=self.worker.epoch,
                    lease_token=self.lease,
                    runner_id=(detail or {}).get("runner_id") or self.worker.executor.executor_id,
                    executor_ref_json=detail,
                    setup_network_revision=self.setup_network_revision,
                    state=AttemptState.STARTING,
                    phase="starting",
                    steps_json=[{"step": "starting", "at": at}],
                    started_at=at,
                )
            )
            self.attempt_ids[(trial_id, attempt_index)] = attempt_id
            self._move(conn, trial, TrialState.PREPARING, phase="starting")
            refresh_operation(conn, self.run_id)

    async def phase_changed(self, trial_id: str, attempt_index: int, step: str) -> None:
        attempt_index = self._index(trial_id, attempt_index)
        target = TrialState(STEPS[step])
        with self.db.write() as conn:
            trial = self._check(conn, trial_id)
            attempt_id = self._attempt(conn, trial_id, attempt_index)
            if (
                conn.execute(
                    select(t.execution_attempts.c.ended_at).where(
                        t.execution_attempts.c.id == attempt_id
                    )
                ).scalar()
                is not None
            ):
                return
            steps = conn.execute(
                select(t.execution_attempts.c.steps_json).where(
                    t.execution_attempts.c.id == attempt_id
                )
            ).scalar()
            conn.execute(
                update(t.execution_attempts)
                .where(t.execution_attempts.c.id == attempt_id)
                .values(
                    phase=step,
                    state=AttemptState(target.value),
                    steps_json=[*(steps or []), {"step": step, "at": clock.to_text(clock.now())}],
                )
            )
            if trial["state"] != target:
                self._move(conn, trial, target, phase=step)
            elif trial["state"] not in TRIAL_TERMINAL:
                conn.execute(update(t.trials).where(t.trials.c.id == trial_id).values(phase=step))
            refresh_operation(conn, self.run_id)

    async def log(self, trial_id: str, attempt_index: int, source: str, text: str) -> None:
        # ``artifact`` applies the attempt offset.
        await self.artifact(
            trial_id, attempt_index, "log", source, redact(text).encode(), append=True
        )

    async def artifact(self, trial_id, attempt_index, kind, relative_path, data, append=False):
        attempt_index = self._index(trial_id, attempt_index)
        with self.db.read() as conn:
            self._check(conn, trial_id)
            attempt_id = self._attempt(conn, trial_id, attempt_index)
        await self._store(
            storage_key(self.run_id, attempt_id, relative_path),
            {"trial_id": trial_id, "attempt_id": attempt_id},
            kind,
            relative_path,
            data,
            append,
        )

    async def run_artifact(self, kind: str, relative_path: str, data: bytes) -> None:
        await self._store(
            storage_key(self.run_id, "run", relative_path),
            {"trial_id": None, "attempt_id": None},
            kind,
            relative_path,
            data,
            False,
        )

    async def artifact_failed(self, trial_id, attempt_index, kind, relative_path, reason):
        attempt_index = self._index(trial_id, attempt_index)
        with self.db.read() as conn:
            self._check(conn, trial_id)
            attempt_id = self._attempt(conn, trial_id, attempt_index)
        await self._store(
            storage_key(self.run_id, attempt_id, relative_path),
            {"trial_id": trial_id, "attempt_id": attempt_id},
            kind,
            relative_path,
            b"",
            False,
            failure_reason=reason,
        )

    async def _store(
        self, key, owner, kind, relative_path, data, append, failure_reason=None
    ) -> None:
        store = self.worker.store
        if append and store.exists(key):
            data = store.get(key) + data
        try:
            if failure_reason:
                raise OSError(failure_reason)
            digest, size = await asyncio.to_thread(store.put, key, data)
            status = "available"
        except OSError:
            digest, size, status = None, None, "failed"
        with self.db.write() as conn:
            existing = conn.execute(
                select(t.artifacts.c.id).where(t.artifacts.c.storage_key == key)
            ).scalar()
            values = {
                "digest": digest,
                "size_bytes": size,
                "status": status,
                "status_reason": None
                if status == "available"
                else (failure_reason or "STORAGE_WRITE_FAILED"),
            }
            if existing:
                conn.execute(
                    update(t.artifacts).where(t.artifacts.c.id == existing).values(**values)
                )
            else:
                now = clock.now()
                conn.execute(
                    insert(t.artifacts).values(
                        id=new_id("art"),
                        run_id=self.run_id,
                        kind=kind,
                        relative_path=relative_path,
                        storage_key=key,
                        created_at=clock.to_text(now),
                        expires_at=expiry_for(conn, self.run_id, owner["trial_id"]),
                        **owner,
                        **values,
                    )
                )
            if status == "failed" and owner["trial_id"]:
                conn.execute(
                    update(t.trials)
                    .where(t.trials.c.id == owner["trial_id"])
                    .values(artifact_state=ArtifactState.FAILED)
                )

    async def attempt_finished(
        self, trial_id: str, attempt_index: int, outcome: AttemptOutcome, final: bool
    ) -> None:
        attempt_index = self._index(trial_id, attempt_index)
        cancelled = outcome.failure_cause == "cancelled"
        state, cause = trial_outcome(outcome)
        error = None
        if state != TrialState.SCORED or outcome.failure_cause:
            error = {
                "stage": "execution",
                "code": (cause or "unclassified").upper(),
                "failure_cause": cause,
                "exception_type": outcome.exception_type,
                "message": redact(outcome.message or ""),
                "reward_valid": outcome.reward_valid,
                "retry_recommended": outcome.retry_recommended,
                "grade_requested": outcome.grade_requested,
                "policy_version": POLICY_VERSION,
            }
            if outcome.detail:
                error["detail"] = outcome.detail
        with self.db.write() as conn:
            trial = self._check(conn, trial_id)
            attempt_id = self._attempt(conn, trial_id, attempt_index)
            at = clock.to_text(clock.now())
            conn.execute(
                update(t.execution_attempts)
                .where(t.execution_attempts.c.id == attempt_id)
                .values(
                    state=AttemptState.CANCELLED
                    if cancelled
                    else AttemptState.COMPLETED
                    if state == TrialState.SCORED
                    else AttemptState.FAILED,
                    phase=None,
                    ended_at=at,
                    error_json=error,
                )
            )
            if cancelled or trial["state"] in TRIAL_TERMINAL:
                # A cancelled attempt is closed by the run's stop; a late result after a
                # terminal state stays as evidence on the attempt only.
                return
            if not final:
                # Retry: the attempt ends, the trial returns to queued.
                self._move(conn, trial, TrialState.QUEUED, phase="retry_wait")
                refresh_operation(conn, self.run_id)
                return
            result = {
                "trial_id": trial_id,
                "attempt_id": attempt_id,
                "raw_score": outcome.raw_score,
                "reward_valid": outcome.reward_valid,
                "failure_cause": cause,
            }
            result_digest = digest_json(result)
            existing = conn.execute(
                select(t.trial_results.c.result_digest).where(
                    t.trial_results.c.trial_id == trial_id
                )
            ).scalar()
            if existing is not None:
                if existing != result_digest:
                    raise MaestroError("RESULT_CONFLICT", "Different result already recorded", 409)
                return
            conn.execute(
                insert(t.trial_results).values(
                    trial_id=trial_id,
                    attempt_id=attempt_id,
                    result_digest=result_digest,
                    raw_score=outcome.raw_score,
                    metrics_json=outcome.metrics,
                    verifier_status=outcome.verifier_status,
                    failure_cause=cause,
                    exception_type=outcome.exception_type,
                    reward_valid=outcome.reward_valid,
                    grade_requested=outcome.grade_requested,
                    retry_recommended=outcome.retry_recommended,
                    actual_model_json=outcome.actual_model,
                    usage_json=outcome.usage,
                    policy_version=POLICY_VERSION,
                    recorded_at=at,
                )
            )
            artifact = (
                ArtifactState.FAILED
                if trial["artifact_state"] == ArtifactState.FAILED
                else ArtifactState.COMPLETE
            )
            self._move(
                conn,
                trial,
                state,
                phase=None,
                queue_reason=None,
                reason_code=None
                if state == TrialState.SCORED
                else (cause or "unclassified").upper(),
                execution_terminal_at=at,
                result_revision=trial["result_revision"] + 1,
                artifact_state=artifact,
            )
            stamp_expiry(conn, self.run_id, trial_id)
            refresh_operation(conn, self.run_id)

    def mark_waiting(self, trial_ids: list[str], phase: str | None) -> None:
        """Queued trials of a task whose cache is being restored show it."""
        with self.db.write() as conn:
            self._check_run(conn)
            conn.execute(
                update(t.trials)
                .where(t.trials.c.id.in_(trial_ids), t.trials.c.state == TrialState.QUEUED)
                .values(phase=phase, queue_reason="PREPARING_CACHE" if phase else None)
            )
            refresh_operation(conn, self.run_id)

    async def preparation_failed(self, trial_id: str, error: dict, final: bool) -> None:
        """A failed cache restore is an attempt of each waiting trial."""
        await self.attempt_started(trial_id, 1, {"runner_id": "platform-preparation"})
        await self.phase_changed(trial_id, 1, "cache")
        await self.attempt_finished(
            trial_id,
            1,
            AttemptOutcome(
                raw_score=None,
                reward_valid=False,
                failure_cause=error["code"].lower(),
                exception_type=error["code"],
                message=error.get("message"),
                retry_recommended=not final,
                grade_requested=False,
                verifier_status="not_run",
                detail={
                    k: error[k]
                    for k in (
                        "kind",
                        "resource_kind",
                        "expected_digest",
                        "operation_id",
                        "evidence",
                        "suggested_actions",
                    )
                    if error.get(k)
                },
            ),
            final=final,
        )
        self.platform_attempts[trial_id] = self.platform_attempts.get(trial_id, 0) + 1


class AttemptSink(RunSink):
    """Fence one dispatched attempt, including delayed resource messages."""

    def __init__(self, worker, spec, lease, attempt_index, attempt_id):
        super().__init__(worker, spec.run_id, lease)
        self.spec = spec
        self.trial_id = spec.trials[0].trial_id
        self.index = attempt_index
        self.attempt_id = attempt_id
        self.attempt_ids[(self.trial_id, self.index)] = attempt_id
        self.outcome = None
        self.retry = False

    def _index(self, trial_id, attempt_index):
        if trial_id != self.trial_id or attempt_index != 1:
            raise StaleLease()
        return self.index

    def _check_run(self, conn):
        super()._check_run(conn)
        attempt = (
            conn.execute(
                select(t.execution_attempts).where(t.execution_attempts.c.id == self.attempt_id)
            )
            .mappings()
            .one()
        )
        if (
            attempt["lease_token"] != self.lease
            or attempt["service_epoch"] != self.worker.epoch
            or not attempt["slot_held"]
        ):
            raise StaleLease()

    async def runner_started(self, identity):
        with self.db.write() as conn:
            self._check_run(conn)
            conn.execute(
                update(t.execution_attempts)
                .where(t.execution_attempts.c.id == self.attempt_id)
                .values(
                    process_identity_json=identity,
                    protocol_version=identity.get("protocol_version"),
                )
            )
            conn.execute(
                update(t.runs)
                .where(t.runs.c.id == self.run_id)
                .values(heartbeat_at=clock.to_text(clock.now()))
            )

    async def attempt_started(self, trial_id, attempt_index, detail=None):
        self._index(trial_id, attempt_index)
        with self.db.write() as conn:
            self._check_run(conn)
            conn.execute(
                update(t.execution_attempts)
                .where(t.execution_attempts.c.id == self.attempt_id)
                .values(executor_ref_json=detail, runner_id=(detail or {}).get("runner_id"))
            )

    async def run_artifact(self, kind, relative_path, data):
        await self.artifact(self.trial_id, 1, kind, relative_path, data)

    def _check_resources(self, conn):
        self._check_run(conn)
        attempt = conn.execute(
            select(t.execution_attempts.c.ended_at, t.execution_attempts.c.cleanup_state).where(
                t.execution_attempts.c.id == self.attempt_id
            )
        ).one()
        if attempt.ended_at is not None or attempt.cleanup_state != CleanupState.PENDING:
            raise StaleLease()

    async def resource_requested(self, phase):
        if phase not in self.spec.stage_resources:
            raise StaleLease()
        with self.db.write() as conn:
            self._check_resources(conn)
            if conn.execute(
                select(t.runs.c.stop_requested).where(t.runs.c.id == self.run_id)
            ).scalar():
                raise StaleLease()
            row = (
                conn.execute(
                    select(t.stage_permits).where(
                        t.stage_permits.c.attempt_id == self.attempt_id,
                        t.stage_permits.c.phase == phase,
                    )
                )
                .mappings()
                .first()
            )
            if row and row["state"] == "granted":
                return row["id"]
            if row and row["state"] == "released":
                raise StaleLease()
            if row is None:
                # The old environment must be confirmed gone before waiting for a
                # larger verifier. Never hold one environment while asking for another.
                held = conn.execute(
                    select(t.stage_permits.c.id).where(
                        t.stage_permits.c.attempt_id == self.attempt_id,
                        t.stage_permits.c.state == "granted",
                    )
                ).first()
                if held or phase != "verification":
                    raise StaleLease()
                permit_id = new_id("prm")
                conn.execute(
                    t.stage_permits.insert().values(
                        id=permit_id,
                        attempt_id=self.attempt_id,
                        phase=phase,
                        state="waiting",
                        **self.spec.stage_resources[phase],
                        requested_at=clock.to_text(clock.now()),
                    )
                )
            else:
                permit_id = row["id"]
        future = self.worker.permit_waiters.get(permit_id)
        if future is None or future.cancelled():
            future = asyncio.get_running_loop().create_future()
            self.worker.permit_waiters[permit_id] = future
        self.worker.wake()
        return await asyncio.shield(future)

    async def resource_released(self, phase, permit_id):
        with self.db.write() as conn:
            self._check_resources(conn)
            row = (
                conn.execute(
                    select(t.stage_permits).where(
                        t.stage_permits.c.id == permit_id,
                        t.stage_permits.c.attempt_id == self.attempt_id,
                        t.stage_permits.c.phase == phase,
                    )
                )
                .mappings()
                .first()
            )
            if row is None or row["state"] == "waiting":
                raise StaleLease()
            if row["state"] == "granted":
                conn.execute(
                    update(t.stage_permits)
                    .where(t.stage_permits.c.id == permit_id)
                    .values(state="released", released_at=clock.to_text(clock.now()), provider=None)
                )
                continuing = conn.execute(
                    select(t.execution_attempts.c.ended_at, t.runs.c.stop_requested)
                    .join(t.trials, t.trials.c.id == t.execution_attempts.c.trial_id)
                    .join(t.runs, t.runs.c.id == t.trials.c.run_id)
                    .where(t.execution_attempts.c.id == self.attempt_id)
                ).one()
                if (
                    phase == "execution"
                    and "verification" in self.spec.stage_resources
                    and continuing.ended_at is None
                    and not continuing.stop_requested
                ):
                    # Reserve a fitting verifier in the same transaction that returns
                    # execution capacity. A larger verifier waits holding no old budget.
                    need = self.spec.stage_resources["verification"]
                    at = clock.to_text(clock.now())
                    granted = (
                        waiting_reason(
                            conn, self.worker.capacity, {"slots": 0, "provider": None, **need}
                        )
                        is None
                    )
                    conn.execute(
                        t.stage_permits.insert().values(
                            id=new_id("prm"),
                            attempt_id=self.attempt_id,
                            phase="verification",
                            state="granted" if granted else "waiting",
                            **need,
                            requested_at=at,
                            granted_at=at if granted else None,
                        )
                    )
        self.worker.wake()

    async def model_finished(self):
        with self.db.write() as conn:
            self._check_resources(conn)
            conn.execute(
                update(t.stage_permits)
                .where(t.stage_permits.c.attempt_id == self.attempt_id)
                .values(provider=None)
            )
        self.worker.wake()

    async def attempt_finished(self, trial_id, attempt_index, outcome, final):
        self._index(trial_id, attempt_index)
        if self.outcome is not None:
            return
        self.outcome = outcome
        self.retry = (
            outcome.retry_recommended
            and outcome.exception_type not in self.spec.retry_policy.get("exclude_exceptions", [])
            and not (outcome.reward_valid and legal_score(outcome.raw_score))
            and self.index <= self.spec.retry_policy.get("max_retries", MAX_RETRIES)
        )
        await super().attempt_finished(trial_id, attempt_index, outcome, final=not self.retry)


class Worker:
    def __init__(
        self,
        db: Database,
        executor: Executor,
        store: ArtifactStore,
        capacity: HostCapacity,
        *,
        preparer: Preparer | None = None,
        credentials=None,
        poll_seconds: float = 0.5,
        cleanup_retry_seconds: float = CLEANUP_RETRY_SECONDS,
        disk=None,
    ):
        self.disk = disk
        self.db = db
        self.executor = executor
        self.store = store
        self.capacity = capacity
        self.preparer = preparer
        self.credentials = credentials
        if preparer is not None:
            preparer.wake = self.wake
        # Back-off between failed cache restores of one task.
        self.retry_waits = (RETRY_MIN_WAIT_SEC, RETRY_WAIT_MULTIPLIER, RETRY_MAX_WAIT_SEC)
        self.poll_seconds = poll_seconds
        self.cleanup_retry_seconds = cleanup_retry_seconds
        self.worker_id = f"local-{os.getpid()}"
        self.epoch: str | None = None
        self.instance_id: str | None = None
        self.leftovers: list[Leftover] = []
        self.active: dict[str, tuple[asyncio.Task, _Cancel]] = {}
        self.ready: dict[str, tuple[RunSpec, str]] = {}
        self.attempt_tasks: dict[str, tuple[asyncio.Task, str]] = {}
        self.permit_waiters: dict[str, asyncio.Future] = {}
        self._wake = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False
        self._last_cleanup_retry = 0.0
        self._recovery_blocked = False

    def labels(self, run_id: str | None = None) -> dict[str, str]:
        labels = {LABEL_INSTANCE: self.instance_id}
        if run_id:
            labels[LABEL_RUN] = run_id.lower()
        return labels

    # --- lifecycle -------------------------------------------------------------

    def start_epoch(self) -> dict:
        """Close out runs an earlier process left claimed before any claim.

        Database part only; ``recover`` then stops leftover Runners and cleans by label.
        """
        self.instance_id = instance_id(self.db)
        self.epoch = new_id("ep")
        report = {"interrupted_runs": [], "requeued_runs": [], "completed_runs": []}
        leftovers = []
        with self.db.write() as conn:
            conn.execute(
                insert(t.service_epochs).values(
                    id=self.epoch,
                    started_at=clock.to_text(clock.now()),
                    process_identity_json={"pid": os.getpid(), "instance_id": self.instance_id},
                )
            )
            stale = (
                conn.execute(
                    select(t.runs.c.id, t.runs.c.runner_json, t.runs.c.stop_reason).where(
                        t.runs.c.lease_token.is_not(None),
                        t.runs.c.state.not_in([s.value for s in RUN_TERMINAL]),
                    )
                )
                .mappings()
                .all()
            )
            for run in stale:
                run_id = run["id"]
                attempts = (
                    conn.execute(
                        select(t.execution_attempts)
                        .join(t.trials, t.trials.c.id == t.execution_attempts.c.trial_id)
                        .where(
                            t.trials.c.run_id == run_id, t.execution_attempts.c.slot_held.is_(True)
                        )
                    )
                    .mappings()
                    .all()
                )
                for attempt in attempts:
                    leftovers.append(
                        Leftover(
                            run_id,
                            {
                                **self.labels(run_id),
                                LABEL_TRIAL: attempt["trial_id"].lower(),
                                LABEL_ATTEMPT: attempt["id"].lower(),
                            },
                            attempt["process_identity_json"],
                            attempt["id"],
                        )
                    )
                if not attempts:
                    leftovers.append(Leftover(run_id, self.labels(run_id), run["runner_json"]))
                trial_states = set(
                    conn.execute(
                        select(t.trials.c.state).where(t.trials.c.run_id == run_id)
                    ).scalars()
                )
                started = conn.execute(
                    select(t.execution_attempts.c.id)
                    .join(t.trials, t.trials.c.id == t.execution_attempts.c.trial_id)
                    .where(t.trials.c.run_id == run_id)
                    .limit(1)
                ).first()
                if started is None and not run["stop_reason"]:
                    # Nothing ever ran: the run keeps its place in the queue.
                    conn.execute(
                        update(t.runs)
                        .where(t.runs.c.id == run_id)
                        .values(
                            state=RunState.QUEUED,
                            lease_token=None,
                            service_epoch=None,
                            started_at=None,
                            runner_json=null(),  # SQL NULL, not a JSON null
                            heartbeat_at=None,
                            reservation_json=null(),
                            cleanup_state=CleanupState.PENDING,
                        )
                    )
                    conn.execute(
                        update(t.work_items)
                        .where(
                            t.work_items.c.trial_id.in_(
                                select(t.trials.c.id).where(t.trials.c.run_id == run_id)
                            )
                        )
                        .values(state="queued", lease_token=None)
                    )
                    refresh_operation(conn, run_id)
                    report["requeued_runs"].append(run_id)
                    continue
                if trial_states <= {s.value for s in TRIAL_TERMINAL}:
                    # Every result was already confirmed; only cleanup remained.
                    finalize(conn, run_id, run["stop_reason"], cleanup=CleanupState.PENDING)
                    report["completed_runs"].append(run_id)
                    continue
                at = clock.to_text(clock.now())
                conn.execute(
                    update(t.execution_attempts)
                    .where(
                        t.execution_attempts.c.trial_id.in_(
                            select(t.trials.c.id).where(t.trials.c.run_id == run_id)
                        ),
                        t.execution_attempts.c.ended_at.is_(None),
                    )
                    .values(state=AttemptState.INTERRUPTED, phase=None, ended_at=at)
                )
                interrupted = list(TRIAL_ACTIVE) + [TrialState.QUEUED]
                conn.execute(
                    update(t.trials)
                    .where(
                        t.trials.c.run_id == run_id,
                        t.trials.c.state.in_(interrupted),
                        t.trials.c.id.in_(select(t.execution_attempts.c.trial_id)),
                    )
                    .values(
                        state=TrialState.INTERRUPTED,
                        phase=None,
                        reason_code="SERVICE_RESTARTED",
                        execution_terminal_at=at,
                    )
                )
                finish_unstarted(conn, run_id, TrialState.NOT_STARTED, "RUN_INTERRUPTED")
                conn.execute(
                    update(t.trials)
                    .where(
                        t.trials.c.run_id == run_id,
                        t.trials.c.cleanup_state != CleanupState.NOT_REQUIRED,
                    )
                    .values(cleanup_state=CleanupState.PENDING)
                )
                finalize(conn, run_id, "SERVICE_RESTARTED", cleanup=CleanupState.PENDING)
                report["interrupted_runs"].append(run_id)
            seen_attempts = {item.attempt_id for item in leftovers if item.attempt_id}
            # A run can already be terminal while a failed cleanup still holds its
            # attempts. Reconcile those process identities before the instance sweep.
            held = (
                conn.execute(
                    select(t.execution_attempts, t.trials.c.run_id)
                    .join(t.trials, t.trials.c.id == t.execution_attempts.c.trial_id)
                    .where(t.execution_attempts.c.slot_held.is_(True))
                )
                .mappings()
                .all()
            )
            for attempt in held:
                if attempt["id"] not in seen_attempts:
                    leftovers.append(
                        Leftover(
                            attempt["run_id"],
                            {
                                **self.labels(attempt["run_id"]),
                                LABEL_TRIAL: attempt["trial_id"].lower(),
                                LABEL_ATTEMPT: attempt["id"].lower(),
                            },
                            attempt["process_identity_json"],
                            attempt["id"],
                        )
                    )
            seen_runs = {item.run_id for item in leftovers}
            legacy = (
                conn.execute(
                    select(t.runs.c.id, t.runs.c.runner_json).where(
                        t.runs.c.reservation_json.is_not(None),
                        t.runs.c.cleanup_state.in_([CleanupState.PENDING, CleanupState.FAILED]),
                    )
                )
                .mappings()
                .all()
            )
            for run in legacy:
                if run["id"] not in seen_runs:
                    leftovers.append(
                        Leftover(run["id"], self.labels(run["id"]), run["runner_json"])
                    )
        self.leftovers = leftovers
        if self.preparer is not None:
            report["preparation"] = self.preparer.start_epoch(self.epoch, self.instance_id)
        return report

    async def recover(self) -> dict:
        """Stop leftover Runners that are provably ours and clean their resources by label;
        then sweep anything else this instance labelled. Nothing is active yet."""
        released = {}
        for leftover in self.leftovers:
            report = await self.executor.reconcile(leftover)
            released[leftover.run_id] = released.get(leftover.run_id, True) and report.released
            if leftover.attempt_id:
                self._release_attempt(leftover.attempt_id, report.released)
            else:
                self._set_cleanup(leftover.run_id, report.released)
        sweep = await self.executor.cleanup(self.labels())
        for run_id, was_released in released.items():
            with self.db.read() as conn:
                held = conn.execute(
                    select(t.execution_attempts.c.id)
                    .join(t.trials, t.trials.c.id == t.execution_attempts.c.trial_id)
                    .where(t.trials.c.run_id == run_id, t.execution_attempts.c.slot_held.is_(True))
                ).first()
            self._set_cleanup(run_id, not held and was_released)
        self._recovery_blocked = not sweep.released
        self.leftovers = []
        result = {"runs": released, "sweep_released": sweep.released}
        if self.preparer is not None:
            result["preparation"] = await self.preparer.recover()
        if self.epoch:
            with self.db.write() as conn:
                conn.execute(
                    update(t.service_epochs)
                    .where(t.service_epochs.c.id == self.epoch)
                    .values(
                        process_identity_json={
                            "pid": os.getpid(),
                            "instance_id": self.instance_id,
                            "recovery": result,
                        }
                    )
                )
        if released or not sweep.released:
            log.warning("restart reconciliation: %s", result)
        return result

    def _set_cleanup(self, run_id: str, released: bool) -> None:
        state = CleanupState.COMPLETE if released else CleanupState.FAILED
        with self.db.write() as conn:
            run = conn.execute(
                select(t.runs.c.lease_token, t.runs.c.state).where(t.runs.c.id == run_id)
            ).one()
            if run.lease_token is not None or run.state not in RUN_TERMINAL:
                return  # requeued or reclaimed; its next execution owns cleanup
            conn.execute(update(t.runs).where(t.runs.c.id == run_id).values(cleanup_state=state))
            conn.execute(
                update(t.trials)
                .where(
                    t.trials.c.run_id == run_id,
                    t.trials.c.cleanup_state != CleanupState.NOT_REQUIRED,
                )
                .values(cleanup_state=state)
            )

    def wake(self) -> None:
        """Safe from request threads: the event belongs to the worker's loop."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is loop:
            self._wake.set()
        else:
            loop.call_soon_threadsafe(self._wake.set)

    async def run_forever(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self.epoch is None:
            self.start_epoch()
        try:
            await self.recover()
        except Exception:
            self._recovery_blocked = True
            log.exception("restart reconciliation failed")
        self._last_cleanup_retry = self._loop.time()
        while not self._stopping:
            try:
                await self.tick()
            except Exception:  # keep the loop alive; failures are visible in logs
                log.exception("worker tick failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), self.poll_seconds)
            except TimeoutError:
                pass

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        for task, cancel in list(self.active.values()):
            cancel.event.set()
            task.cancel()
        for task, _ in list(self.active.values()):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self.preparer is not None:
            await self.preparer.stop()

    async def tick(self) -> None:
        self._enforce_stops()
        await self._retry_cleanups()
        if self.preparer is not None:
            await self.preparer.tick()
        while True:
            claimed = self._claim()
            if claimed is None:
                break
            run_id, lease, spec = claimed
            cancel = _Cancel()
            task = asyncio.create_task(self._execute(run_id, lease, spec, cancel))
            self.active[run_id] = (task, cancel)
        self._dispatch()

    async def drain(self, timeout: float = 10) -> None:
        """Test helper: tick until nothing is queued or active."""
        self._loop = asyncio.get_running_loop()
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            await self.tick()
            with self.db.read() as conn:
                queued = conn.execute(
                    select(t.runs.c.id).where(t.runs.c.state.not_in(RUN_TERMINAL)).limit(1)
                ).first()
            busy = self.preparer is not None and (self.preparer.jobs or self.preparer.active)
            if not self.active and not queued and not busy:
                return
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("worker did not drain")
            await asyncio.sleep(0.01)

    # --- claim and execution ----------------------------------------------------

    def _enforce_stops(self) -> None:
        now = clock.to_text(clock.now())
        with self.db.write() as conn:
            expired = conn.execute(
                select(t.runs.c.id, t.runs.c.lease_token).where(
                    t.runs.c.state.not_in([s.value for s in RUN_TERMINAL]),
                    t.runs.c.stop_requested.is_(False),
                    t.runs.c.deadline_at <= now,
                )
            ).all()
            for run_id, lease in expired:
                conn.execute(
                    update(t.runs)
                    .where(t.runs.c.id == run_id)
                    .values(stop_requested=True, stop_reason="RUN_DEADLINE_EXCEEDED")
                )
                if lease is None:
                    finish_unstarted(conn, run_id, TrialState.NOT_STARTED, "RUN_DEADLINE_EXCEEDED")
                    finalize(
                        conn, run_id, "RUN_DEADLINE_EXCEEDED", cleanup=CleanupState.NOT_REQUIRED
                    )
            stopping = set(
                conn.execute(
                    select(t.runs.c.id).where(
                        t.runs.c.id.in_(list(self.active)), t.runs.c.stop_requested.is_(True)
                    )
                ).scalars()
            )
        for run_id in stopping:
            self.active[run_id][1].event.set()

    async def _retry_cleanups(self) -> None:
        """Resources not confirmed released keep their capacity; retry them periodically."""
        loop = asyncio.get_running_loop()
        if loop.time() - self._last_cleanup_retry < self.cleanup_retry_seconds:
            return
        self._last_cleanup_retry = loop.time()
        if self.leftovers:
            await self.recover()
        with self.db.read() as conn:
            pending = (
                conn.execute(
                    select(t.runs.c.id, t.runs.c.runner_json).where(
                        t.runs.c.lease_token.is_(None),
                        t.runs.c.state.in_([s.value for s in RUN_TERMINAL]),
                        t.runs.c.cleanup_state.in_([CleanupState.PENDING, CleanupState.FAILED]),
                        t.runs.c.reservation_json.is_not(None),
                    )
                )
                .mappings()
                .all()
            )
        with self.db.read() as conn:
            attempts = (
                conn.execute(
                    select(
                        t.execution_attempts.c.id,
                        t.trials.c.run_id,
                        t.trials.c.id.label("trial_id"),
                        t.execution_attempts.c.service_epoch,
                        t.execution_attempts.c.process_identity_json,
                    )
                    .join(t.trials, t.trials.c.id == t.execution_attempts.c.trial_id)
                    .where(
                        t.execution_attempts.c.slot_held.is_(True),
                        t.execution_attempts.c.cleanup_state.in_(
                            [CleanupState.PENDING, CleanupState.FAILED]
                        ),
                    )
                )
                .mappings()
                .all()
            )
        for attempt in attempts:
            if attempt["id"] in self.attempt_tasks:
                continue
            labels = {
                **self.labels(attempt["run_id"]),
                LABEL_TRIAL: attempt["trial_id"].lower(),
                LABEL_ATTEMPT: attempt["id"].lower(),
            }
            if attempt["service_epoch"] != self.epoch:
                report = await self.executor.reconcile(
                    Leftover(
                        attempt["run_id"], labels, attempt["process_identity_json"], attempt["id"]
                    )
                )
            else:
                report = await self.executor.cleanup(labels)
            self._release_attempt(attempt["id"], report.released)
            if report.released:
                with self.db.read() as conn:
                    held = conn.execute(
                        select(t.execution_attempts.c.id)
                        .join(t.trials, t.trials.c.id == t.execution_attempts.c.trial_id)
                        .where(
                            t.trials.c.run_id == attempt["run_id"],
                            t.execution_attempts.c.slot_held.is_(True),
                        )
                    ).first()
                if not held:
                    self._set_cleanup(attempt["run_id"], True)
        if self._recovery_blocked:
            self._recovery_blocked = not (await self.executor.cleanup(self.labels())).released
        for run in pending:
            report = await self.executor.reconcile(
                Leftover(run["id"], self.labels(run["id"]), run["runner_json"])
            )
            self._set_cleanup(run["id"], report.released)

    def _claim(self):
        """Lease a run for preparation without reserving execution capacity."""
        lease = secrets.token_urlsafe(24)
        paused = "STORAGE_LOW" if self.disk and not self.disk.dispatching() else None
        with self.db.write() as conn:
            if paused is None and updates.dispatch_paused(conn):
                paused = "UPDATE_PENDING"
            if paused is not None:
                conn.execute(
                    update(t.runs)
                    .where(
                        t.runs.c.state == RunState.QUEUED,
                        t.runs.c.lease_token.is_(None),
                        t.runs.c.queue_reason.is_distinct_from(paused),
                    )
                    .values(queue_reason=paused)
                )
                return None
            queued = (
                conn.execute(
                    select(t.runs.c.id, t.runs.c.concurrency, t.runs.c.queue_reason)
                    .where(
                        t.runs.c.state == RunState.QUEUED,
                        t.runs.c.stop_requested.is_(False),
                        t.runs.c.lease_token.is_(None),
                    )
                    .order_by(
                        t.runs.c.accepted_at,
                        select(func.min(t.work_items.c.enqueue_seq))
                        .join(t.trials, t.trials.c.id == t.work_items.c.trial_id)
                        .where(t.trials.c.run_id == t.runs.c.id)
                        .scalar_subquery(),
                        t.runs.c.id,
                    )
                    .limit(CLAIM_SCAN)
                )
                .mappings()
                .all()
            )
            if not queued:
                return None
            run = queued[0]
            conn.execute(
                update(t.runs)
                .where(t.runs.c.id == run["id"])
                .values(
                    lease_token=lease,
                    service_epoch=self.epoch,
                    state=RunState.PREPARING,
                    started_at=clock.to_text(clock.now()),
                    queue_reason=None,
                    reservation_json=null(),
                    cleanup_state=CleanupState.PENDING,
                )
            )
            conn.execute(
                update(t.work_items)
                .where(
                    t.work_items.c.trial_id.in_(
                        select(t.trials.c.id).where(t.trials.c.run_id == run["id"])
                    )
                )
                .values(state="claimed", lease_token=lease)
            )
            row = conn.execute(select(t.runs).where(t.runs.c.id == run["id"])).mappings().one()
            snapshot, policy = conn.execute(
                select(t.run_snapshots.c.effective_json, t.run_snapshots.c.policy_json).where(
                    t.run_snapshots.c.run_id == run["id"]
                )
            ).one()
            trial_rows = conn.execute(
                select(
                    t.trials.c.id,
                    t.trials.c.repetition_index,
                    t.tasks.c.id.label("task_id"),
                    t.tasks.c.task_key,
                    t.tasks.c.definition_json,
                    t.tasks.c.content_digest,
                    t.tasks.c.material_digest,
                )
                .join(t.tasks, t.tasks.c.id == t.trials.c.task_id)
                .where(t.trials.c.run_id == run["id"])
                .order_by(t.tasks.c.task_key, t.trials.c.repetition_index)
            ).all()
            refresh_operation(conn, run["id"])
        spec = RunSpec(
            run_id=row["id"],
            trials=tuple(
                TrialSpec(
                    r.id,
                    r.task_key,
                    r.definition_json,
                    r.repetition_index,
                    r.content_digest,
                    r.material_digest,
                    r.task_id,
                )
                for r in trial_rows
            ),
            concurrency=row["concurrency"],
            effective_config=snapshot,
            deadline_at=row["deadline_at"],
            labels=self.labels(row["id"]),
            metric_id=snapshot_contract(policy)["metric_id"],
            retry_policy=policy.get("retry") or harbor_retry_config(),
        )
        return row["id"], lease, spec

    def _dispatch(self):
        """Grant continuation stages first, then pack trials in run acceptance order."""
        launches = []
        if self._recovery_blocked or self._stopping:
            return
        with self.db.write() as conn:
            pending = (
                conn.execute(
                    select(t.stage_permits, t.trials.c.id.label("trial_id"), t.trials.c.run_id)
                    .join(
                        t.execution_attempts,
                        t.execution_attempts.c.id == t.stage_permits.c.attempt_id,
                    )
                    .join(t.trials, t.trials.c.id == t.execution_attempts.c.trial_id)
                    .join(t.runs, t.runs.c.id == t.trials.c.run_id)
                    .where(
                        t.stage_permits.c.state == "waiting",
                        t.runs.c.stop_requested.is_(False),
                        t.execution_attempts.c.service_epoch == self.epoch,
                        t.execution_attempts.c.slot_held.is_(True),
                        t.execution_attempts.c.cleanup_state == CleanupState.PENDING,
                        t.execution_attempts.c.ended_at.is_(None),
                    )
                    .order_by(t.stage_permits.c.requested_at, t.stage_permits.c.id)
                )
                .mappings()
                .all()
            )
            protected_stage = False
            for permit in pending:
                need = {
                    "slots": 0,
                    "cpus": permit["cpus"],
                    "memory_mb": permit["memory_mb"],
                    "provider": None,
                }
                reason = waiting_reason(conn, self.capacity, need)
                self._waiting(conn, permit, reason)
                if (
                    reason
                    and (clock.now() - clock.from_text(permit["requested_at"])).total_seconds()
                    >= scheduling.PROTECT_AFTER_SECONDS
                ):
                    # New attempts must eventually drain for a larger verifier;
                    # continuation priority alone cannot prevent endless backfill.
                    protected_stage = True
                if reason is None:
                    conn.execute(
                        update(t.stage_permits)
                        .where(t.stage_permits.c.id == permit["id"])
                        .values(state="granted", granted_at=clock.to_text(clock.now()))
                    )
                    future = self.permit_waiters.pop(permit["id"], None)
                    if future is not None and not future.done():
                        future.set_result(permit["id"])
            paused = "STORAGE_LOW" if self.disk and not self.disk.dispatching() else None
            if updates.dispatch_paused(conn):
                paused = "UPDATE_PENDING"
            rows = (
                conn.execute(
                    select(t.work_items, t.trials.c.run_id, t.runs.c.concurrency)
                    .join(t.trials, t.trials.c.id == t.work_items.c.trial_id)
                    .join(t.runs, t.runs.c.id == t.trials.c.run_id)
                    .where(
                        t.trials.c.id.in_(list(self.ready)),
                        t.trials.c.state == TrialState.QUEUED,
                        t.runs.c.stop_requested.is_(False),
                        t.runs.c.service_epoch == self.epoch,
                        t.work_items.c.eligible_at <= clock.to_text(clock.now()),
                    )
                    .order_by(t.runs.c.accepted_at, t.work_items.c.enqueue_seq)
                )
                .mappings()
                .all()
            )
            running = dict(
                conn.execute(
                    select(t.trials.c.run_id, func.count())
                    .join(t.execution_attempts, t.execution_attempts.c.trial_id == t.trials.c.id)
                    .where(t.execution_attempts.c.slot_held.is_(True))
                    .group_by(t.trials.c.run_id)
                ).all()
            )
            active_trials = set(
                conn.execute(
                    select(t.execution_attempts.c.trial_id).where(
                        t.execution_attempts.c.slot_held.is_(True)
                    )
                ).scalars()
            )
            candidates = []
            for row in rows:
                if row["trial_id"] in active_trials:
                    self._waiting(conn, row, "CLEANUP_PENDING")
                    continue
                spec, lease = self.ready[row["trial_id"]]
                settings = next(
                    (
                        x
                        for x in spec.effective_config.get("tasks") or []
                        if x["task_key"] == spec.trials[0].task_key
                    ),
                    {},
                )
                stages = stage_resources(settings, spec.trials[0].task_definition)
                provider = (
                    provider_of(spec.effective_config)
                    if spec.effective_config.get("execution_mode") == "agent"
                    else None
                )
                need = {"slots": 1, **stages["execution"], "provider": provider}
                reason = (
                    paused
                    or ("WAITING_PRIORITY" if protected_stage else None)
                    or (
                        "RUN_CONCURRENCY"
                        if running.get(row["run_id"], 0) >= row["concurrency"]
                        else None
                    )
                )
                if reason:
                    self._waiting(conn, row, reason)
                    continue
                candidates.append(
                    {**row, "spec": spec, "lease": lease, "stages": stages, "need": need}
                )
            bypassed = {}
            while candidates:
                run_id = candidates[0]["run_id"]
                group = [x for x in candidates if x["run_id"] == run_id]
                protected = next((x for x in group if scheduling.protected(x, clock.now())), None)
                used = in_use(conn)
                limits = provider_limits(conn)
                slots = min(
                    self.capacity.slots - used["slots"],
                    group[0]["concurrency"] - running.get(run_id, 0),
                )
                provider = group[0]["need"]["provider"]
                if provider in limits:
                    slots = min(slots, limits[provider] - used["providers"].get(provider, 0))
                # Drain capacity for an aged request. Already-started continuations
                # above are exempt so every occupied Runner can still finish.
                chosen = []
                if protected is not None:
                    reason = waiting_reason(conn, self.capacity, protected["need"])
                    if reason and slots > 0:
                        for item in candidates:
                            self._waiting(
                                conn, item, reason if item is protected else "WAITING_PRIORITY"
                            )
                        break
                    if slots > 0:
                        chosen = [protected]
                    protected = None
                elif slots > 0:
                    # Bound each packing search, but inspect every window before
                    # letting a later run consume capacity this run could use.
                    for start in range(0, len(group), scheduling.CANDIDATES):
                        chosen = scheduling.pack(
                            group[start : start + scheduling.CANDIDATES],
                            slots,
                            self.capacity.cpus - used["cpus"],
                            self.capacity.memory_mb - used["memory_mb"],
                            self.capacity.cpus,
                            self.capacity.memory_mb,
                        )
                        if chosen:
                            break
                for item in chosen:
                    launches.append(
                        self._grant_attempt(
                            conn,
                            item,
                            backfilled=any(
                                older["run_id"] != run_id
                                and older["enqueue_seq"] < item["enqueue_seq"]
                                for older in rows
                            ),
                        )
                    )
                    running[run_id] = running.get(run_id, 0) + 1
                skipped = [x for x in group if x not in chosen]
                for item in skipped:
                    reason = waiting_reason(conn, self.capacity, item["need"])
                    if running.get(run_id, 0) >= item["concurrency"]:
                        reason = "RUN_CONCURRENCY"
                    self._waiting(conn, item, reason or "WAITING_PRIORITY")
                # Count actual bypasses, including within this run, rather than
                # polling ticks. Preparation and backoff never accrue bypasses.
                bypassed.update((item["id"], item) for item in skipped)
                for item in chosen:
                    bypassed.pop(item["id"], None)
                if chosen:
                    for item in bypassed.values():
                        item["bypass_count"] += len(chosen)
                        conn.execute(
                            update(t.work_items)
                            .where(t.work_items.c.id == item["id"])
                            .values(bypass_count=t.work_items.c.bypass_count + len(chosen))
                        )
                candidates = [
                    x for x in candidates if x not in chosen and (chosen or x["run_id"] != run_id)
                ]
        for spec, lease, sink in launches:
            cancel = self.active[spec.run_id][1]
            task = asyncio.create_task(self._execute_attempt(spec, lease, sink, cancel))
            self.attempt_tasks[spec.attempt_id] = (task, spec.run_id)

    @staticmethod
    def _waiting(conn, item, reason):
        conn.execute(
            update(t.trials).where(t.trials.c.id == item["trial_id"]).values(queue_reason=reason)
        )
        conn.execute(
            update(t.runs).where(t.runs.c.id == item["run_id"]).values(queue_reason=reason)
        )

    def _grant_attempt(self, conn, item, backfilled):
        spec, lease = item["spec"], item["lease"]
        trial_id = item["trial_id"]
        index = (
            conn.execute(
                select(func.coalesce(func.max(t.execution_attempts.c.attempt_index), 0)).where(
                    t.execution_attempts.c.trial_id == trial_id
                )
            ).scalar()
            + 1
        )
        attempt_id, at = new_id("att"), clock.to_text(clock.now())
        conn.execute(
            t.execution_attempts.insert().values(
                id=attempt_id,
                trial_id=trial_id,
                attempt_index=index,
                worker_id=self.worker_id,
                service_epoch=self.epoch,
                lease_token=lease,
                state=AttemptState.STARTING,
                phase="starting",
                started_at=at,
                steps_json=[{"step": "starting", "at": at}],
                slot_held=True,
                cleanup_state=CleanupState.PENDING,
                backfilled=backfilled,
            )
        )
        conn.execute(
            t.stage_permits.insert().values(
                id=new_id("prm"),
                attempt_id=attempt_id,
                phase="execution",
                state="granted",
                **item["stages"]["execution"],
                provider=item["need"]["provider"],
                requested_at=at,
                granted_at=at,
            )
        )
        conn.execute(
            update(t.trials)
            .where(t.trials.c.id == trial_id)
            .values(
                state=TrialState.PREPARING,
                phase="starting",
                queue_reason=None,
                cleanup_state=CleanupState.PENDING,
            )
        )
        conn.execute(
            update(t.work_items)
            .where(t.work_items.c.trial_id == trial_id)
            .values(state="claimed", bypass_count=0)
        )
        conn.execute(update(t.runs).where(t.runs.c.id == spec.run_id).values(queue_reason=None))
        spec = replace(
            spec,
            attempt_id=attempt_id,
            attempt_index=index,
            stage_resources=item["stages"],
            labels={
                **spec.labels,
                LABEL_TRIAL: trial_id.lower(),
                LABEL_ATTEMPT: attempt_id.lower(),
            },
        )
        refresh_operation(conn, spec.run_id)
        return spec, lease, AttemptSink(self, spec, lease, index, attempt_id)

    async def _execute_attempt(self, spec, lease, sink, cancel):
        released = False
        try:
            with self.db.read() as conn:
                previous_bytes = dict(
                    conn.execute(
                        select(
                            t.artifacts.c.trial_id,
                            func.coalesce(func.sum(t.artifacts.c.size_bytes), 0),
                        )
                        .where(
                            t.artifacts.c.trial_id == spec.trials[0].trial_id,
                            t.artifacts.c.kind == "output",
                        )
                        .group_by(t.artifacts.c.trial_id)
                    ).all()
                )
                spec = replace(
                    spec,
                    setup_network=setup_network.current(conn),
                    deliverable_bytes=previous_bytes,
                )
            if spec.effective_config.get("harness") or installed_packages(spec.effective_config):
                sink.setup_network_revision = spec.setup_network.revision
                with self.db.write() as conn:
                    conn.execute(
                        update(t.execution_attempts)
                        .where(t.execution_attempts.c.id == spec.attempt_id)
                        .values(setup_network_revision=spec.setup_network.revision)
                    )
            report = await self.executor.execute(spec, sink, cancel)
            released = report.cleanup_complete
            if sink.outcome is None and not cancel.cancelled:
                await sink.attempt_finished(
                    spec.trials[0].trial_id,
                    1,
                    AttemptOutcome(
                        raw_score=None,
                        reward_valid=False,
                        failure_cause=(report.failure_code or "EXECUTOR_NO_RESULT").lower(),
                        exception_type=report.failure_code or "ExecutorNoResult",
                        retry_recommended=report.failure_code
                        not in {
                            "NETWORK_POLICY_UNSUPPORTED",
                            "RUNNER_INPUT_INVALID",
                            "ATTACHMENT_CONTENT_MISMATCH",
                            "RUNNER_EXITED",
                            "RUNNER_PROTOCOL_ERROR",
                            "RUNNER_UNRESPONSIVE",
                        },
                    ),
                    True,
                )
        except StaleLease:
            log.warning("attempt %s lost its lease", spec.attempt_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("attempt %s executor failed", spec.attempt_id)
            if not cancel.cancelled:
                with contextlib.suppress(StaleLease):
                    await sink.attempt_finished(
                        spec.trials[0].trial_id,
                        1,
                        AttemptOutcome(
                            raw_score=None,
                            reward_valid=False,
                            failure_cause="sandbox_failed",
                            exception_type="ExecutorFailed",
                            retry_recommended=True,
                        ),
                        True,
                    )
            with contextlib.suppress(Exception):
                released = (await self.executor.cleanup(spec.labels)).released
        finally:
            self.attempt_tasks.pop(spec.attempt_id, None)
            self._release_attempt(spec.attempt_id, released)
            if sink.retry:
                delay = self._retry_delay(spec, sink.index)
                with self.db.write() as conn:
                    conn.execute(
                        update(t.work_items)
                        .where(t.work_items.c.trial_id == spec.trials[0].trial_id)
                        .values(
                            state="queued",
                            eligible_at=clock.to_text(clock.now() + timedelta(seconds=delay)),
                            ready_at=clock.to_text(clock.now()),
                            bypass_count=0,
                        )
                    )
                    conn.execute(
                        update(t.trials)
                        .where(t.trials.c.id == spec.trials[0].trial_id)
                        .values(queue_reason="RETRY_BACKOFF" if released else "CLEANUP_PENDING")
                    )
            self.wake()

    def _retry_delay(self, spec, index):
        low, factor, high = self.retry_waits
        if self.retry_waits == (RETRY_MIN_WAIT_SEC, RETRY_WAIT_MULTIPLIER, RETRY_MAX_WAIT_SEC):
            low, factor, high = (
                spec.retry_policy[k] for k in ("min_wait_sec", "wait_multiplier", "max_wait_sec")
            )
        return min(low * factor ** (index - 1), high)

    def _release_attempt(self, attempt_id, released):
        with self.db.write() as conn:
            conn.execute(
                update(t.execution_attempts)
                .where(t.execution_attempts.c.id == attempt_id)
                .values(
                    slot_held=not released,
                    cleanup_state=CleanupState.COMPLETE if released else CleanupState.FAILED,
                )
            )
            permit_ids = list(
                conn.execute(
                    select(t.stage_permits.c.id).where(t.stage_permits.c.attempt_id == attempt_id)
                ).scalars()
            )
            for permit_id in permit_ids:
                future = self.permit_waiters.pop(permit_id, None)
                if future is not None and not future.done():
                    future.cancel()
            conn.execute(
                update(t.stage_permits)
                .where(
                    t.stage_permits.c.attempt_id == attempt_id,
                    t.stage_permits.c.state != "released"
                    if released
                    else t.stage_permits.c.state == "waiting",
                )
                .values(state="released", provider=None, released_at=clock.to_text(clock.now()))
            )

    def _ready_trials(self, spec: RunSpec, lease: str) -> None:
        with self.db.write() as conn:
            for trial in spec.trials:
                self.ready[trial.trial_id] = (replace(spec, trials=(trial,), concurrency=1), lease)
                conn.execute(
                    update(t.work_items)
                    .where(t.work_items.c.trial_id == trial.trial_id)
                    .values(ready_at=clock.to_text(clock.now()))
                )
        asyncio.get_running_loop().call_soon(self._dispatch)
        self.wake()

    async def _execute(self, run_id: str, lease: str, spec: RunSpec, cancel: _Cancel) -> None:
        self.active.setdefault(run_id, (asyncio.current_task(), cancel))
        sink = RunSink(self, run_id, lease)
        cleanup, failure, close = CleanupState.COMPLETE, None, True
        try:
            spec = replace(spec, secrets=self._secrets(spec))
            if self.preparer is not None:
                await self._prepare(spec, sink, cancel)
            else:
                self._ready_trials(spec, lease)
            while not cancel.cancelled:
                self._dispatch()
                with self.db.read() as conn:
                    remaining = conn.execute(
                        select(t.trials.c.id)
                        .where(t.trials.c.run_id == run_id, t.trials.c.state.not_in(TRIAL_TERMINAL))
                        .limit(1)
                    ).first()
                if remaining is None:
                    break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(cancel.wait(), 0.05)
        except StaleLease:
            log.warning("run %s lost its lease", run_id)
        except CredentialUnavailable as error:
            failure = "CREDENTIAL_UNAVAILABLE"
            log.warning("run %s: credential %s cannot be used", run_id, error.credential_id)
        except asyncio.CancelledError:
            close = False
            cancel.event.set()
            raise
        except Exception:
            failure = "EXECUTOR_FAILED"
            cancel.event.set()
            log.exception("execution coordinator failed for run %s", run_id)
        finally:
            tasks = [task for task, owner in self.attempt_tasks.values() if owner == run_id]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            with self.db.read() as conn:
                held = conn.execute(
                    select(t.execution_attempts.c.id)
                    .join(t.trials, t.trials.c.id == t.execution_attempts.c.trial_id)
                    .where(t.trials.c.run_id == run_id, t.execution_attempts.c.slot_held.is_(True))
                    .limit(1)
                ).first()
            cleanup = CleanupState.FAILED if held else CleanupState.COMPLETE
            self.ready = {
                key: value for key, value in self.ready.items() if value[0].run_id != run_id
            }
            self.active.pop(run_id, None)
            if close and not self._stopping:
                self._close(run_id, lease, cleanup, failure)
                if self.preparer is not None:
                    self.preparer.release("run", run_id)
            self.wake()

    def _secrets(self, spec: RunSpec) -> dict[str, str]:
        """Values of the credentials the snapshot binds, read now: a run uses the current
        value of its credential, never one copied at acceptance."""
        effective = spec.effective_config
        wanted = {}
        if (effective.get("model") or {}).get("credential_id"):
            wanted["model_api_key"] = effective["model"]["credential_id"]
        for name, value in (effective.get("parameters") or {}).items():
            if "credential_id" in value:
                wanted[f"param:{name}"] = value["credential_id"]
        if not wanted:
            return {}
        if self.credentials is None:
            raise CredentialUnavailable(next(iter(wanted.values())))
        secrets = {}
        for key, credential_id in wanted.items():
            try:
                secrets[key], _ = self.credentials.resolve_value(credential_id)
            except Exception:  # noqa: BLE001 - disabled, deleted, or undecryptable
                raise CredentialUnavailable(credential_id) from None
        return secrets

    async def _prepare(self, spec: RunSpec, sink: RunSink, cancel: _Cancel) -> RunSpec:
        """Before the run's executor starts, every task's prepared image is
        pinned, restoring it when the cache is cold. Restores are shared with other
        runs; a failed restore is an attempt of each trial of that task, retried with
        back-off only for transient causes; other tasks of the run go ahead."""
        by_task: dict[str, list[TrialSpec]] = {}
        for trial in spec.trials:
            by_task.setdefault(trial.task_id, []).append(trial)
        with self.db.read() as conn:
            contexts = {task_id: load_context(conn, task_id) for task_id in by_task}

        async def one(task_id: str) -> tuple[str, dict[str, str] | None]:
            trials = by_task[task_id]
            ids = [x.trial_id for x in trials]
            failures = 0
            while True:
                sink.mark_waiting(ids, "restoring_cache")
                try:
                    output = await self.preparer.acquire(
                        contexts[task_id],
                        "run",
                        spec.run_id,
                        cancel=cancel.event,
                        deadline_at=spec.deadline_at,
                    )
                    sink.mark_waiting(ids, None)
                    self._ready_trials(
                        replace(
                            spec,
                            trials=tuple(replace(t, prepared_images=output.refs()) for t in trials),
                        ),
                        sink.lease,
                    )
                    return task_id, output.refs()
                except PreparationFailure as error:
                    if error.kind == "cancelled" or cancel.cancelled:
                        return task_id, None
                    failures += 1
                    final = not error.retryable or failures > spec.retry_policy.get(
                        "max_retries", MAX_RETRIES
                    )
                    for trial_id in ids:
                        await sink.preparation_failed(trial_id, error.error, final)
                    if final:
                        return task_id, None
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(cancel.wait(), self._retry_delay(spec, failures))
                    if cancel.cancelled:
                        return task_id, None

        # An unexpected failure in one task cancels the others (and takes their waiter
        # rows with them) before it propagates: nothing may keep waiting for a run that
        # is about to be closed.
        images = dict(await gather_or_cancel(*(one(task_id) for task_id in by_task)))
        return replace(
            spec,
            trials=tuple(
                replace(trial, prepared_images=images[trial.task_id])
                for trial in spec.trials
                if images.get(trial.task_id)
            ),
        )

    def _close(self, run_id: str, lease: str, cleanup: CleanupState, failure: str | None) -> None:
        with self.db.write() as conn:
            run = conn.execute(select(t.runs).where(t.runs.c.id == run_id)).mappings().one()
            if run["lease_token"] != lease or run["state"] in RUN_TERMINAL:
                return
            conn.execute(
                update(t.runs).where(t.runs.c.id == run_id).values(state=RunState.FINALIZING)
            )
            reason = run["stop_reason"] if run["stop_requested"] else None
            at = clock.to_text(clock.now())
            if reason:
                active_target, active_reason = TrialState.CANCELLED, reason
            else:
                # The executor returned without a result for these trials.
                active_target = TrialState.EXECUTION_ERROR
                active_reason = failure or "EXECUTOR_NO_RESULT"
            # Trials that already ran (e.g. waiting for a retry) end like active ones;
            # only never-started trials become not_started.
            conn.execute(
                update(t.trials)
                .where(
                    t.trials.c.run_id == run_id,
                    t.trials.c.state.in_([*TRIAL_ACTIVE, TrialState.QUEUED]),
                    t.trials.c.id.in_(select(t.execution_attempts.c.trial_id)),
                )
                .values(
                    state=active_target,
                    phase=None,
                    reason_code=active_reason,
                    execution_terminal_at=at,
                )
            )
            if reason:
                finish_unstarted(conn, run_id, TrialState.NOT_STARTED, reason)
            else:
                finish_unstarted(conn, run_id, active_target, active_reason)
            conn.execute(
                update(t.execution_attempts)
                .where(
                    t.execution_attempts.c.lease_token == lease,
                    t.execution_attempts.c.ended_at.is_(None),
                )
                .values(
                    state=AttemptState.CANCELLED if reason else AttemptState.FAILED,
                    phase=None,
                    ended_at=at,
                )
            )
            held_trials = set(
                conn.execute(
                    select(t.execution_attempts.c.trial_id).where(
                        t.execution_attempts.c.slot_held.is_(True)
                    )
                ).scalars()
            )
            conn.execute(
                update(t.trials).where(t.trials.c.run_id == run_id).values(queue_reason=None)
            )
            for trial_id in conn.execute(
                select(t.trials.c.id).where(t.trials.c.run_id == run_id)
            ).scalars():
                conn.execute(
                    update(t.trials)
                    .where(t.trials.c.id == trial_id)
                    .values(
                        cleanup_state=CleanupState.FAILED
                        if trial_id in held_trials
                        else CleanupState.COMPLETE
                    )
                )

            conn.execute(
                update(t.work_items).where(t.work_items.c.lease_token == lease).values(state="done")
            )
            finalize(conn, run_id, reason, cleanup=cleanup)
