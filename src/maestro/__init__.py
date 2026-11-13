"""Maestro: a team experiment service built on Harbor."""

import json
import sys
from importlib.metadata import version
from pathlib import Path

__version__ = version("maestro")


def installed_build(prefix: str | Path = sys.prefix) -> str | None:
    """Build name of the release package this service was installed from, e.g.
    ``0.4.1.dev0-921d0e7f95b7``. Development builds of one version share the version
    string, so the build (which carries the source commit) is what tells them apart.
    ``maestroctl`` puts the package's ``RELEASE.json`` next to the service environment;
    a source checkout has none and gets ``None``."""
    try:
        info = json.loads((Path(prefix).parent / "RELEASE.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or info.get("name") != "maestro":
        return None
    build = info.get("build")
    return build if isinstance(build, str) and build else None


__build__ = installed_build()
