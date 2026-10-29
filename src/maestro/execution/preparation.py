"""Docker preparation environment (02 sections 5-8, M1 TECH-03, U-17, N-01).

The platform prepares every image a task's trials start itself, without Harbor and
without an agent: a reproducible ``buildx`` build of a directory, or a pull of the
author's image pinned to its repository digest. Each image is pushed to the local image
registry and identified by its manifest digest there; layers are stored once, however
many tasks share them. An identical definition (same files, build options and method)
is built once and reused by every task that has it.

Restore pulls each image from the registry by digest; only when the registry has lost
it does it rebuild or re-pull, and a different digest is a mismatch that is never
published. Work happens under a temporary tag, so a late or orphaned build cannot
replace a published image.
"""

import asyncio
import contextlib
import ctypes
import logging
import os
import re
import secrets
import shutil
import signal
from dataclasses import replace
from pathlib import Path

import httpx

from maestro.boundaries.environment import (
    AUTH,
    FAILED,
    MISMATCH,
    TRANSIENT,
    UNAVAILABLE,
    UNSUPPORTED,
    PreparationError,
    PreparationSpec,
    PreparedImage,
    PreparedOutput,
    Progress,
)
from maestro.execution.materials import MAX_BUILD_TIMEOUT, build_timeout, image_key

log = logging.getLogger(__name__)

LABEL_KEY = "io.maestro.image_key"
DEFAULT_REGISTRY = "127.0.0.1:8425"
DEFAULT_BUILD_TIMEOUT = 600.0
PULL_TIMEOUT = 1800.0
PUSH_TIMEOUT = 1800.0
MANIFESTS = (
    "application/vnd.oci.image.manifest.v1+json, application/vnd.oci.image.index.v1+json, "
    "application/vnd.docker.distribution.manifest.v2+json, "
    "application/vnd.docker.distribution.manifest.list.v2+json"
)
# Failure classification works on the docker client's stderr. Output of the Dockerfile's
# own RUN steps ("#7 3.2 ..." while building, "3.2 ..." in the failure summary) is the
# task's business, not the registry's: a pip "401 Unauthorized" in a RUN step is not a
# credential problem.
_STEP_OUTPUT = re.compile(r"^(#\d+ )?\d+(\.\d+)? ")
# Registry, pull and login refusals only.
_AUTH = re.compile(
    r"pull access denied|requested access to the resource is denied|"
    r"unauthorized: (authentication required|incorrect username or password)|"
    r"no basic auth credentials|authentication required|insufficient_scope|"
    r"failed to authorize\b.*\b(401|403)\b",
    re.IGNORECASE,
)
# "Exactly gone". The containerd image store says the last one for a digest the registry
# does not have (Docker 29).
_MISSING = re.compile(
    r"manifest unknown|not found: manifest|manifest for .* not found|"
    r"failed to resolve reference .*@sha256:[0-9a-f]{64}: not found",
    re.IGNORECASE,
)
# What is known to pass by itself; anything else (a Dockerfile error, a RUN step that
# exits non-zero, an unsupported platform) fails the same way again and is not retried.
_TRANSIENT = re.compile(
    r"tls handshake timeout|connection reset|i/o timeout|client\.timeout exceeded|"
    r"temporary failure (in name resolution|resolving)|server misbehaving|"
    r"context deadline exceeded|"
    r"bad gateway|service unavailable|gateway time-?out|internal server error|"
    r"toomanyrequests|too many requests|"
    r"unexpected eof|[\w\"]: eof\b|"
    r"connection refused|network is unreachable|no route to host|"
    r"cannot connect to the docker daemon",
    re.IGNORECASE,
)


# BuildKit says this when a RUN step itself failed. The base image was resolved fine and
# the build reached the task's own commands, so no registry refused anything. It also
# echoes the command text three times without a step prefix (the error line, the
# instruction header, the Dockerfile excerpt); that text is the task's, not Docker's.
_RUN_FAILED = re.compile(r"did not complete successfully")


def classify(stderr: str) -> str:
    """AUTH for a registry refusal, TRANSIENT for a known passing trouble, else FAILED."""
    lines = stderr.splitlines()
    if _RUN_FAILED.search(stderr):
        # Only what the step itself printed can show passing trouble (a flaky mirror).
        output = "\n".join(line for line in lines if _STEP_OUTPUT.match(line))
        return TRANSIENT if _TRANSIENT.search(output) else FAILED
    registry = "\n".join(line for line in lines if not _STEP_OUTPUT.match(line))
    if _AUTH.search(registry):
        return AUTH
    return TRANSIENT if _TRANSIENT.search(stderr) else FAILED


