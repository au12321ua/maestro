"""Dataset source boundary: search, versions, a fixed version's tasks,
and the exact content of one task.

Sources map their failures to ``SourceError`` kinds. A timeout, 429, 5xx or missing
download permission is never evidence that content is gone for good.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# Failure kinds every adapter maps to.
AUTH = "auth"  # missing or rejected credential; fix the credential, not the content
TRANSIENT = "transient"  # network, rate limit, upstream error: retry later
NOT_FOUND = "not_found"  # the fixed content is not offered (any more)
MISMATCH = "mismatch"  # content was offered but its digest differs
UNSUPPORTED = "unsupported"  # manifest or feature outside the platform contract


class SourceError(Exception):
    def __init__(self, kind: str, message: str, evidence: dict | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.evidence = evidence or {}


@dataclass(frozen=True)
class SourceTask:
    task_key: str
    content_digest: str
    """Upstream content identity (Hub: the task package digest)."""
    locator: dict = field(default_factory=dict)
    """Source-specific way to fetch exactly this content, e.g. Hub package and ref."""


@dataclass(frozen=True)
class SourceVersion:
    namespace: str
    name: str
    upstream_ref: str
    ref_kind: str  # tag | revision | digest
    resolved_digest: str
    tasks: tuple[SourceTask, ...]
    description: str = ""
    detail: dict = field(default_factory=dict)
    """Display facts such as revision, tags, publish time; never used for identity."""


@dataclass(frozen=True)
class FetchedTask:
    path: Path
    content_digest: str
    """Verified against the requested upstream digest by the adapter."""


class DatasetSource(Protocol):
    kind: str

    async def search(self, query: str, credential: str | None) -> list[dict]:
        """External descriptions only; never creates versions or prepares anything."""
        ...

    async def list_versions(self, namespace: str, name: str, credential: str | None) -> list[dict]:
        """Explicit refs (revision, tags) with their resolved digests."""
        ...

    async def describe(
        self, namespace: str, name: str, ref: str, credential: str | None
    ) -> SourceVersion:
        """Resolve a tag/revision/digest to an immutable digest and its task list."""
        ...

    async def fetch(
        self, task: SourceTask, destination: Path, credential: str | None
    ) -> FetchedTask:
        """Materialize exactly ``task.content_digest`` as the new directory ``destination``."""
        ...
