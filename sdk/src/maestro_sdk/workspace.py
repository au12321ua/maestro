"""A local workspace for an agent working with Maestro (the recommended layout).

    <workspace>/
      AGENTS.md          the context an agent needs every time (fill it in)
      .agents/skills/    the shipped skills (also .claude/skills/), not committed
      notes/             free-form notes: hypotheses, evidence excerpts, what did not work
      attachments/<name>/  one git repository per attachment, not committed here

Nothing here talks to the service. The layout is a recommendation: notes have no format
and nothing reads them back.
"""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path

from maestro_sdk import skills

GITIGNORE = """\
# Installed by maestro_sdk.skills.install; reinstall after upgrading the SDK.
/.agents/skills/
/.claude/skills/
# Each attachment is its own git repository.
/attachments/
"""


def init(directory: str | os.PathLike[str] = ".") -> Path:
    """Create the workspace, or complete it: files that exist are never overwritten, and
    the skills are reinstalled from this SDK. Returns the workspace directory."""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    for folder in ("notes", "attachments"):
        (root / folder).mkdir(exist_ok=True)
    template = resources.files("maestro_sdk") / "templates" / "AGENTS.md"
    _create(root / "AGENTS.md", template.read_text(encoding="utf-8"))
    _create(root / ".gitignore", GITIGNORE)
    skills.install(root)
    return root


def _create(path: Path, text: str) -> None:
    if not path.exists() and not path.is_symlink():
        path.write_text(text, encoding="utf-8")
