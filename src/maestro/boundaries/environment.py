"""Run environment boundary: task preparation and the local preparation cache (02).

The platform owns preparation: an environment builds or pulls a task's image without
running an agent, exports the result as a recoverable original (U-17), and later
restores exactly that result. Business code keeps admission, availability and cache
state; the environment only reports what happened, classified by ``PreparationError``.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from maestro.domain.setup_network import BUILT_IN, SetupNetwork

# PreparationError kinds (06 section 3, platform preparation errors). Only TRANSIENT is
# retried; a cause that is not known to be transient is never retried.
TRANSIENT = "transient"  # network, registry or daemon trouble; the content may still exist
AUTH = "auth"  # registry or source refused access; cannot tell "gone" from "denied"
UNAVAILABLE = "unavailable"  # the exact content cannot be obtained any more
MISMATCH = "mismatch"  # obtained something, but not the recorded digest
UNSUPPORTED = "unsupported"  # the task's environment is outside what we can prepare
FAILED = "failed"  # a build or pull that fails the same way every time (bad Dockerfile, ...)


class PreparationError(Exception):
    def __init__(self, kind: str, message: str, evidence: dict | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.evidence = evidence or {}


@dataclass(frozen=True)
class PreparationSpec:
    spec_digest: str
    task_key: str
    task_dir: Path
    """Verified local task material (read only)."""
    spec: dict = field(default_factory=dict)
    """The normalized spec the digest covers (02 section 6)."""
    setup_network: SetupNetwork = BUILT_IN
    """In force when this preparation started (N-08); builds use its proxy."""


@dataclass(frozen=True)
class PreparedOutput:
    """Identity of a preparation result. ``image_id`` is the content identity a restore
    must reproduce; ``image_ref`` is the local name handed to the executor."""

    image_id: str
    image_ref: str
    archive: dict | None = None
    """Recoverable original: {"path", "digest", "size_bytes"}; None if export failed."""
    detail: dict = field(default_factory=dict)

    def as_json(self) -> dict:
        return {
            "image_id": self.image_id,
            "image_ref": self.image_ref,
            "archive": self.archive,
            "detail": self.detail,
        }

    @classmethod
    def from_json(cls, value: dict) -> "PreparedOutput":
        return cls(
            image_id=value["image_id"],
            image_ref=value["image_ref"],
            archive=value.get("archive"),
            detail=value.get("detail") or {},
        )


Progress = Callable[[str], Awaitable[None]]
"""Called with a phase: downloading / pulling / building / checking / exporting / loading."""


@dataclass
class CleanupReport:
    released: bool
    remaining: list[str] = field(default_factory=list)


class Environment(Protocol):
    location_id: str

    async def prepare(self, spec: PreparationSpec, progress: Progress) -> PreparedOutput:
        """First preparation: build or pull, pin to an image id, export the original."""
        ...

    async def restore(
        self, spec: PreparationSpec, expected: PreparedOutput, progress: Progress
    ) -> PreparedOutput:
        """Bring back exactly ``expected.image_id``: load the original, or rebuild and
        compare. A different result raises MISMATCH and is never published."""
        ...

    async def check(self, expected: PreparedOutput) -> bool:
        """Is the expected image present locally under its reference right now?"""
        ...

    async def evict(self, expected: PreparedOutput) -> None:
        """Remove the local image; the original archive stays (U-17)."""
        ...

    async def discard(self, expected: PreparedOutput) -> None:
        """Remove the local image and its original: the dataset version is purged (U-19)."""
        ...
