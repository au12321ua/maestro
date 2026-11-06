"""Final-attempt selection and output paths, mirrored by the standalone SDK."""

from collections.abc import Iterable, Mapping
from typing import Any, Literal

ArchiveContent = Literal["all", "deliverables"]


def final_attempt_id(
    result_attempt_id: str | None, attempts: Iterable[Mapping[str, Any]]
) -> str | None:
    """A result owns its attempt even if a later attempt exists."""
    if result_attempt_id is not None:
        return result_attempt_id
    last = max(attempts, key=lambda a: a["attempt_index"], default=None)
    return last["id"] if last is not None else None


def deliverable_path(relative: str) -> str | None:
    """Remove Harbor's collection prefix and omit its bookkeeping manifest."""
    if relative == "artifacts/manifest.json":
        return None
    collected = "artifacts/logs/artifacts/"
    if relative.startswith(collected):
        return relative.removeprefix(collected)
    return relative.removeprefix("artifacts/")
