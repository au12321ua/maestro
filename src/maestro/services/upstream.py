"""Upstream versions of the programs harness adapters install (N-09).

Harness CLIs release every few days. An administrator publishing a harness version sees
the release tags and recent releases of the adapter's npm package next to the version
this Maestro was tested with, and a version that does not exist upstream is refused at
publishing. The lookup goes through the setup network (its proxy and npm registry
mirror, 12 section 3), so it works wherever installs work. When it fails, the form still
takes a typed version and publishing does not depend on it.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from urllib.parse import quote, urlsplit

import httpx

from maestro.core import clock as wall
from maestro.domain.setup_network import SetupNetwork

REGISTRY = "https://registry.npmjs.org"
# Releases only: no pre-release, platform or snapshot builds.
RELEASE = re.compile(r"\d+\.\d+\.\d+")
# Tags shown, in this order; Claude Code has both, the others ``latest`` only.
TAGS = ("stable", "latest")
RECENT = 10
CACHE_SEC = 600
TIMEOUT_SEC = 30
# A package document lists every version ever published (Codex: about 19 MB).
MAX_BYTES = 64 * 1024 * 1024


class Unavailable(Exception):
    """The registry could not be asked or gave no usable answer."""


def _get(url: str, proxy: str | None) -> tuple[int, bytes]:
    """Status and body of a GET; ``proxy`` None means the service's own environment."""
    try:
        with (
            httpx.Client(
                proxy=proxy, trust_env=proxy is None, timeout=TIMEOUT_SEC, follow_redirects=True
            ) as client,
            client.stream("GET", url, headers={"Accept": "application/json"}) as response,
        ):
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    raise Unavailable(f"the answer from {url} is larger than {MAX_BYTES} bytes")
            return response.status_code, bytes(body)
    except httpx.HTTPError as error:
        raise Unavailable(f"{type(error).__name__}: {error}") from error


def registry(network: SetupNetwork) -> str:
    """The npm registry installs use: the setup network's ``npm_config_registry``."""
    for name, value in network.env.items():
        if name.lower() == "npm_config_registry" and value.strip():
            return value.strip().rstrip("/")
    return REGISTRY


def _proxy(network: SetupNetwork, url: str) -> str | None:
    if not network.proxy:
        return None
    host = urlsplit(url).hostname or ""
    for entry in network.no_proxy.split(","):
        entry = entry.strip().lstrip(".").lower()
        if entry and (host == entry or host.endswith("." + entry)):
            return None
    return network.proxy


class UpstreamVersions:
    def __init__(
        self,
        network_of: Callable[[], SetupNetwork],
        get: Callable[[str, str | None], tuple[int, bytes]] = _get,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.network_of = network_of
        self.get = get
        self.clock = clock
        self.lock = threading.Lock()
        # (package, registry, proxy) -> (monotonic time, summary)
        self.cache: dict[tuple, tuple[float, dict]] = {}

    def _url(self, base: str, package: str, version: str | None = None) -> str:
        url = f"{base}/{quote(package, safe='@')}"
        return url if version is None else f"{url}/{quote(version, safe='')}"

    def lookup(self, package: str, refresh: bool = False) -> dict:
        """Release tags and recent releases of ``package``; raises ``Unavailable``."""
        network = self.network_of()
        base = registry(network)
        url = self._url(base, package)
        proxy = _proxy(network, url)
        key = (package, base, proxy)
        with self.lock:
            cached = self.cache.get(key)
        if cached and not refresh and self.clock() - cached[0] < CACHE_SEC:
            return cached[1]
        status, body = self.get(url, proxy)
        if status != 200:
            raise Unavailable(f"{base} answered {status} for {package}")
        try:
            document = json.loads(body)
            times = document.get("time") or {}
            versions = set(document.get("versions") or {})
            tags = document.get("dist-tags") or {}
        except (ValueError, AttributeError) as error:
            raise Unavailable(f"{base} gave no package document for {package}") from error
        releases = sorted(
            (v for v in versions if RELEASE.fullmatch(v) and isinstance(times.get(v), str)),
            key=lambda v: times[v],
            reverse=True,
        )
        summary = {
            "registry": base,
            "checked_at": wall.to_text(wall.now()),
            "tags": [
                {"tag": tag, "version": tags[tag], "published_at": times.get(tags[tag])}
                for tag in TAGS
                if isinstance(tags.get(tag), str) and RELEASE.fullmatch(tags[tag])
            ],
            "recent": [{"version": v, "published_at": times[v]} for v in releases[:RECENT]],
            "versions": versions,
        }
        with self.lock:
            self.cache[key] = (self.clock(), summary)
        return summary

    def exists(self, package: str, version: str) -> bool | None:
        """Whether ``version`` of ``package`` is published; None when it cannot be told."""
        network = self.network_of()
        base = registry(network)
        with self.lock:
            cached = [
                summary
                for (name, where, _), (at, summary) in self.cache.items()
                if name == package and where == base and self.clock() - at < CACHE_SEC
            ]
        if cached and version in cached[0]["versions"]:
            return True
        url = self._url(base, package, version)
        try:
            status, _ = self.get(url, _proxy(network, url))
        except Unavailable:
            return None
        return True if status == 200 else False if status == 404 else None
