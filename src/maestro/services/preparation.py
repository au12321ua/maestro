"""Preparation coordinator (02 sections 3-8): imports, first preparation, shared cache
restore, eviction and restart reconciliation.

All of it runs on the worker's event loop, so join / leave / publish decisions are
serialized; every decision is also a short write transaction that re-checks the
records it depends on (01 section 6.5).

Cache key is (spec_digest, location_id). One restore Operation per key and
generation holds the lease; waiters (runs, or the admin's first-preparation
Operation) join it, leave it independently, and receive a pin when it publishes.
A late or superseded restore cannot publish: publication checks the row's
generation and active Operation. Each restore Operation tries once; retries belong
to the waiting trials' attempts (06 section 4).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import shutil
from dataclasses import dataclass, field

from sqlalchemy import Connection, delete, func, insert, select, update

from maestro.boundaries import dataset_source as src
from maestro.boundaries import environment as env
from maestro.boundaries.dataset_source import DatasetSource, SourceError, SourceTask
from maestro.boundaries.environment import (
    Environment,
    PreparationError,
    PreparationSpec,
    PreparedOutput,
)
from maestro.core import clock
from maestro.core.digests import digest_json
from maestro.core.ids import new_id
from maestro.db import schema as t
from maestro.db.engine import Database
from maestro.domain.states import OPERATION_TERMINAL, OperationStatus
from maestro.execution.materials import (
    MAX_BUILD_TIMEOUT,
    PREPARATION_METHOD_DIGEST,
    TaskMaterials,
    inspect_task,
)
from maestro.security.redaction import redact
from maestro.services import deleted, operations, setup_network

log = logging.getLogger(__name__)

KIND_IMPORT = "dataset_import"
KIND_FIRST = "preparation_first"
KIND_RESTORE = "preparation_restore"
KIND_EVICT = "preparation_eviction"
KINDS = (KIND_IMPORT, KIND_FIRST, KIND_RESTORE, KIND_EVICT)
CONVERTER_VERSION = "maestro-import/1"
PIN_HOURS = 48
RESTORE_SLOTS = 2
EVICTION_POLL = 0.1

# Platform preparation error codes (06 section 3).
CODES = {
    env.TRANSIENT: "PREPARATION_RESTORE_FAILED",
    env.AUTH: "PREPARATION_RESTORE_FAILED",
    env.FAILED: "PREPARATION_RESTORE_FAILED",
    env.UNAVAILABLE: "RESOURCE_CONTENT_UNAVAILABLE",
    env.MISMATCH: "RESOURCE_CONTENT_MISMATCH",
    env.UNSUPPORTED: "PREPARATION_UNSUPPORTED",
    "cancelled": "PREPARATION_CANCELLED",
}
_SOURCE_KINDS = {
    src.AUTH: env.AUTH,
    src.TRANSIENT: env.TRANSIENT,
    src.NOT_FOUND: env.UNAVAILABLE,
    src.MISMATCH: env.MISMATCH,
    src.UNSUPPORTED: env.UNSUPPORTED,
}


class PreparationFailure(Exception):
    def __init__(self, error: dict):
        super().__init__(error.get("message"))
        self.error = error

    @property
    def kind(self) -> str:
        return self.error["kind"]

    @property
    def code(self) -> str:
        return self.error["code"]

    @property
    def retryable(self) -> bool:
        """Only a confirmed transient cause is retried; auth and config need a fix."""
        return self.kind == env.TRANSIENT


def failure(kind: str, message: str, evidence: dict | None = None, **extra) -> dict:
    error = {
        "kind": kind,
        "code": CODES.get(kind, "PREPARATION_RESTORE_FAILED"),
        "message": redact(message)[:2000],
        "evidence": {
            k: redact(v) if isinstance(v, str) else v for k, v in (evidence or {}).items()
        },
        **extra,
    }
    if kind == env.AUTH:
        error["suggested_actions"] = [{"type": "check_dataset_credential"}]
    return error


def _now() -> str:
    return clock.to_text(clock.now())


async def gather_or_cancel(*awaitables) -> list:
    """``asyncio.gather`` that does not leave siblings behind: when one fails, or this
    task is cancelled, the others are cancelled and awaited before the error moves on.
    A sibling still waiting on a shared restore would otherwise join it after its
    owner has released everything, and be handed a pin nothing releases."""
    tasks = [asyncio.ensure_future(item) for item in awaitables]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


@dataclass
class TaskContext:
    """Everything a restore needs about one task, read in one transaction."""

    task_id: str
    task_key: str
    spec_digest: str
    spec: dict | None
    """None for a task recorded as unsupported: it has no preparation spec."""
    content_digest: str
    material_digest: str
    locator: dict
    source_kind: str
    credential_id: str | None
    expected: PreparedOutput | None = None
    """The output a restore must reproduce: the admission evidence, which a revoked
    admission keeps, so that runs accepted before the revocation still start."""


@dataclass
class _Active:
    op_id: str
    lease: str
    generation: int
    preparation_id: str
    done: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    cancelling: bool = False


def load_context(conn: Connection, task_id: str) -> TaskContext:
    row = (
        conn.execute(
            select(
                t.tasks,
                t.datasets.c.source_kind,
                t.datasets.c.credential_id,
            )
            .join(t.dataset_versions, t.dataset_versions.c.id == t.tasks.c.dataset_version_id)
            .join(t.datasets, t.datasets.c.id == t.dataset_versions.c.dataset_id)
            .where(t.tasks.c.id == task_id)
        )
        .mappings()
        .one()
    )
    definition = row["definition_json"]
    # A revoked admission is an explicit stop for new work; runs accepted earlier keep
    # the snapshot and its recorded output (02 section 5).
    admission = (
        conn.execute(
            select(t.task_admissions.c.evidence_json)
            .where(
                t.task_admissions.c.task_id == task_id,
                t.task_admissions.c.state.in_(["admitted", "revoked"]),
            )
            .order_by(t.task_admissions.c.revision.desc())
        )
        .scalars()
        .first()
    )
    output = (admission or {}).get("output")
    return TaskContext(
        task_id=task_id,
        task_key=row["task_key"],
        spec_digest=row["spec_digest"],
        spec=(definition.get("preparation") or {}).get("spec"),
        content_digest=row["content_digest"],
        material_digest=row["material_digest"],
        locator=(definition.get("source") or {}).get("locator") or {},
        source_kind=row["source_kind"],
        credential_id=row["credential_id"],
        expected=PreparedOutput.from_json(output) if output else None,
    )


def recorded_outputs(conn: Connection, exclude_task_ids=()) -> list[PreparedOutput]:
    """Every preparation result still on record: cache rows and admission evidence (an
    evicted cache keeps its output; a purged version has neither)."""
    rows = conn.execute(
        select(t.preparations.c.output_json).where(t.preparations.c.output_json.is_not(None))
    ).scalars()
    found = [PreparedOutput.from_json(row) for row in rows]
    evidence = conn.execute(
        select(t.task_admissions.c.evidence_json).where(
            t.task_admissions.c.evidence_json.is_not(None),
            t.task_admissions.c.task_id.not_in(list(exclude_task_ids)),
        )
    ).scalars()
    found += [PreparedOutput.from_json(e["output"]) for e in evidence if e.get("output")]
    return found


def in_use(conn: Connection, exclude: str) -> frozenset[str]:
    """Image digests another cache needs locally now: pinned, or being restored. Tasks
    share identical images (N-01), so evicting one task must not pull them from under
    another."""
    busy = select(t.preparations.c.output_json).where(
        t.preparations.c.id != exclude,
        t.preparations.c.output_json.is_not(None),
        (t.preparations.c.active_operation_id.is_not(None))
        | t.preparations.c.id.in_(select(t.preparation_pins.c.preparation_id)),
    )
    return frozenset(
        image.digest
        for output in conn.execute(busy).scalars()
        for image in PreparedOutput.from_json(output).images
    )


def known_images(conn: Connection) -> dict[str, env.PreparedImage]:
    """Recorded images by definition key, so an identical definition is reused (N-01)."""
    return {image.key: image for output in recorded_outputs(conn) for image in output.images}


def set_availability(
    conn: Connection, digest: str, resource_kind: str, availability: str, evidence: dict
) -> None:
    """Content availability per digest and resource (02 section 5). Callers decide
    when evidence is strong enough; a timeout never makes content unavailable."""
    at = _now()
    row = (
        conn.execute(
            select(t.content_availability).where(
                t.content_availability.c.content_digest == digest,
                t.content_availability.c.resource_kind == resource_kind,
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        conn.execute(
            insert(t.content_availability).values(
                id=new_id("cav"),
                content_digest=digest,
                resource_kind=resource_kind,
                availability=availability,
                evidence_json=evidence,
                checked_at=at,
                revision=1,
            )
        )
    else:
        conn.execute(
            update(t.content_availability)
            .where(t.content_availability.c.id == row["id"])
            .values(
                availability=availability,
                evidence_json=evidence,
                checked_at=at,
                revision=row["revision"] + 1,
            )
        )


class Preparer:
    def __init__(
        self,
        db: Database,
        environment: Environment,
        sources: dict[str, DatasetSource],
        materials: TaskMaterials,
        credentials,
        *,
        restore_slots: int = RESTORE_SLOTS,
        build_timeout_max: float = MAX_BUILD_TIMEOUT,
        job_slots: int = 2,
    ):
        self.build_timeout_max = build_timeout_max
        self.db = db
        self.environment = environment
        self.sources = sources
        self.materials = materials
        self.credentials = credentials
        self.epoch: str | None = None
        self.active: dict[str, _Active] = {}
        self.jobs: dict[str, asyncio.Task] = {}
        self.restore_slot_count = restore_slots
        self._slots: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None
        self.job_slots = job_slots
        self.wake = None  # set by the worker: wakes its loop after a state change

    # --- lifecycle ------------------------------------------------------------------

    def start_epoch(self, epoch: str, instance_id: str) -> dict:
        """Preparation work of an earlier process is over (02 section 8): its
        Operations are interrupted, restoring caches fall back, waiters and pins of
        that process are dropped. Admission and its evidence are untouched."""
        self.epoch = epoch
        bind = getattr(self.environment, "bind", None)
        if bind is not None:
            bind(instance_id)
        report = {"interrupted_operations": [], "reset_preparations": []}
        at = _now()
        with self.db.write() as conn:
            stale = (
                conn.execute(
                    select(t.operations.c.id).where(
                        t.operations.c.kind.in_(KINDS),
                        t.operations.c.status.not_in([s.value for s in OPERATION_TERMINAL]),
                    )
                )
                .scalars()
                .all()
            )
            for op_id in stale:
                operations.advance(
                    conn,
                    op_id,
                    status=OperationStatus.INTERRUPTED,
                    error={"code": "SERVICE_RESTARTED", "message": "Service restarted"},
                )
                report["interrupted_operations"].append(op_id)
            rows = (
                conn.execute(
                    select(t.preparations).where(
                        (t.preparations.c.cache_state == "restoring")
                        | t.preparations.c.eviction_operation_id.is_not(None)
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                conn.execute(
                    update(t.preparations)
                    .where(t.preparations.c.id == row["id"])
                    .values(
                        cache_state="stale" if row["output_json"] else "absent",
                        active_operation_id=None,
                        eviction_operation_id=None,
                        last_restore_error_json=(
                            failure(env.TRANSIENT, "Service restarted during preparation")
                            if row["cache_state"] == "restoring"
                            else row["last_restore_error_json"]
                        ),
                    )
                )
                report["reset_preparations"].append(row["id"])
            conn.execute(
                update(t.preparation_waiters)
                .where(t.preparation_waiters.c.state == "waiting")
                .values(state="failed", ended_at=at)
            )
            conn.execute(delete(t.preparation_pins))
        return report

    async def recover(self) -> dict:
        """Start-up sweep of what an interrupted process left behind. It runs before any
        import or preparation starts, so it cannot race a live fetch or install."""
        sweep = getattr(self.environment, "sweep_temporary", None)
        removed = await sweep() if sweep is not None else []
        sweep = getattr(self.environment, "sweep_legacy", None)
        files = await asyncio.to_thread(sweep) if sweep is not None else []
        with self.db.read() as conn:
            referenced = set(conn.execute(select(t.tasks.c.content_digest)).scalars())
        files += await asyncio.to_thread(self.materials.sweep, referenced)
        report = {"temporary_images_removed": removed}
        if files:
            report["leftovers_removed"] = files
        return report

    async def stop(self) -> None:
        tasks = [a.task for a in self.active.values() if a.task] + list(self.jobs.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def idle(self) -> None:
        """Test helper: wait until no job or restore is running."""
        while self.jobs or self.active:
            tasks = [a.task for a in self.active.values() if a.task] + list(self.jobs.values())
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)

    def _wake(self) -> None:
        if self.wake is not None:
            self.wake()

    def restore_slots(self) -> asyncio.Semaphore:
        """Concurrent restores per process; bound to the running loop."""
        loop = asyncio.get_running_loop()
        if self._slots is None or self._slots[0] is not loop:
            self._slots = (loop, asyncio.Semaphore(self.restore_slot_count))
        return self._slots[1]

    async def import_and_prepare(self, request: dict, owner_id: str | None) -> dict:
        """Import and first-prepare in this process, without the worker loop.
        For the development command and tests; the API goes through Operations."""
        request = {
            "prepare": True,
            "credential_id": None,
            "accept_changed_content": False,
            **request,
        }
        with self.db.write() as conn:
            op_id = operations.create(
                conn, KIND_IMPORT, owner_id=owner_id, progress={"request": request}
            )
        await self._job(op_id, self._import)
        with self.db.read() as conn:
            done = conn.execute(select(t.operations).where(t.operations.c.id == op_id)).mappings()
            done = done.one()
        result = {"import": operations.public(done)}
        first = (done["result_json"] or {}).get("preparation_operation_id")
        if first:
            await self._job(first, self._first)
            with self.db.read() as conn:
                row = conn.execute(select(t.operations).where(t.operations.c.id == first))
                result["preparation"] = operations.public(row.mappings().one())
        result["dataset_version_id"] = (done["result_json"] or {}).get("dataset_version_id")
        return result

    # --- admin operations ------------------------------------------------------------

    async def tick(self) -> None:
        """Start accepted import, first-preparation and eviction Operations."""
        room = self.job_slots - len(self.jobs)
        if room <= 0:
            return
        with self.db.read() as conn:
            pending = conn.execute(
                select(t.operations.c.id, t.operations.c.kind)
                .where(
                    t.operations.c.kind.in_([KIND_IMPORT, KIND_FIRST, KIND_EVICT]),
                    t.operations.c.status == OperationStatus.ACCEPTED,
                )
                .order_by(t.operations.c.created_at, t.operations.c.id)
                .limit(room + len(self.jobs))
            ).all()
        runner = {KIND_IMPORT: self._import, KIND_FIRST: self._first, KIND_EVICT: self._evict}
        for op_id, kind in pending:
            if op_id in self.jobs or len(self.jobs) >= self.job_slots:
                continue
            self.jobs[op_id] = asyncio.create_task(self._job(op_id, runner[kind]))

    async def _job(self, op_id: str, body) -> None:
        try:
            with self.db.write() as conn:
                op = (
                    conn.execute(select(t.operations).where(t.operations.c.id == op_id))
                    .mappings()
                    .one()
                )
                if op["status"] != OperationStatus.ACCEPTED:
                    return
                operations.advance(conn, op_id, status=OperationStatus.RUNNING)
                conn.execute(
                    update(t.operations)
                    .where(t.operations.c.id == op_id)
                    .values(service_epoch=self.epoch)
                )
            await body(op_id, op)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.exception("operation %s failed", op_id)
            with self.db.write() as conn:
                operations.advance(
                    conn,
                    op_id,
                    status=OperationStatus.FAILED,
                    error={"code": "INTERNAL_ERROR", "message": f"{type(error).__name__}"},
                )
        finally:
            self.jobs.pop(op_id, None)
            self._wake()

    def _credential(self, credential_id: str | None) -> str | None:
        if not credential_id:
            return None
        value, _ = self.credentials.resolve_value(credential_id)
        return value

    async def _fetch(self, source, task: SourceTask, credential, expected_material=None) -> str:
        """Fetch one task into the material cache; returns its material digest."""
        staging = await asyncio.to_thread(self.materials.staging)
        try:
            fetched = await source.fetch(task, staging / "task", credential)
            if fetched.content_digest != task.content_digest:
                raise SourceError(src.MISMATCH, "source returned different content")
            return await asyncio.to_thread(
                self.materials.install, fetched.path, task.content_digest, expected_material
            )
        finally:
            await asyncio.to_thread(shutil.rmtree, staging, True)

    async def _import(self, op_id: str, op) -> None:
        """Resolve, fetch and inspect a fixed version, then record it (02 section 3).
        The version is written in one transaction after every task is in place."""
        request = op["progress_json"]["request"]
        source = self.sources.get(request["source_kind"])
        if source is None:
            raise RuntimeError("source not configured")
        credential = self._credential(request.get("credential_id"))

        def progress(phase: str, **values) -> None:
            with self.db.write() as conn:
                operations.advance(
                    conn, op_id, phase=phase, progress={"request": request, **values}
                )

        def fail(code: str, message: str, **detail) -> None:
            with self.db.write() as conn:
                operations.advance(
                    conn,
                    op_id,
                    status=OperationStatus.FAILED,
                    error={"code": code, "message": redact(message), **detail},
                )

        progress("resolving")
        try:
            version = await source.describe(
                request["namespace"], request["name"], request["ref"], credential
            )
        except SourceError as error:
            fail(_import_code(error.kind), error.message, kind=error.kind, evidence=error.evidence)
            return
        with self.db.read() as conn:
            dataset = live_dataset(conn, request["source_kind"], version.namespace, version.name)
            if dataset is not None and deleted.state(conn, "dataset", dataset):
                fail(
                    "DATASET_RECYCLED",
                    "This dataset is in the recycle bin; restore it before adding versions",
                    dataset_id=dataset,
                )
                return
            # Purged versions are receipts only: the same content may be added again.
            versions = (
                conn.execute(
                    select(
                        t.dataset_versions.c.id,
                        t.dataset_versions.c.resolved_digest,
                        t.dataset_versions.c.upstream_ref,
                    )
                    .where(
                        t.dataset_versions.c.dataset_id == dataset,
                        t.dataset_versions.c.id.not_in(purged_ids("dataset_version")),
                    )
                    .order_by(t.dataset_versions.c.created_at.desc())
                ).all()
                if dataset
                else []
            )
            recycled = {v.id for v in versions if deleted.state(conn, "dataset_version", v.id)}
        existing = [(v.id, v.resolved_digest) for v in versions]
        previous_same_ref = next(
            (v for v in versions if v.upstream_ref == version.upstream_ref), None
        )
        same = next((v for v, d in existing if d == version.resolved_digest), None)
        if same in recycled:
            fail(
                "DATASET_VERSION_RECYCLED",
                "This content is in the recycle bin; restore that version instead",
                dataset_version_id=same,
            )
            return
        if same:
            await self._reverify(same, version, source, credential, op_id)
            self._import_done(op_id, request, same, existing=True)
            return
        if (
            previous_same_ref
            and previous_same_ref.resolved_digest != version.resolved_digest
            and not request.get("accept_changed_content")
        ):
            fail(
                "CONTENT_REFERENCE_CHANGED",
                "This reference now resolves to different content; confirm it as a new version",
                previous_version_id=previous_same_ref.id,
                previous_digest=previous_same_ref.resolved_digest,
                resolved_digest=version.resolved_digest,
            )
            return
        tasks = []
        for index, task in enumerate(version.tasks):
            progress(
                "downloading",
                resolved_digest=version.resolved_digest,
                tasks_total=len(version.tasks),
                tasks_done=index,
            )
            try:
                material = await self._fetch(source, task, credential)
                path = self.materials.path_for(task.content_digest)
                definition = await asyncio.to_thread(
                    inspect_task, path, task.content_digest, material, self.build_timeout_max
                )
            except SourceError as error:
                if error.kind != src.UNSUPPORTED:
                    fail(
                        _import_code(error.kind),
                        f"{task.task_key}: {error.message}",
                        kind=error.kind,
                        task_key=task.task_key,
                        evidence=error.evidence,
                    )
                    return
                # Recorded but never admissible: first preparation reports why.
                material = None
                definition = {"unsupported": ["manifest"], "unsupported_reason": error.message}
            definition["source"] = {"locator": task.locator}
            tasks.append((task, material, definition))
        progress("recording", resolved_digest=version.resolved_digest, tasks_total=len(tasks))
        version_id = self._record_version(op, request, version, tasks)
        self._import_done(op_id, request, version_id, existing=False)

    async def _reverify(self, version_id, version, source, credential, op_id) -> None:
        """Importing a known version again re-checks task content marked unavailable:
        the Hub cannot tell deleted from no longer visible, so an administrator can
        bring it back once the same digest is offered again (02 section 5)."""
        with self.db.read() as conn:
            lost = conn.execute(
                select(t.tasks.c.content_digest, t.tasks.c.material_digest)
                .join(
                    t.content_availability,
                    t.content_availability.c.content_digest == t.tasks.c.content_digest,
                )
                .where(
                    t.tasks.c.dataset_version_id == version_id,
                    t.content_availability.c.resource_kind == "task_content",
                    t.content_availability.c.availability == "unavailable",
                )
            ).all()
        by_digest = {task.content_digest: task for task in version.tasks}
        for digest, material in lost:
            if digest not in by_digest:
                continue
            try:
                await self._fetch(source, by_digest[digest], credential, material or None)
            except SourceError:
                continue  # still not offered: it stays unavailable
            with self.db.write() as conn:
                set_availability(
                    conn, digest, "task_content", "available", {"reverified_by": op_id}
                )
        await self._reverify_images(version_id, op_id)

    async def _reverify_images(self, version_id: str, op_id: str) -> None:
        """The same for a prepared image recorded as unavailable (a rebuild gave a
        different image). A blocked task never triggers a restore, so nothing else would
        look again: ask the environment for the recorded image once more (local copy,
        original, or an identical rebuild). A restore that succeeds publishes the cache
        and marks the image available (``_publish``); one that fails changes nothing."""
        with self.db.read() as conn:
            admitted = conn.execute(
                select(t.tasks.c.id, t.task_admissions.c.evidence_json)
                .join(t.task_admissions, t.task_admissions.c.task_id == t.tasks.c.id)
                .where(
                    t.tasks.c.dataset_version_id == version_id,
                    t.task_admissions.c.state == "admitted",
                )
            ).all()
            images = {
                task: ((e or {}).get("output") or {}).get("content_id") for task, e in admitted
            }
            lost = set(
                conn.execute(
                    select(t.content_availability.c.content_digest).where(
                        t.content_availability.c.resource_kind == "environment_image",
                        t.content_availability.c.availability == "unavailable",
                        t.content_availability.c.content_digest.in_(
                            [image for image in images.values() if image]
                        ),
                    )
                ).scalars()
            )
            contexts = [load_context(conn, task) for task, image in images.items() if image in lost]
        for ctx in contexts:
            try:
                output = await self.acquire(ctx, "operation", op_id)
            except PreparationFailure:
                continue  # still cannot be brought back: it stays unavailable
            finally:
                self.release("operation", op_id, task_id=ctx.task_id)
            with self.db.write() as conn:  # (a cache that was still ready never published)
                set_availability(
                    conn,
                    output.content_id,
                    "environment_image",
                    "available",
                    {"reverified_by": op_id, "spec_digest": ctx.spec_digest},
                )

    def _record_version(self, op, request, version, tasks) -> str:
        at = _now()
        with self.db.write() as conn:
            dataset = live_dataset(conn, request["source_kind"], version.namespace, version.name)
            if dataset is None:
                dataset = new_id("ds")
                conn.execute(
                    insert(t.datasets).values(
                        id=dataset,
                        source_kind=request["source_kind"],
                        namespace=version.namespace,
                        name=version.name,
                        description=version.description,
                        enabled=True,
                        credential_id=request.get("credential_id"),
                        created_at=at,
                    )
                )
            elif request.get("credential_id"):
                conn.execute(
                    update(t.datasets)
                    .where(t.datasets.c.id == dataset)
                    .values(credential_id=request["credential_id"])
                )
            found = conn.execute(
                select(t.dataset_versions.c.id).where(
                    t.dataset_versions.c.dataset_id == dataset,
                    t.dataset_versions.c.resolved_digest == version.resolved_digest,
                    t.dataset_versions.c.id.not_in(purged_ids("dataset_version")),
                )
            ).scalar()
            if found:  # a concurrent import of the same content won
                return found
            version_id = new_id("dsv")
            materials = [m for _, m, _ in tasks if m]
            conn.execute(
                insert(t.dataset_versions).values(
                    id=version_id,
                    dataset_id=dataset,
                    upstream_ref=version.upstream_ref,
                    ref_kind=version.ref_kind,
                    resolved_digest=version.resolved_digest,
                    materialization_digest=digest_json(sorted(materials)),
                    manifest_json={
                        "converter_version": CONVERTER_VERSION,
                        "description": version.description,
                        "detail": version.detail,
                        "task_count": len(tasks),
                        "credential_id": request.get("credential_id"),
                    },
                    status="ready",
                    created_by=op["owner_id"],
                    operation_id=op["id"],
                    created_at=at,
                )
            )
            for task, material, definition in tasks:
                task_id = new_id("task")
                spec_digest = (definition.get("preparation") or {}).get("spec_digest")
                conn.execute(
                    insert(t.tasks).values(
                        id=task_id,
                        dataset_version_id=version_id,
                        task_key=task.task_key,
                        definition_json=definition,
                        content_digest=task.content_digest,
                        material_digest=material or "",
                        spec_digest=spec_digest or "",
                    )
                )
                conn.execute(
                    insert(t.task_admissions).values(
                        id=new_id("adm"),
                        task_id=task_id,
                        preparation_method_digest=PREPARATION_METHOD_DIGEST,
                        state="pending",
                        revision=1,
                        evidence_json={},
                    )
                )
                if material:
                    set_availability(
                        conn,
                        task.content_digest,
                        "task_content",
                        "available",
                        {"verified_by": op["id"], "material_digest": material},
                    )
        return version_id

    def _import_done(self, op_id: str, request: dict, version_id: str, existing: bool) -> None:
        result = {"dataset_version_id": version_id, "existing": existing}
        with self.db.write() as conn:
            if request.get("prepare", True):
                pending = (
                    conn.execute(
                        select(t.tasks.c.id)
                        .join(t.task_admissions, t.task_admissions.c.task_id == t.tasks.c.id)
                        .where(
                            t.tasks.c.dataset_version_id == version_id,
                            t.task_admissions.c.state.in_(["pending", "first_failed"]),
                        )
                    )
                    .scalars()
                    .all()
                )
                active = first_in_progress(conn, version_id)
                if pending and not active:
                    op = (
                        conn.execute(select(t.operations).where(t.operations.c.id == op_id))
                        .mappings()
                        .one()
                    )
                    result["preparation_operation_id"] = create_first(
                        conn, op["owner_id"], version_id, list(pending)
                    )
                elif active:
                    result["preparation_operation_id"] = active
            operations.set_resource(conn, op_id, "dataset_version", version_id)
            operations.advance(
                conn, op_id, status=OperationStatus.SUCCEEDED, phase="done", result=result
            )

    async def _first(self, op_id: str, op) -> None:
        """Admin first preparation and its retry: admission per task (02 section 5)."""
        task_ids = op["progress_json"]["task_ids"]
        results: dict[str, dict] = {}

        def record_progress(phase: str) -> None:
            with self.db.write() as conn:
                operations.advance(
                    conn,
                    op_id,
                    phase=phase,
                    progress={
                        "task_ids": task_ids,
                        "tasks_total": len(task_ids),
                        "tasks_done": len(results),
                        "admitted": sum(1 for r in results.values() if r["state"] == "admitted"),
                        "failed": sum(1 for r in results.values() if r["state"] == "first_failed"),
                    },
                )

        async def one(task_id: str) -> None:
            with self.db.read() as conn:
                ctx = load_context(conn, task_id)
            try:
                ctx, definition = await self._reinspect(ctx)
                if definition.get("unsupported"):
                    reason = definition.get("unsupported_reason")
                    raise PreparationFailure(
                        failure(
                            env.UNSUPPORTED,
                            "Task is outside what the platform can prepare: "
                            + ", ".join(definition["unsupported"])
                            + (f" ({reason})" if reason else ""),
                            {"reason": reason},
                        )
                    )
                output = await self.acquire(ctx, "operation", op_id, allow_new=True)
                results[task_id] = self._admit(op, ctx, output)
            except PreparationFailure as error:
                results[task_id] = self._first_failed(op, ctx, error.error)
            finally:
                self.release("operation", op_id, task_id=task_id)
                record_progress("preparing")

        record_progress("preparing")
        await gather_or_cancel(*(one(task_id) for task_id in task_ids))
        failed = [k for k, r in results.items() if r["state"] != "admitted"]
        with self.db.write() as conn:
            operations.advance(
                conn,
                op_id,
                status=OperationStatus.FAILED if failed else OperationStatus.SUCCEEDED,
                phase="done",
                result={"tasks": results},
                error=(
                    {
                        "code": "FIRST_PREPARATION_INCOMPLETE",
                        "message": f"{len(failed)} of {len(results)} tasks were not admitted",
                        "failed_task_ids": failed,
                    }
                    if failed
                    else None
                ),
            )

    async def _reinspect(self, ctx: TaskContext) -> tuple[TaskContext, dict]:
        """Inspect a task that is not admitted yet again from its material: how the
        platform prepares may have changed since the import (U-53). An admitted task keeps
        what it was admitted with."""
        with self.db.read() as conn:
            recorded = conn.execute(
                select(t.tasks.c.definition_json).where(t.tasks.c.id == ctx.task_id)
            ).scalar()
        if not ctx.material_digest:  # recorded as unsupported at import
            return ctx, recorded

        async def quiet(phase: str) -> None:
            pass

        path = await self._material(ctx, quiet)
        try:
            definition = await asyncio.to_thread(
                inspect_task, path, ctx.content_digest, ctx.material_digest, self.build_timeout_max
            )
        except SourceError:
            return ctx, recorded
        definition["source"] = recorded.get("source") or {}
        if definition == recorded:
            return ctx, recorded
        with self.db.write() as conn:
            conn.execute(
                update(t.tasks)
                .where(t.tasks.c.id == ctx.task_id)
                .values(
                    definition_json=definition,
                    spec_digest=definition["preparation"]["spec_digest"],
                )
            )
            return load_context(conn, ctx.task_id), definition

    def _admit(self, op, ctx: TaskContext, output: PreparedOutput) -> dict:
        at = _now()
        evidence = {
            "operation_id": op["id"],
            "verified_at": at,
            "spec_digest": ctx.spec_digest,
            "preparation_method_digest": PREPARATION_METHOD_DIGEST,
            "task_content_digest": ctx.content_digest,
            "material_digest": ctx.material_digest,
            "output": output.as_json(),
        }
        with self.db.write() as conn:
            admission = (
                conn.execute(
                    select(t.task_admissions).where(
                        t.task_admissions.c.task_id == ctx.task_id,
                        t.task_admissions.c.preparation_method_digest == PREPARATION_METHOD_DIGEST,
                    )
                )
                .mappings()
                .one()
            )
            if admission["state"] not in {"pending", "first_failed"}:
                return {"state": admission["state"], "skipped": True}
            conn.execute(
                update(t.task_admissions)
                .where(t.task_admissions.c.id == admission["id"])
                .values(
                    state="admitted",
                    revision=admission["revision"] + 1,
                    evidence_json=evidence,
                    approved_by=op["owner_id"],
                    approved_at=at,
                )
            )
            set_availability(
                conn,
                output.content_id,
                "environment_image",
                "available",
                {"verified_by": op["id"], "spec_digest": ctx.spec_digest},
            )
        return {"state": "admitted", "content_id": output.content_id}

    def _first_failed(self, op, ctx: TaskContext, error: dict) -> dict:
        with self.db.write() as conn:
            admission = (
                conn.execute(
                    select(t.task_admissions).where(
                        t.task_admissions.c.task_id == ctx.task_id,
                        t.task_admissions.c.preparation_method_digest == PREPARATION_METHOD_DIGEST,
                    )
                )
                .mappings()
                .one()
            )
            if admission["state"] not in {"pending", "first_failed"}:
                return {"state": admission["state"], "skipped": True}
            conn.execute(
                update(t.task_admissions)
                .where(t.task_admissions.c.id == admission["id"])
                .values(
                    state="first_failed",
                    revision=admission["revision"] + 1,
                    evidence_json={"operation_id": op["id"], "error": error, "at": _now()},
                )
            )
        return {"state": "first_failed", "error": error}

    async def _evict(self, op_id: str, op) -> None:
        """Remove a local image; the original and every admission stay (02 section 8)."""
        preparation_id = op["resource_id"]
        with self.db.write() as conn:
            row = (
                conn.execute(select(t.preparations).where(t.preparations.c.id == preparation_id))
                .mappings()
                .one()
            )
            pinned = conn.execute(
                select(func.count())
                .select_from(t.preparation_pins)
                .where(t.preparation_pins.c.preparation_id == preparation_id)
            ).scalar()
            reason = None
            if row["cache_state"] != "ready" or not row["output_json"]:
                reason = "NOT_READY"
            elif row["active_operation_id"] or row["eviction_operation_id"]:
                reason = "PREPARATION_BUSY"
            elif pinned:
                reason = "PREPARATION_IN_USE"
            if reason:
                operations.advance(
                    conn,
                    op_id,
                    status=OperationStatus.FAILED,
                    error={"code": reason, "message": "The cache cannot be evicted now"},
                )
                return
            # Claim first: from here nobody pins it, then the image goes.
            conn.execute(
                update(t.preparations)
                .where(t.preparations.c.id == preparation_id)
                .values(cache_state="stale", eviction_operation_id=op_id)
            )
            keep = in_use(conn, exclude=preparation_id)
        status, error = OperationStatus.SUCCEEDED, None
        try:
            await self.environment.evict(PreparedOutput.from_json(row["output_json"]), keep)
        except PreparationError as exc:
            status = OperationStatus.FAILED
            error = {"code": "EVICTION_FAILED", "message": redact(exc.message)}
        except Exception as exc:  # the docker client could not even be started, ...
            log.exception("eviction %s failed", op_id)
            status = OperationStatus.FAILED
            error = {"code": "EVICTION_FAILED", "message": redact(f"{type(exc).__name__}: {exc}")}
        # Shutting down mid-eviction (CancelledError) leaves the claim: the next start
        # reconciles it as interrupted rather than inventing an outcome.
        with self.db.write() as conn:
            conn.execute(
                update(t.preparations)
                .where(t.preparations.c.id == preparation_id)
                .values(eviction_operation_id=None)
            )
            operations.advance(conn, op_id, status=status, error=error, phase="done")

    # --- acquire / shared restore -----------------------------------------------------

    async def acquire(
        self,
        ctx: TaskContext,
        holder_kind: str,
        holder_id: str,
        *,
        cancel: asyncio.Event | None = None,
        deadline_at: str | None = None,
        allow_new: bool = False,
    ) -> PreparedOutput:
        """Return a pinned, verified preparation for the holder, restoring it if needed.

        ``allow_new`` lets the admin's first preparation produce a result nobody has
        recorded yet; runs may only bring back the admitted output.
        """
        if ctx.spec is None:
            raise PreparationFailure(
                failure(env.UNSUPPORTED, "The task has no preparation spec", {"task": ctx.task_key})
            )
        while True:
            if cancel is not None and cancel.is_set():
                raise PreparationFailure(failure("cancelled", "Waiting was cancelled"))
            pinned = self._pin_ready(ctx, holder_kind, holder_id, deadline_at)
            if isinstance(pinned, dict):
                raise PreparationFailure(pinned)
            if pinned is not None:
                preparation_id, generation, output = pinned
                try:
                    present = await self._check(output)
                except PreparationFailure:
                    self._drop_pin(preparation_id, holder_kind, holder_id)
                    raise
                if present:
                    return output
                self._drop_pin(preparation_id, holder_kind, holder_id, stale_generation=generation)
                continue
            decision, op_id = self._join(ctx, holder_kind, holder_id, deadline_at, allow_new)
            active = self.active.get(op_id) if op_id else None
            if decision == "wait":
                await self._wait(active.done if active else None, cancel, EVICTION_POLL)
                continue
            try:
                listening = await self._wait(active.done, cancel, None)
            except asyncio.CancelledError:
                # The holder is gone (its run was closed, the service is stopping): it
                # must not stay a waiter and be handed a pin when the restore ends.
                self._leave(op_id, holder_kind, holder_id)
                raise
            if not listening:
                self._leave(op_id, holder_kind, holder_id)
                raise PreparationFailure(failure("cancelled", "Waiting was cancelled"))
            outcome = self._outcome(op_id, ctx, holder_kind, holder_id)
            if isinstance(outcome, PreparedOutput):
                return outcome
            if outcome is None:
                continue  # superseded without an answer for us: try again
            raise PreparationFailure(outcome)

    @staticmethod
    async def _wait(done: asyncio.Event | None, cancel: asyncio.Event | None, poll) -> bool:
        waits = []
        if done is not None:
            waits.append(asyncio.ensure_future(done.wait()))
        if cancel is not None:
            waits.append(asyncio.ensure_future(cancel.wait()))
        try:
            if not waits:
                await asyncio.sleep(poll)
                return True
            await asyncio.wait(waits, timeout=poll, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waits:
                waiter.cancel()
        return not (cancel is not None and cancel.is_set())

    def _holder_columns(self, holder_kind: str, holder_id: str) -> dict:
        if holder_kind == "run":
            return {"run_id": holder_id}
        return {"holder_operation_id": holder_id}

    def _pin_ready(self, ctx, holder_kind, holder_id, deadline_at):
        with self.db.write() as conn:
            row = self._row(conn, ctx)
            if row is None or row["cache_state"] != "ready" or row["eviction_operation_id"]:
                return None
            output = PreparedOutput.from_json(row["output_json"])
            if ctx.expected and output.content_id != ctx.expected.content_id:
                # Never hand out something other than the admitted result.
                return failure(
                    env.MISMATCH,
                    "The cached preparation differs from the admitted result",
                    {
                        "expected_digest": ctx.expected.content_id,
                        "actual_digest": output.content_id,
                    },
                )
            self._pin(conn, row["id"], holder_kind, holder_id, deadline_at)
            conn.execute(
                update(t.preparations)
                .where(t.preparations.c.id == row["id"])
                .values(last_used_at=_now())
            )
            return row["id"], row["generation"], output

    async def _check(self, output: PreparedOutput) -> bool:
        """Is the image there? A check that cannot answer (the 60 s inspect timeout, the
        docker client missing) fails like a restore does: classified, and retried only
        when it is transient."""
        try:
            return await self.environment.check(output)
        except PreparationError as error:
            raise PreparationFailure(failure(error.kind, error.message, error.evidence)) from None
        except Exception as error:  # an unknown cause is reported, never guessed
            log.exception("cache check failed")
            raise PreparationFailure(
                failure(env.TRANSIENT, f"{type(error).__name__}: {error}")
            ) from None

    def _drop_pin(
        self, preparation_id, holder_kind, holder_id, *, stale_generation: int | None = None
    ) -> None:
        """Take the holder's pin back; with ``stale_generation``, also record that the
        image of that generation turned out to be missing."""
        with self.db.write() as conn:
            conn.execute(
                delete(t.preparation_pins).where(
                    t.preparation_pins.c.preparation_id == preparation_id,
                    t.preparation_pins.c.holder_kind == holder_kind,
                    t.preparation_pins.c.holder_id == holder_id,
                )
            )
            if stale_generation is not None:
                conn.execute(
                    update(t.preparations)
                    .where(
                        t.preparations.c.id == preparation_id,
                        t.preparations.c.generation == stale_generation,
                        t.preparations.c.cache_state == "ready",
                    )
                    .values(cache_state="stale")
                )

    def _row(self, conn, ctx: TaskContext):
        return (
            conn.execute(
                select(t.preparations).where(
                    t.preparations.c.spec_digest == ctx.spec_digest,
                    t.preparations.c.location_id == self.environment.location_id,
                )
            )
            .mappings()
            .first()
        )

    @staticmethod
    def _pin(conn, preparation_id, holder_kind, holder_id, deadline_at) -> None:
        expires = deadline_at or clock.to_text(clock.now() + clock.timedelta(hours=PIN_HOURS))
        exists = conn.execute(
            select(t.preparation_pins.c.id).where(
                t.preparation_pins.c.preparation_id == preparation_id,
                t.preparation_pins.c.holder_kind == holder_kind,
                t.preparation_pins.c.holder_id == holder_id,
            )
        ).first()
        if exists is None:
            conn.execute(
                insert(t.preparation_pins).values(
                    id=new_id("pin"),
                    preparation_id=preparation_id,
                    holder_kind=holder_kind,
                    holder_id=holder_id,
                    expires_at=expires,
                )
            )

    def _join(self, ctx, holder_kind, holder_id, deadline_at, allow_new):
        """Join the active restore for this key or start a new generation."""
        start = None
        with self.db.write() as conn:
            row = self._row(conn, ctx)
            if row is not None and row["eviction_operation_id"]:
                return "wait", None
            if row is not None and row["cache_state"] == "ready":
                return "wait", None  # published meanwhile: take the fast path
            active = self.active.get(row["active_operation_id"]) if row else None
            if row is not None and row["cache_state"] == "restoring" and active is not None:
                if active.cancelling:
                    return "wait", active.op_id  # never join a generation being torn down
                self._add_waiter(conn, active.op_id, holder_kind, holder_id, deadline_at)
                return "joined", active.op_id
            expected = (
                PreparedOutput.from_json(row["output_json"])
                if row is not None and row["output_json"]
                else ctx.expected
            )
            if expected is None and not allow_new:
                raise PreparationFailure(
                    failure(env.UNSUPPORTED, "No admitted preparation result is recorded")
                )
            at = _now()
            lease = secrets.token_urlsafe(24)
            op_id = operations.create(
                conn,
                KIND_RESTORE,
                owner_id=None,
                progress={"spec_digest": ctx.spec_digest, "task_key": ctx.task_key},
                phase="waiting",
            )
            if row is None:
                preparation_id = new_id("prep")
                generation = 1
                conn.execute(
                    insert(t.preparations).values(
                        id=preparation_id,
                        task_id=ctx.task_id,
                        spec_digest=ctx.spec_digest,
                        location_id=self.environment.location_id,
                        cache_state="restoring",
                        generation=generation,
                        active_operation_id=op_id,
                    )
                )
            else:
                preparation_id = row["id"]
                generation = row["generation"] + 1
                conn.execute(
                    update(t.preparations)
                    .where(t.preparations.c.id == preparation_id)
                    .values(
                        cache_state="restoring", generation=generation, active_operation_id=op_id
                    )
                )
            conn.execute(
                update(t.operations)
                .where(t.operations.c.id == op_id)
                .values(
                    status=OperationStatus.RUNNING,
                    resource_type="preparation",
                    resource_id=preparation_id,
                    lease_token=lease,
                    service_epoch=self.epoch,
                    updated_at=at,
                )
            )
            self._add_waiter(conn, op_id, holder_kind, holder_id, deadline_at)
            start = _Active(op_id, lease, generation, preparation_id)
            self.active[op_id] = start
        start.task = asyncio.create_task(self._restore(start, ctx, expected))
        return "joined", op_id

    def _add_waiter(self, conn, op_id, holder_kind, holder_id, deadline_at) -> None:
        columns = self._holder_columns(holder_kind, holder_id)
        found = conn.execute(
            select(t.preparation_waiters.c.id).where(
                t.preparation_waiters.c.operation_id == op_id,
                *[getattr(t.preparation_waiters.c, k) == v for k, v in columns.items()],
            )
        ).first()
        if found:
            conn.execute(
                update(t.preparation_waiters)
                .where(t.preparation_waiters.c.id == found.id)
                .values(state="waiting", ended_at=None)
            )
            return
        conn.execute(
            insert(t.preparation_waiters).values(
                id=new_id("pwt"),
                operation_id=op_id,
                deadline_at=deadline_at or clock.to_text(clock.now() + clock.timedelta(hours=24)),
                state="waiting",
                created_at=_now(),
                **columns,
            )
        )
        waiting = conn.execute(
            select(func.count())
            .select_from(t.preparation_waiters)
            .where(
                t.preparation_waiters.c.operation_id == op_id,
                t.preparation_waiters.c.state == "waiting",
            )
        ).scalar()
        op = conn.execute(
            select(t.operations.c.progress_json).where(t.operations.c.id == op_id)
        ).scalar()
        operations.advance(conn, op_id, progress={**op, "waiters": waiting})

    def _leave(self, op_id: str, holder_kind: str, holder_id: str) -> None:
        """A waiter leaves; the last one to leave ends the restore (02 section 7.4)."""
        columns = self._holder_columns(holder_kind, holder_id)
        with self.db.write() as conn:
            conn.execute(
                update(t.preparation_waiters)
                .where(
                    t.preparation_waiters.c.operation_id == op_id,
                    t.preparation_waiters.c.state == "waiting",
                    *[getattr(t.preparation_waiters.c, k) == v for k, v in columns.items()],
                )
                .values(state="cancelled", ended_at=_now())
            )
            waiting = conn.execute(
                select(func.count())
                .select_from(t.preparation_waiters)
                .where(
                    t.preparation_waiters.c.operation_id == op_id,
                    t.preparation_waiters.c.state == "waiting",
                )
            ).scalar()
            active = self.active.get(op_id)
            if waiting == 0 and active is not None and not active.done.is_set():
                active.cancelling = True
                operations.advance(conn, op_id, phase="cancelling")
        if waiting == 0 and active is not None and active.task and not active.done.is_set():
            active.task.cancel()

    def _outcome(self, op_id, ctx, holder_kind, holder_id):
        columns = self._holder_columns(holder_kind, holder_id)
        with self.db.read() as conn:
            waiter = conn.execute(
                select(t.preparation_waiters.c.state).where(
                    t.preparation_waiters.c.operation_id == op_id,
                    *[getattr(t.preparation_waiters.c, k) == v for k, v in columns.items()],
                )
            ).scalar()
            op = (
                conn.execute(select(t.operations).where(t.operations.c.id == op_id))
                .mappings()
                .one()
            )
            if waiter == "released":
                return PreparedOutput.from_json(op["result_json"]["output"])
            if waiter == "failed" and op["error_json"]:
                return {**op["error_json"], "operation_id": op_id}
        return None

    async def _restore(self, active: _Active, ctx: TaskContext, expected) -> None:
        """One attempt for one generation; publication re-checks the lease."""
        op_id = active.op_id

        async def progress(phase: str) -> None:
            with self.db.write() as conn:
                operations.advance(conn, op_id, phase=phase)

        try:
            async with self.restore_slots():
                path = await self._material(ctx, progress)
                with self.db.read() as conn:
                    network = setup_network.current(conn)
                    known = known_images(conn)
                spec = PreparationSpec(
                    ctx.spec_digest, ctx.task_key, path, ctx.spec, network, known
                )
                if expected is None:
                    output = await self.environment.prepare(spec, progress)
                else:
                    output = await self.environment.restore(spec, expected, progress)
            self._publish(active, ctx, output)
        except asyncio.CancelledError:
            self._fail(
                active, ctx, failure("cancelled", "No run is waiting any more"), cancelled=True
            )
        except PreparationFailure as error:
            self._fail(active, ctx, error.error)
        except PreparationError as error:
            self._fail(
                active, ctx, failure(error.kind, error.message, error.evidence), expected=expected
            )
        except Exception as error:  # an unknown cause is reported, never guessed
            log.exception("restore %s failed", op_id)
            self._fail(active, ctx, failure(env.TRANSIENT, f"{type(error).__name__}: {error}"))
        finally:
            active.done.set()
            self.active.pop(op_id, None)
            self._wake()

    async def _material(self, ctx: TaskContext, progress) -> object:
        path = await asyncio.to_thread(
            self.materials.verified, ctx.content_digest, ctx.material_digest
        )
        if path is not None:
            return path
        # Upstream may have dropped it; only a fetch that fails for content reasons
        # (not found / different) makes it unavailable (02 section 5).
        await progress("downloading")
        source = self.sources.get(ctx.source_kind)
        if source is None:
            raise PreparationFailure(
                failure(env.UNSUPPORTED, "The dataset source is not configured")
            )
        task = SourceTask(ctx.task_key, ctx.content_digest, ctx.locator)
        try:
            credential = self._credential(ctx.credential_id)
            await self._fetch(source, task, credential, ctx.material_digest)
        except SourceError as error:
            kind = _SOURCE_KINDS.get(error.kind, env.TRANSIENT)
            evidence = {**error.evidence, "expected_digest": ctx.content_digest}
            if kind in (env.UNAVAILABLE, env.MISMATCH):
                with self.db.write() as conn:
                    set_availability(
                        conn,
                        ctx.content_digest,
                        "task_content",
                        "unavailable",
                        {"reason": error.kind, "message": redact(error.message), **evidence},
                    )
            raise PreparationFailure(
                failure(kind, error.message, evidence, resource_kind="task_content")
            ) from None
        except Exception as error:
            # Disabled or deleted: an administrator has to act, retrying cannot help.
            code = getattr(error, "code", None)
            if code in ("CREDENTIAL_DISABLED", "CREDENTIAL_DELETED"):
                state = "disabled" if code == "CREDENTIAL_DISABLED" else "deleted"
                raise PreparationFailure(
                    failure(env.AUTH, f"The dataset credential is {state}")
                ) from None
            raise
        path = await asyncio.to_thread(
            self.materials.verified, ctx.content_digest, ctx.material_digest
        )
        if path is None:
            raise PreparationFailure(failure(env.TRANSIENT, "Task material did not verify"))
        with self.db.write() as conn:
            set_availability(
                conn, ctx.content_digest, "task_content", "available", {"refetched": True}
            )
        return path

    def _current(self, conn, active: _Active) -> bool:
        row = conn.execute(
            select(t.preparations.c.generation, t.preparations.c.active_operation_id).where(
                t.preparations.c.id == active.preparation_id
            )
        ).one()
        lease = conn.execute(
            select(t.operations.c.lease_token).where(t.operations.c.id == active.op_id)
        ).scalar()
        return (
            row.generation == active.generation
            and row.active_operation_id == active.op_id
            and lease == active.lease
        )

    def _publish(self, active: _Active, ctx: TaskContext, output: PreparedOutput) -> None:
        """Atomically: ready + output, pins handed to every current waiter (02 section 7.5)."""
        at = _now()
        with self.db.write() as conn:
            if not self._current(conn, active):
                log.warning("restore %s lost its lease; result discarded", active.op_id)
                # Its waiters get no answer: they join the generation that took over,
                # and these rows end here instead of staying open forever.
                conn.execute(
                    update(t.preparation_waiters)
                    .where(
                        t.preparation_waiters.c.operation_id == active.op_id,
                        t.preparation_waiters.c.state == "waiting",
                    )
                    .values(state="cancelled", ended_at=at)
                )
                operations.advance(
                    conn,
                    active.op_id,
                    status=OperationStatus.FAILED,
                    error={"code": "STALE_PREPARATION_LEASE", "message": "Superseded"},
                )
                return
            conn.execute(
                update(t.preparations)
                .where(t.preparations.c.id == active.preparation_id)
                .values(
                    cache_state="ready",
                    output_json=output.as_json(),
                    prepared_at=at,
                    last_used_at=at,
                    active_operation_id=None,
                    last_restore_error_json=None,
                )
            )
            waiters = (
                conn.execute(
                    select(t.preparation_waiters).where(
                        t.preparation_waiters.c.operation_id == active.op_id,
                        t.preparation_waiters.c.state == "waiting",
                    )
                )
                .mappings()
                .all()
            )
            for waiter in waiters:
                kind, holder = (
                    ("run", waiter["run_id"])
                    if waiter["run_id"]
                    else ("operation", waiter["holder_operation_id"])
                )
                self._pin(conn, active.preparation_id, kind, holder, waiter["deadline_at"])
            conn.execute(
                update(t.preparation_waiters)
                .where(
                    t.preparation_waiters.c.operation_id == active.op_id,
                    t.preparation_waiters.c.state == "waiting",
                )
                .values(state="released", ended_at=at)
            )
            if output.images:
                set_availability(
                    conn,
                    output.content_id,
                    "environment_image",
                    "available",
                    {"verified_by": active.op_id, "spec_digest": ctx.spec_digest},
                )
            operations.advance(
                conn,
                active.op_id,
                status=OperationStatus.SUCCEEDED,
                phase="done",
                result={"output": output.as_json(), "waiters": len(waiters)},
            )

    def _fail(
        self, active: _Active, ctx: TaskContext, error: dict, *, cancelled=False, expected=None
    ):
        """Waiters get the cause; the cache falls back; admission never changes."""
        with self.db.write() as conn:
            if self._current(conn, active):
                row = conn.execute(
                    select(t.preparations.c.output_json).where(
                        t.preparations.c.id == active.preparation_id
                    )
                ).scalar()
                conn.execute(
                    update(t.preparations)
                    .where(t.preparations.c.id == active.preparation_id)
                    .values(
                        cache_state="stale" if row else "absent",
                        active_operation_id=None,
                        last_restore_error_json=error,
                    )
                )
            if error.get("resource_kind") == "task_content" and error["kind"] in (
                env.UNAVAILABLE,
                env.MISMATCH,
            ):
                error = {
                    **error,
                    "code": "RESOURCE_CONTENT_UNAVAILABLE",
                    "expected_digest": ctx.content_digest,
                }
            elif error["kind"] in (env.UNAVAILABLE, env.MISMATCH) and expected is not None:
                # No copy of the recorded image is left and the source cannot give it back.
                set_availability(
                    conn,
                    expected.content_id,
                    "environment_image",
                    "unavailable",
                    {"operation_id": active.op_id, **error.get("evidence", {})},
                )
                error = {
                    **error,
                    "code": "RESOURCE_CONTENT_UNAVAILABLE",
                    "resource_kind": "environment_image",
                    "expected_digest": expected.content_id,
                }
            conn.execute(
                update(t.preparation_waiters)
                .where(
                    t.preparation_waiters.c.operation_id == active.op_id,
                    t.preparation_waiters.c.state == "waiting",
                )
                .values(state="failed", ended_at=_now())
            )
            operations.advance(
                conn,
                active.op_id,
                status=OperationStatus.CANCELLED if cancelled else OperationStatus.FAILED,
                phase="done",
                error=error,
            )

    def release(self, holder_kind: str, holder_id: str, *, task_id: str | None = None) -> None:
        """Drop the holder's pins (all of them, or the one for ``task_id``'s spec)."""
        with self.db.write() as conn:
            query = delete(t.preparation_pins).where(
                t.preparation_pins.c.holder_kind == holder_kind,
                t.preparation_pins.c.holder_id == holder_id,
            )
            if task_id is not None:
                query = query.where(
                    t.preparation_pins.c.preparation_id.in_(
                        select(t.preparations.c.id)
                        .join(t.tasks, t.tasks.c.spec_digest == t.preparations.c.spec_digest)
                        .where(t.tasks.c.id == task_id)
                    )
                )
            conn.execute(query)


def purged_ids(object_type: str):
    return select(t.deletions.c.object_id).where(
        t.deletions.c.object_type == object_type, t.deletions.c.state == deleted.PURGED
    )


def live_dataset(conn: Connection, source_kind: str, namespace: str, name: str) -> str | None:
    """The dataset row for this identity that is not purged (a purge keeps a receipt
    row, and the same identity may be added again as a new dataset)."""
    return conn.execute(
        select(t.datasets.c.id).where(
            t.datasets.c.source_kind == source_kind,
            t.datasets.c.namespace == namespace,
            t.datasets.c.name == name,
            t.datasets.c.id.not_in(purged_ids("dataset")),
        )
    ).scalar()


def _import_code(kind: str) -> str:
    return {
        src.AUTH: "SOURCE_AUTH_FAILED",
        src.TRANSIENT: "SOURCE_UNAVAILABLE_TRY_AGAIN",
        src.NOT_FOUND: "SOURCE_VERSION_NOT_FOUND",
        src.MISMATCH: "SOURCE_CONTENT_MISMATCH",
        src.UNSUPPORTED: "SOURCE_MANIFEST_UNSUPPORTED",
    }.get(kind, "SOURCE_FAILED")


def first_in_progress(conn: Connection, version_id: str) -> str | None:
    return conn.execute(
        select(t.operations.c.id).where(
            t.operations.c.kind == KIND_FIRST,
            t.operations.c.resource_type == "dataset_version",
            t.operations.c.resource_id == version_id,
            t.operations.c.status.not_in([s.value for s in OPERATION_TERMINAL]),
        )
    ).scalar()


def create_first(conn: Connection, owner_id: str, version_id: str, task_ids: list[str]) -> str:
    return operations.create(
        conn,
        KIND_FIRST,
        owner_id=owner_id,
        resource_type="dataset_version",
        resource_id=version_id,
        progress={"task_ids": task_ids, "tasks_total": len(task_ids), "tasks_done": 0},
        phase="queued",
    )
