"""Long-running work and waiting for it."""

from __future__ import annotations

import random
import time
from collections.abc import Iterator

from maestro_sdk import models
from maestro_sdk._transport import _Resource
from maestro_sdk.errors import WaitTimeout

TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted"})


class Operations(_Resource):
    def get(self, operation_id: str) -> models.Operation:
        return self._c._get(f"operations/{operation_id}", models.Operation)

    def list(
        self, *, kind: str | None = None, owner_id: str | None = None, limit: int | None = None
    ) -> Iterator[models.Operation]:
        return self._c._iterate(
            "operations", models.Operation, kind=kind, owner_id=owner_id, limit=limit
        )

    def wait(
        self,
        operation_id: str,
        *,
        timeout: float = 3600.0,
        interval: float = 1.0,
        max_interval: float = 5.0,
    ) -> models.Operation:
        """Poll until the Operation ends (succeeded, failed, cancelled or interrupted).

        Polling starts at ``interval`` and slows to ``max_interval`` with jitter. On timeout
        WaitTimeout is raised and the work on the service continues; "no progress yet" is
        never a failure.
        """
        deadline = time.monotonic() + timeout
        delay = interval
        while True:
            operation = self.get(operation_id)
            if operation.status in TERMINAL:
                return operation
            left = deadline - time.monotonic()
            if left <= 0:
                raise WaitTimeout(operation, timeout)
            time.sleep(min(left, delay * (0.8 + 0.4 * random.random())))
            delay = min(max_interval, delay * 1.5)