def _die_with_parent() -> None:
    """The docker client ends with the service; BuildKit then cancels the build."""
    with contextlib.suppress(OSError, AttributeError):
        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG


class DockerEnvironment:
    location_id = "local-docker"

    def __init__(
        self,
        registry: str = DEFAULT_REGISTRY,
        docker: str | None = None,
        *,
        build_timeout: float = DEFAULT_BUILD_TIMEOUT,
        build_timeout_max: float = MAX_BUILD_TIMEOUT,
        pull_timeout: float = PULL_TIMEOUT,
        push_timeout: float = PUSH_TIMEOUT,
        legacy_dir: Path | None = None,
    ):
        self.registry = registry
        self.docker = docker or shutil.which("docker") or "docker"
        self.path: str | None = None
        # A task's own build time is capped at ``build_timeout_max`` every time it builds,
        # so lowering the maximum also reaches versions imported earlier.
        self.build_timeout = build_timeout
        self.build_timeout_max = build_timeout_max
        self.pull_timeout = pull_timeout
        self.push_timeout = push_timeout
        self.legacy_dir = legacy_dir
        # One producer per definition: a second task with it waits and reuses the result,
        # also when both started before either was recorded.
        self._locks: dict[str, asyncio.Lock] = {}
        self._produced: dict[str, PreparedImage] = {}

    def bind(self, instance_id: str) -> None:
        """Images live under a per-instance repository, so instances never collide."""
        self.path = f"maestro-prep/{instance_id.lower()}"

    @property
    def repository(self) -> str:
        if self.path is None:
            raise RuntimeError("environment is not bound to an instance")
        return f"{self.registry}/{self.path}"

    def ref(self, digest: str) -> str:
        return f"{self.repository}@{digest}"

    def tag(self, key: str) -> str:
        return f"{self.repository}:{key.split(':', 1)[1]}"

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    # --- docker client ---------------------------------------------------------------

    async def _run(self, *args: str, timeout: float) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            self.docker,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            preexec_fn=_die_with_parent,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except (TimeoutError, asyncio.CancelledError) as error:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGTERM)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 10)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            if isinstance(error, asyncio.CancelledError):
                raise
            raise PreparationError(TRANSIENT, f"docker {args[0]} timed out after {timeout}s")
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")

    async def _image_id(self, ref: str) -> str | None:
        code, out, _ = await self._run("image", "inspect", "--format", "{{.Id}}", ref, timeout=60)
        value = out.strip()
        return value if code == 0 and value.startswith("sha256:") else None

    async def _untag(self, ref: str) -> None:
        await self._run("image", "rm", "--no-prune", ref, timeout=120)

    async def _present(self, ref: str) -> bool:
        return await self._image_id(ref) is not None

    # --- the registry ------------------------------------------------------------------

    # The registry is local: proxy settings meant for the internet never apply to it.
    def _manifest_url(self, reference: str) -> str:
        return f"http://{self.registry}/v2/{self.path}/manifests/{reference}"

    async def _registry_digest(self, key: str) -> str:
        """The manifest digest the registry holds under a definition's tag."""
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                response = await client.head(
                    self._manifest_url(key.split(":", 1)[1]), headers={"Accept": MANIFESTS}
                )
        except httpx.HTTPError as error:
            raise PreparationError(TRANSIENT, f"the image registry is unreachable: {error}")
        digest = response.headers.get("Docker-Content-Digest", "")
        if response.status_code != 200 or not digest.startswith("sha256:"):
            raise PreparationError(
                TRANSIENT,
                f"the image registry did not report the pushed image ({response.status_code})",
            )
        return digest

    async def _delete_manifest(self, digest: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                response = await client.delete(self._manifest_url(digest))
        except httpx.HTTPError as error:
            raise PreparationError(TRANSIENT, f"the image registry is unreachable: {error}")
        if response.status_code not in {200, 202, 404}:
            raise PreparationError(
                TRANSIENT,
                f"the image registry refused to delete {digest} ({response.status_code})",
            )

    async def _fetch(self, ref: str, progress: Progress) -> bool:
        """Pull an image from the local registry by digest: False when the registry no
        longer has it, so it must be obtained again."""
        if await self._present(ref):
            return True
        await progress("pulling")
        code, _, err = await self._run("pull", "--quiet", ref, timeout=self.pull_timeout)
        if code == 0:
            return await self._present(ref)
        if _MISSING.search(err):
            return False
        raise PreparationError(
            classify(err), "pulling from the image registry failed", {"stderr_tail": err[-2000:]}
        )

    async def _push(self, temporary: str, key: str, progress: Progress) -> str:
        """Publish a produced image under its definition's tag; returns its digest and
        leaves the image present locally under ``ref(digest)``."""
        await progress("pushing")
        tag = self.tag(key)
        code, _, err = await self._run("tag", temporary, tag, timeout=60)
        await self._untag(temporary)
        if code:
            raise PreparationError(TRANSIENT, "tagging the image failed", {"stderr": err[-500:]})
        code, _, err = await self._run("push", "--quiet", tag, timeout=self.push_timeout)
        if code:
            raise PreparationError(
                classify(err), "pushing to the image registry failed", {"stderr_tail": err[-2000:]}
            )
        # Docker's own record of the pushed digest can name content the registry does not
        # hold (an index of which only one platform was pushed): ask the registry.
        digest = await self._registry_digest(key)
        if not await self._fetch(self.ref(digest), progress):
            raise PreparationError(TRANSIENT, "the pushed image is not in the image registry")
        return digest

    # --- producing an image ------------------------------------------------------------

    async def _build(self, spec: PreparationSpec, entry: dict, tag: str, progress: Progress):
        context = spec.task_dir / entry["context"]
        dockerfile = context / entry["dockerfile"]
        if not dockerfile.is_file():
            raise PreparationError(UNSUPPORTED, f"{entry['role']}: no {entry['dockerfile']}")
        await progress("building")
        timeout = build_timeout(spec.spec.get("build_timeout_sec"), self.build_timeout_max)
        # The setup network's proxy as Docker's predefined proxy build arguments: RUN steps
        # can download through it, and Docker keeps them out of the image's history and the
        # build cache. Its other variables are not passed (N-08).
        network = spec.setup_network
        proxy_args = [
            arg
            for k, v in sorted(network.proxy_env().items())
            for arg in ("--build-arg", f"{k}={v}")
        ]
        author_args = [
            arg for k, v in sorted(entry["args"].items()) for arg in ("--build-arg", f"{k}={v}")
        ]
        target = ["--target", entry["target"]] if entry.get("target") else []
        code, _, err = await self._run(
            "buildx",
            "build",
            "--provenance=false",
            *proxy_args,
            *author_args,
            *target,
            "--build-arg",
            "SOURCE_DATE_EPOCH=0",
            "--label",
            f"{LABEL_KEY}={image_key(entry).split(':', 1)[1]}",
            "--output",
            f"type=docker,rewrite-timestamp=true,name={tag}",
            "-f",
            str(dockerfile),
            str(context),
            timeout=timeout or self.build_timeout,
        )
        if code:
            raise PreparationError(
                classify(err), f"{entry['role']}: image build failed", {"stderr_tail": err[-2000:]}
            )
        return {
            "method": "build",
            "proxy": bool(network.proxy),
            "setup_network_revision": network.revision,
        }

    async def _pull(self, reference: str, tag: str, progress: Progress) -> dict:
        await progress("pulling")
        code, _, err = await self._run("pull", "--quiet", reference, timeout=self.pull_timeout)
        if code:
            # Docker reports "repository does not exist" and "login required" alike, so
            # a refused pull is AUTH; only a digest that is missing is "exactly gone".
            kind = classify(err)
            if kind == FAILED and _MISSING.search(err) and "@sha256:" in reference:
                kind = UNAVAILABLE
            raise PreparationError(
                kind, f"pull of {reference} failed", {"stderr_tail": err[-2000:]}
            )
        code, out, _ = await self._run(
            "image", "inspect", "--format", "{{json .RepoDigests}}", reference, timeout=60
        )
        digests = [d for d in re.findall(r'"([^"]+@sha256:[0-9a-f]{64})"', out)]
        pinned = reference if "@sha256:" in reference else (digests[0] if digests else None)
        code, _, err = await self._run("tag", reference, tag, timeout=60)
        if code:
            raise PreparationError(TRANSIENT, "tagging the pulled image failed")
        return {"method": "pull", "reference": reference, "repo_digest": pinned}

    async def _produce(
        self, spec: PreparationSpec, entry: dict, progress: Progress, pinned: str | None
    ) -> tuple[str, dict]:
        """Build or pull one image under a temporary tag and push it; returns (digest, detail)."""
        key = image_key(entry)
        temporary = f"{self.tag(key)}-tmp-{secrets.token_hex(4)}"
        try:
            if entry["source"] == "build":
                detail = await self._build(spec, entry, temporary, progress)
            elif entry["source"] == "pull":
                detail = await self._pull(pinned or entry["reference"], temporary, progress)
            else:
                raise PreparationError(UNSUPPORTED, f"{entry['role']}: unsupported source")
            if not await self._present(temporary):
                raise PreparationError(TRANSIENT, "the prepared image is not present")
            digest = await self._push(temporary, key, progress)
        except BaseException:
            with contextlib.suppress(Exception):
                await asyncio.shield(self._untag(temporary))
            raise
        return digest, detail

    # --- boundary -----------------------------------------------------------------------

    async def prepare(self, spec: PreparationSpec, progress: Progress) -> PreparedOutput:
        entries = spec.spec.get("images") or []
        if not entries:
            raise PreparationError(UNSUPPORTED, "the task has no supported environment")
        images = []
        for entry in entries:
            key = image_key(entry)
            async with self._lock(key):
                known = self._produced.get(key) or spec.known.get(key)
                if known is not None and await self._fetch(self.ref(known.digest), progress):
                    # The same definition was prepared before, for this task or another.
                    image = replace(known, role=entry["role"], ref=self.ref(known.digest))
                else:
                    digest, detail = await self._produce(spec, entry, progress, None)
                    image = PreparedImage(entry["role"], digest, self.ref(digest), key, detail)
                    self._produced[key] = image
            images.append(image)
        network = spec.setup_network
        return PreparedOutput(tuple(images), {"setup_network_revision": network.revision})

    async def check(self, expected: PreparedOutput) -> bool:
        for image in expected.images:
            if not await self._present(self.ref(image.digest)):
                return False
        return bool(expected.images)

    async def restore(
        self, spec: PreparationSpec, expected: PreparedOutput, progress: Progress
    ) -> PreparedOutput:
        await progress("checking")
        entries = {entry["role"]: entry for entry in spec.spec.get("images") or []}
        images = []
        for image in expected.images:
            ref = self.ref(image.digest)
            async with self._lock(image.key):
                if not await self._fetch(ref, progress):
                    # The registry lost it: obtain the fixed content again, but only an
                    # identical image may be published (02 section 5).
                    entry = entries.get(image.role)
                    if entry is None or image_key(entry) != image.key:
                        raise PreparationError(
                            UNAVAILABLE,
                            f"{image.role}: the image is gone and its definition changed",
                            {"expected_digest": image.digest},
                        )
                    digest, _ = await self._produce(
                        spec, entry, progress, (image.detail or {}).get("repo_digest")
                    )
                    if digest != image.digest:
                        raise PreparationError(
                            MISMATCH,
                            f"{image.role}: the rebuilt image differs from the prepared one",
                            {"expected_digest": image.digest, "actual_digest": digest},
                        )
            images.append(replace(image, ref=ref))
        return PreparedOutput(tuple(images), expected.detail)

    async def evict(self, expected: PreparedOutput, keep: frozenset[str] = frozenset()) -> None:
        for image in expected.images:
            if image.digest in keep:
                continue
            ref = self.ref(image.digest)
            names = [name for name in (ref, self.tag(image.key)) if await self._present(name)]
            if not names:
                continue
            code, _, err = await self._run("image", "rm", *names, timeout=120)
            if code and await self._present(ref):
                raise PreparationError(
                    TRANSIENT, "the image is still in use", {"stderr": err[-500:]}
                )

    async def discard(self, expected: PreparedOutput, keep: frozenset[str] = frozenset()) -> None:
        """Local images and registry manifests go; the registry's garbage collection frees
        the layers no remaining manifest uses (12)."""
        await self.evict(expected, keep)
        for image in expected.images:
            if image.digest not in keep:
                await self._delete_manifest(image.digest)

    async def sweep_temporary(self) -> list[str]:
        """Restart: drop temporary tags an interrupted preparation left behind."""
        if self.path is None:
            return []
        code, out, _ = await self._run(
            "image", "ls", self.repository, "--format", "{{.Repository}}:{{.Tag}}", timeout=60
        )
        removed = []
        for ref in out.split() if code == 0 else []:
            if "-tmp-" in ref.rsplit(":", 1)[-1]:
                await self._untag(ref)
                removed.append(ref)
        return removed

    def sweep_legacy(self) -> list[str]:
        """Remove what the archive-based preparation left (``docker save`` originals): the
        registry holds every image now, and older preparations are prepared again (U-53)."""
        if self.legacy_dir is None or not self.legacy_dir.is_dir():
            return []
        removed = sorted(path.name for path in self.legacy_dir.iterdir())
        shutil.rmtree(self.legacy_dir, ignore_errors=True)
        return removed
