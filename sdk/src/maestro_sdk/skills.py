"""The agent skills shipped with this SDK, installed where agents look for skills.

The skills are the repository's ``skills/`` directory, packaged into the wheel, so an
installed skill always matches the SDK that runs its examples. Reinstall after upgrading
the SDK.
"""

from __future__ import annotations

import os
import shutil
from importlib import metadata, resources
from pathlib import Path

# Where Codex, OpenCode and other agents (.agents) and Claude Code (.claude) find skills.
TARGETS = (".agents/skills", ".claude/skills")
VERSION_FILE = ".maestro-sdk-version"


def source() -> Path:
    """The directory holding the shipped skills."""
    packaged = Path(str(resources.files("maestro_sdk") / "_skills"))
    if packaged.is_dir():
        return packaged
    # An editable install of the repository runs from sdk/src/maestro_sdk.
    checkout = Path(__file__).resolve().parents[3] / "skills"
    if checkout.is_dir():
        return checkout
    raise FileNotFoundError("This maestro-sdk installation ships no skills")


def names() -> list[str]:
    return sorted(p.name for p in source().iterdir() if (p / "SKILL.md").is_file())


def version() -> str:
    try:
        return metadata.version("maestro-sdk")
    except metadata.PackageNotFoundError:
        return "unknown"


def install(directory: str | os.PathLike[str] = ".", targets=TARGETS) -> list[Path]:
    """Copy the shipped skills into ``<directory>/<target>/<skill>`` for each target.

    A skill of the same name is replaced as a whole, so files dropped by a newer SDK go
    too; other skills in the targets are left alone. Each installed skill records the SDK
    version in ``.maestro-sdk-version``. Returns the installed skill directories.
    """
    root = Path(directory)
    installed = []
    for target in targets:
        for name in names():
            destination = root / target / name
            if destination.is_symlink() or destination.is_file():
                destination.unlink()
            elif destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(source() / name, destination)
            (destination / VERSION_FILE).write_text(version() + "\n")
            installed.append(destination)
    return installed


def installed_version(skill_directory: str | os.PathLike[str]) -> str | None:
    """The SDK version an installed skill came from, or None if unknown."""
    marker = Path(skill_directory) / VERSION_FILE
    return marker.read_text().strip() if marker.is_file() else None
