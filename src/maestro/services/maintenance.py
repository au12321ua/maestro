"""Background upkeep that runs with or without an executor: export builds, artifact
retention and export expiry (07 sections 6 and 7), expired attachment uploads, plus
start-up reconciliation."""

import asyncio
import logging
import time

from maestro.services.artifacts import Artifacts
from maestro.services.attachments import Attachments
from maestro.services.exports import Exports

log = logging.getLogger(__name__)
SWEEP_SECONDS = 60.0
UNINDEXED_SWEEP_SECONDS = 600.0
# Retention goes first and each step stands alone; builds per pass are bounded so a steady
# stream of exports, or one that keeps failing, cannot hold retention back.
BUILDS_PER_CYCLE = 4


class Maintenance:
    def __init__(
        self,
        artifacts: Artifacts,
        exports: Exports,
        attachments: Attachments | None = None,
        runs=None,
        *,
        poll_seconds: float = 1.0,
        sweep_seconds: float = SWEEP_SECONDS,
        unindexed_seconds: float = UNINDEXED_SWEEP_SECONDS,
    ):
        self.artifacts = artifacts
        self.exports = exports
        self.attachments = attachments
        self.runs = runs
        self.poll_seconds = poll_seconds
        self.sweep_seconds = sweep_seconds
        self.unindexed_seconds = unindexed_seconds
        self._last_unindexed = float("-inf")
        self._wake = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False

    def start(self) -> dict:
        """Before any run executes: index/file reconciliation and interrupted exports."""
        report = {
            "storage": self.artifacts.reconcile(),
            "exports_requeued": self.exports.recover(),
            "attachment_staging_removed": (
                self.attachments.store.discard_partial() if self.attachments else 0
            ),
        }
        return report

    def upkeep(self) -> dict:
        """Reclaim what has expired. Every step has its own failure handling: one that
        keeps failing must not stop the others."""
        steps = [("artifacts", self.artifacts.sweep), ("exports_expired", self.exports.expire)]
        if self.runs is not None:
            steps.append(("resolutions_removed", self.runs.sweep_resolutions))
        if self.attachments is not None:
            steps.append(("attachment_uploads_removed", self.attachments.sweep_uploads))
        # Walking the whole store is the costly step, so it runs less often.
        if time.monotonic() - self._last_unindexed >= self.unindexed_seconds:
            self._last_unindexed = time.monotonic()
            steps.append(("unindexed_files", self.artifacts.sweep_unindexed))
        report = {}
        for name, step in steps:
            try:
                report[name] = step()
            except Exception:
                log.exception("maintenance step %s failed", name)
        return report

    def run_once(self) -> dict:
        """Retention first, then a bounded number of export builds."""
        report = self.upkeep()
        built = 0
        while built < BUILDS_PER_CYCLE and (export_id := self.exports.next_queued()) is not None:
            self.exports.build(export_id)
            built += 1
        return {"exports_built": built, **report}

    def wake(self) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._wake.set)

    async def run_forever(self) -> None:
        self._loop = asyncio.get_running_loop()
        last_sweep = float("-inf")
        while not self._stopping:
            more = False
            if self._loop.time() - last_sweep >= self.sweep_seconds:
                last_sweep = self._loop.time()
                await asyncio.to_thread(self.upkeep)
            try:
                built = 0
                while (
                    built < BUILDS_PER_CYCLE
                    and (export_id := self.exports.next_queued()) is not None
                ):
                    await asyncio.to_thread(self.exports.build, export_id)
                    built += 1
                more = built == BUILDS_PER_CYCLE  # still queued: come back after retention
            except Exception:
                log.exception("export builds failed")
            if more:
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), self.poll_seconds)
            except TimeoutError:
                pass

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
