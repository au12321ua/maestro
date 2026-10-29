"""Run environment boundary: task preparation and the local preparation cache (02).

The platform owns preparation: an environment builds or pulls every image a task's
trials start (the main environment, a separate verifier, Compose services) without
running an agent, keeps each in the local image registry by content (N-01), and later
brings exactly those images back. Business code keeps admission, availability and
cache state; the environment only reports what happened, classified by
``PreparationError``.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from maestro.core.digests import digest_json
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
    known: Mapping[str, "PreparedImage"] = field(default_factory=dict)
    """Images already recorded by definition key: an identical definition is reused,
    not built again (N-01)."""


@dataclass(frozen=True)
class PreparedImage:
    """One image of a preparation. ``digest`` is its manifest digest in the local registry,
    the content a restore must reproduce; ``ref`` names it by that digest and is what the
    executor runs; ``key`` identifies the author's definition, so identical ones share one
    image."""

    role: str
    """``<directory>/<service>``, e.g. ``environment/main``, ``tests/main``, ``environment/db``."""
    digest: str
    ref: str
    key: str
    detail: dict = field(default_factory=dict)

    def as_json(self) -> dict:
        return {
            "role": self.role,
            "digest": self.digest,
            "ref": self.ref,
            "key": self.key,
            "detail": self.detail,
        }

    @classmethod
    def from_json(cls, value: dict) -> "PreparedImage":
        return cls(
            role=value["role"],
            digest=value["digest"],
            ref=value["ref"],
            key=value["key"],
            detail=value.get("detail") or {},
        )


@dataclass(frozen=True)
class PreparedOutput:
    """Identity of a preparation result: every image, each pinned to its content."""

    images: tuple[PreparedImage, ...]
    detail: dict = field(default_factory=dict)

    @property
    def content_id(self) -> str:
        """The images as one content identity: what availability is recorded for and a
        restore must reproduce (02 section 5)."""
        return digest_json(sorted([i.role, i.digest] for i in self.images))

    def refs(self) -> dict[str, str]:
        return {i.role: i.ref for i in self.images}

    def as_json(self) -> dict:
        return {
            "content_id": self.content_id,
            "images": [i.as_json() for i in self.images],
            "detail": self.detail,
        }

    @classmethod
    def from_json(cls, value: dict) -> "PreparedOutput":
        return cls(
            images=tuple(PreparedImage.from_json(i) for i in value.get("images") or []),
            detail=value.get("detail") or {},
        )


Progress = Callable[[str], Awaitable[None]]
"""Called with a phase: downloading / pulling / building / checking / pushing / loading."""


@dataclass
class CleanupReport:
    released: bool
    remaining: list[str] = field(default_factory=list)


class Environment(Protocol):
    location_id: str

    async def prepare(self, spec: PreparationSpec, progress: Progress) -> PreparedOutput:
        """First preparation: build or pull every image (or reuse an identical definition
        already in the registry), pin each to its content and keep it in the registry."""
        ...

    async def restore(
        self, spec: PreparationSpec, expected: PreparedOutput, progress: Progress
    ) -> PreparedOutput:
        """Bring back exactly ``expected``: pull each image from the registry, or rebuild
        and compare. A different image raises MISMATCH and is never published."""
        ...

    async def check(self, expected: PreparedOutput) -> bool:
        """Is every expected image present locally right now?"""
        ...

    async def evict(self, expected: PreparedOutput, keep: frozenset[str] = frozenset()) -> None:
        """Remove the local images, except the digests in ``keep`` (another task uses
        them); the registry copies stay (U-17)."""
        ...

    async def discard(self, expected: PreparedOutput, keep: frozenset[str] = frozenset()) -> None:
        """The dataset version is purged (U-19): remove the local images and the registry
        copies, except the digests in ``keep`` that other preparations still record."""
        ...
