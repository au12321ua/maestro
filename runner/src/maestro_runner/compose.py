"""Read author Compose with the Docker parser and enforce the runtime boundary.

This runs before Harbor can create any containers, including for older admissions.
The preparation-side policy is checked against this one by the same regression cases.
"""

import json
import os
import subprocess
from pathlib import Path

COMPOSE_FILE = "docker-compose.yaml"


def check_runtime(document: dict, where: str) -> None:
    """Author mounts cannot reach the host (U-16); each service has one container.

    Keep this policy in step with maestro.execution.compose.check_runtime: the service and
    Runner have independent dependencies, and the Runner also checks older admissions.
    Neither variables nor volume drivers may turn an isolated volume into a host bind.
    """
    volumes = document.get("volumes") or {}
    for kind in ("networks", "volumes"):
        for name, settings in (document.get(kind) or {}).items():
            settings = settings or {}
            if settings.get("external") or settings.get("name"):
                raise ValueError(
                    f"{where} {kind}.{name}: external or named resources are unsupported"
                )
            if kind == "volumes" and (
                settings.get("driver") not in (None, "local") or settings.get("driver_opts")
            ):
                raise ValueError(f"{where} volumes.{name}: volume drivers are unsupported")
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
                raise ValueError(f"{place}: {key} is unsupported")
        for mount in service.get("volumes") or []:
            if isinstance(mount, str):
                source = mount.split(":", 1)[0] if ":" in mount else None
                kind = "volume"
            else:
                source, kind = mount.get("source"), mount.get("type")
            if kind not in ("volume", "tmpfs") or (
                source is not None and (source not in volumes or "$" in source)
            ):
                raise ValueError(f"{place}: host mounts are unsupported")
        deploy = service.get("deploy") or {}
        if deploy.get("mode") not in (None, "replicated") or any(
            str(value) != "1" for value in (service.get("scale", 1), deploy.get("replicas", 1))
        ):
            raise ValueError(f"{place}: only a fixed single replica is supported")


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
        raise ValueError(f"{path.parent.name}/{COMPOSE_FILE} cannot be read")
    document = json.loads(result.stdout)
    document = document if isinstance(document, dict) else {}
    check_runtime(document, f"{directory.name}/{COMPOSE_FILE}")
    return document
