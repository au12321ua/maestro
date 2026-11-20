"""A local workspace for an agent working with Maestro (the recommended layout).

    <workspace>/
      AGENTS.md          the context an agent needs every time (fill it in)
      .env.example      service URL and path to the PAT file (copy to .env)
      update-maestro.py  sync the SDK and skills with the configured service release
      .agents/skills/    the shipped skills (also .claude/skills/), not committed
      notes/             free-form notes: hypotheses, evidence excerpts, what did not work
      attachments/<name>/  one git repository per attachment, not committed here

Creating the layout is a local operation. Notes have no format and nothing reads them back.
"""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path

from maestro_sdk import skills

GITIGNORE = """\
# Local workspace configuration.
/.env
# Installed by maestro_sdk.skills.install; reinstall after upgrading the SDK.
/.agents/skills/
/.claude/skills/
# Each attachment is its own git repository.
/attachments/
"""


def init(directory: str | os.PathLike[str] = ".", workflow: str | None = None) -> Path:
    """Create the workspace, or complete it: files that exist are never overwritten, and
    the skills are reinstalled from this SDK. ``workflow="attachments"`` also adds
    notes/attachments-workflow.md and references it in a newly created AGENTS.md.
    Returns the workspace directory. Unknown workflows raise ValueError."""
    if workflow not in (None, "attachments"):
        raise ValueError(f"Unknown workflow {workflow!r}; available workflows: attachments")
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    for folder in ("notes", "attachments"):
        (root / folder).mkdir(exist_ok=True)
    templates = resources.files("maestro_sdk") / "templates"
    context = (templates / "AGENTS.md").read_text(encoding="utf-8")
    if workflow == "attachments":
        notes = templates / "attachments-workflow.md"
        _create(root / "notes" / "attachments-workflow.md", notes.read_text(encoding="utf-8"))
        context += "\nAttachment workflow: read `notes/attachments-workflow.md`.\n"
    _create(root / "AGENTS.md", context)
    _create(root / ".gitignore", GITIGNORE)
    for filename in (".env.example", "update-maestro.py"):
        _create(root / filename, (templates / filename).read_text(encoding="utf-8"))
    skills.install(root)
    return root


def _create(path: Path, text: str) -> None:
    if not path.exists() and not path.is_symlink():
        path.write_text(text, encoding="utf-8")
