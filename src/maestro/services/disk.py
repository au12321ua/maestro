"""Storage watermarks.

Low free space first stops new runs from being accepted, then stops queued runs
from starting. Running work finishes its bookkeeping; nothing unexpired is deleted
to make room.
"""

import shutil
from collections.abc import Callable
from pathlib import Path

from maestro.config import Settings
from maestro.core.errors import MaestroError

GIB = 1024**3
OK = "ok"
ACCEPT_PAUSED = "accept_paused"
DISPATCH_PAUSED = "dispatch_paused"


class DiskWatch:
    def __init__(self, path: Path, settings: Settings, usage: Callable | None = None):
        self.path = path
        self.settings = settings
        self.usage = usage or shutil.disk_usage

    def _below(self, free: int, total: int, gib: float, percent: float) -> bool:
        return free < gib * GIB or (total > 0 and free * 100 < percent * total)

    def status(self) -> dict:
        probe = self.path if self.path.exists() else self.path.parent
        total, _, free = self.usage(probe)
        s = self.settings
        if self._below(
            free, total, s.storage_dispatch_min_free_gb, s.storage_dispatch_min_free_percent
        ):
            level = DISPATCH_PAUSED
        elif self._below(
            free, total, s.storage_accept_min_free_gb, s.storage_accept_min_free_percent
        ):
            level = ACCEPT_PAUSED
        else:
            level = OK
        return {
            "level": level,
            "free_bytes": free,
            "total_bytes": total,
            "accept_min_free": {
                "gib": s.storage_accept_min_free_gb,
                "percent": s.storage_accept_min_free_percent,
            },
            "dispatch_min_free": {
                "gib": s.storage_dispatch_min_free_gb,
                "percent": s.storage_dispatch_min_free_percent,
            },
        }

    def accepting(self) -> bool:
        return self.status()["level"] == OK

    def dispatching(self) -> bool:
        return self.status()["level"] != DISPATCH_PAUSED

    def refuse_if_low(self, work: str = "runs") -> None:
        if not self.accepting():
            raise MaestroError(
                "STORAGE_LOW",
                f"Free disk space is below the acceptance watermark; new {work} are paused",
                503,
                stage="acceptance",
                retryable=True,
                suggested_actions=[{"type": "contact_admin", "reason": "free_storage"}],
            )
