"""The images an author Compose file needs, read without the daemon or the host's
environment (02 section 6).

``docker compose config --no-interpolate`` parses the file; variables stay as written.
What decides an image's content (``image:`` and ``build:``) is frozen at the defaults the
author wrote, so members never change it (U-54). A variable there without a usable
default, or one that looks like a secret, cannot be prepared.
"""

import json
import math
import os
import re
import subprocess
from pathlib import Path

COMPOSE_FILE = "docker-compose.yaml"
MAIN = "main"
# Build options that do not change the image's content.
IGNORED_BUILD = {"labels", "cache_from", "cache_to", "tags"}
FROZEN_BUILD = {"context", "dockerfile", "args", "target"}

_SENSITIVE = re.compile(r"(KEY|SECRET|TOKEN(?!S)|PASSWORD|CREDENTIAL|AUTH)", re.IGNORECASE)
# ${NAME:-default} and ${NAME-default}; the default itself may not hold another variable.
_DEFAULTED = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(:?-)([^${}]*)\}")
_VARIABLE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
_MEMORY = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([bkmg]?)b?", re.IGNORECASE)
_MEMORY_MB = {"": 1 / 2**20, "b": 1 / 2**20, "k": 1 / 1024, "m": 1, "g": 1024}


class ComposeUnsupported(ValueError):
    """What makes a Compose file unpreparable; the message names the place, never a value."""


def check_runtime(document: dict, where: str) -> None:
    """Author mounts cannot reach the host (U-16); each service has one container.

    Keep this policy in step with maestro_runner.compose.check_runtime: the service and
    Runner have independent dependencies, and the Runner also checks older admissions.
    Neither variables nor volume drivers may turn an isolated volume into a host bind.
    """
    volumes = document.get("volumes") or {}
    for kind in ("networks", "volumes"):
        for name, settings in (document.get(kind) or {}).items():
            settings = settings or {}
            if settings.get("external") or settings.get("name"):
                raise ComposeUnsupported(
                    f"{where} {kind}.{name}: external or named resources are unsupported"
                )
            if kind == "volumes" and (
                settings.get("driver") not in (None, "local") or settings.get("driver_opts")
            ):
                raise ComposeUnsupported(f"{where} volumes.{name}: volume drivers are unsupported")
    for name, service in (document.get("services") or {}).items():
        service = service or {}
        place = f"{where} services.{name}"
        for key in (
            "volumes_from",
            "configs",
            "secrets",
            "devices",
            "use_api_socket",
            "privileged",
        ):
            if service.get(key):
                raise ComposeUnsupported(f"{place}: {key} is unsupported")
        for mount in service.get("volumes") or []:
            if isinstance(mount, str):
                source = mount.split(":", 1)[0] if ":" in mount else None
                kind = "volume"
            else:
                source, kind = mount.get("source"), mount.get("type")
            if kind not in ("volume", "tmpfs") or (
                source is not None and (source not in volumes or "$" in source)
            ):
                raise ComposeUnsupported(f"{place}: host mounts are unsupported")
        deploy = service.get("deploy") or {}
        if deploy.get("mode") not in (None, "replicated") or any(
            str(value) != "1" for value in (service.get("scale", 1), deploy.get("replicas", 1))
        ):
            raise ComposeUnsupported(f"{place}: only a fixed single replica is supported")


def read(directory: Path) -> dict | None:
    """The parsed ``docker-compose.yaml`` of an environment directory, or None."""
    path = directory / COMPOSE_FILE
    if not path.is_file():
        return None
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "-f",
            str(path.resolve()),
            "config",
            "--no-interpolate",
            "--no-normalize",
            "--no-consistency",
            "--format",
            "json",
        ],
        cwd=directory,
        env={"PATH": os.defpath + ":/usr/local/bin", "HOME": "/nonexistent"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise ComposeUnsupported(f"{path.parent.name}/{COMPOSE_FILE} cannot be read")
    document = json.loads(result.stdout)
    return document if isinstance(document, dict) else {}


def freeze(value, where: str, frozen: dict[str, str]) -> str:
    """``value`` with every variable replaced by the author's default; the defaults used
    are added to ``frozen``."""

    def replace(match: re.Match) -> str:
        name, _, default = match.groups()
        if _SENSITIVE.search(name):
            raise ComposeUnsupported(f"{where}: {name} looks like a secret and cannot be frozen")
        if frozen.setdefault(name, default) != default:
            raise ComposeUnsupported(f"{where}: {name} has different defaults")
        return default

    text = _DEFAULTED.sub(replace, str(value))
    if "$" in text.replace("$$", ""):
        found = _VARIABLE.search(text)
        name = found[1] if found else "a variable"
        raise ComposeUnsupported(f"{where}: {name} needs a default, as ${{{name}:-value}}")
    return text.replace("$$", "$")


def variables(value) -> set[str]:
    """Variable names used anywhere in a parsed value."""
    if isinstance(value, dict):
        return set().union(*(variables(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(variables(v) for v in value)) if value else set()
    if isinstance(value, str):
        return set(_VARIABLE.findall(value.replace("$$", "")))
    return set()


def cpus(value, where: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ComposeUnsupported(f"{where}: cpus must be a number") from None
    if not math.isfinite(number) or number <= 0:
        raise ComposeUnsupported(f"{where}: cpus must be positive")
    return number


def memory_mb(value, where: str) -> int:
    if isinstance(value, int | float) and not isinstance(value, bool):
        number, unit = float(value), ""
    else:
        match = _MEMORY.fullmatch(str(value).strip())
        if not match:
            raise ComposeUnsupported(f"{where}: memory limit {value!r} is not understood")
        number, unit = float(match[1]), match[2].lower()
    result = math.ceil(number * _MEMORY_MB[unit])
    if result <= 0:
        raise ComposeUnsupported(f"{where}: memory limit must be positive")
    return result


def limits(service: dict, where: str) -> dict:
    """The author's CPU and memory limits of a service; None where none is given."""
    deploy = ((service.get("deploy") or {}).get("resources") or {}).get("limits") or {}
    cpu = service.get("cpus", deploy.get("cpus"))
    memory = service.get("mem_limit", deploy.get("memory"))
    return {
        "cpus": None if cpu is None else cpus(cpu, where),
        "memory_mb": None if memory is None else memory_mb(memory, where),
    }
