"""Shared preparation helpers for tests."""

from maestro.boundaries.environment import (
    PreparationSpec,
)

SPEC_DIGEST = "sha256:" + "5c" * 32


def build_entry(role="environment/main", context="environment") -> dict:
    return {
        "role": role,
        "source": "build",
        "context": context,
        "dockerfile": "Dockerfile",
        "args": {},
        "target": None,
        "definition_digest": "sha256:" + "de" * 32,
    }


def make_spec(tmp_path, kind="build", docker_image=None, known=None, **top) -> PreparationSpec:
    task = tmp_path / "task"
    (task / "environment").mkdir(parents=True, exist_ok=True)
    (task / "environment" / "Dockerfile").write_text("FROM scratch\n")
    if kind == "build":
        entry = build_entry()
    else:
        entry = {"role": "environment/main", "source": "pull", "reference": docker_image}
    spec = {"images": [entry], **top}
    return PreparationSpec(SPEC_DIGEST, "demo", task, spec, known=known or {})


async def no_progress(phase: str) -> None:
    return None
