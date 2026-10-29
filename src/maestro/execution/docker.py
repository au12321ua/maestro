"""Label-based Docker cleanup (05 sections 7-8, EXE-06).

Ending a Runner's process group does not stop what the Docker daemon runs for it.
Every container, network, volume and built image of a run carries the platform labels, so
cleanup removes exactly those and then confirms nothing labelled remains. Other
instances, unlabelled and manually created resources are never touched.
"""

import asyncio
import logging
import shutil

from maestro.boundaries.environment import CleanupReport

log = logging.getLogger(__name__)

LABEL_INSTANCE = "io.maestro.instance_id"
LABEL_RUN = "io.maestro.run_id"

_LIST = {
    "container": ["ps", "-aq", "--no-trunc"],
    "network": ["network", "ls", "-q", "--no-trunc"],
    "volume": ["volume", "ls", "-q"],
    "image": ["images", "-q", "--no-trunc"],
}
_REMOVE = {
    "container": ["rm", "-f", "-v"],
    "network": ["network", "rm"],
    "volume": ["volume", "rm"],
    "image": ["image", "rm"],
}
# Containers first: networks, volumes and images cannot go while a container uses them.
_ORDER = ("container", "network", "volume", "image")


class DockerError(RuntimeError):
    pass


class DockerResources:
    def __init__(self, docker: str | None = None, timeout: float = 120.0):
        self.docker = docker or shutil.which("docker") or "docker"
        self.timeout = timeout

    async def _docker(self, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            self.docker,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await process.communicate()
        if process.returncode:
            raise DockerError(err.decode(errors="replace").strip()[-500:])
        return out.decode()

    async def _list(self, kind: str, labels: dict[str, str]) -> list[str]:
        filters = [arg for k, v in sorted(labels.items()) for arg in ("--filter", f"label={k}={v}")]
        return sorted(set((await self._docker(*_LIST[kind], *filters)).split()))

    async def _sweep(self, labels: dict[str, str]) -> CleanupReport:
        errors = []
        for kind in _ORDER:
            ids = await self._list(kind, labels)
            if ids:
                try:
                    await self._docker(*_REMOVE[kind], *ids)
                except DockerError as error:
                    errors.append(f"{kind}: {error}")
        remaining = [
            f"{kind}:{item[:19]}" for kind in _ORDER for item in await self._list(kind, labels)
        ]
        if errors and remaining:
            log.warning("cleanup %s left %s: %s", labels, remaining, errors)
        return CleanupReport(released=not remaining, remaining=remaining)

    async def cleanup(self, labels: dict[str, str]) -> CleanupReport:
        # An empty label set (or an empty instance id) would sweep the whole host.
        if not labels.get(LABEL_INSTANCE):
            raise ValueError("cleanup must be scoped to an instance label")
        try:
            return await asyncio.wait_for(self._sweep(labels), self.timeout)
        except (TimeoutError, DockerError, OSError) as error:
            log.warning("cleanup %s failed: %s", labels, error)
            return CleanupReport(released=False, remaining=[f"error:{type(error).__name__}"])
