"""Maestro: a team experiment service built on Harbor."""

import json
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

__version__ = version("maestro")


def installed_build(prefix: str | Path = sys.prefix) -> str | None:
    """Build name of the release package this service was installed from, e.g.
    ``0.5.2-921d0e7f95b7-unreleased``. Development builds of one version share the version
    string, so the build (which carries the source commit) is what tells them apart.
    ``maestroctl`` puts the package's ``RELEASE.json`` next to the service environment;
    a source checkout has none and uses its Git commit instead."""
    try:
        info = json.loads((Path(prefix).parent / "RELEASE.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or info.get("name") != "maestro":
        return None
    build = info.get("build")
    return build if isinstance(build, str) and build else None


def source_build(root: Path | None = None) -> str | None:
    """Identify an editable source checkout without treating it as a released package."""
    root = root or Path(__file__).resolve().parents[2]
    if not (root / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=2,
        )
        changes = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    dirty = "-dirty" if changes.stdout.strip() else ""
    return f"{__version__}-{result.stdout.strip()[:12]}{dirty}-unreleased"


__build__ = installed_build() or source_build()
