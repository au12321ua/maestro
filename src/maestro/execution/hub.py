"""Harbor Hub dataset source through the Runner environment.

The service never imports Harbor: each catalog call starts the Runner with one
``hub`` command and reads a single ``result`` event. The team Hub key, when the
dataset is bound to one, reaches only that subprocess's environment
(``HARBOR_API_KEY``); its HOME is a fresh directory, so no stored login is used.
"""

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import tempfile
from pathlib import Path

from maestro.boundaries.dataset_source import (
    TRANSIENT,
    UNSUPPORTED,
    FetchedTask,
    SourceError,
    SourceTask,
    SourceVersion,
)
from maestro.execution import process
from maestro.execution.harbor import ENV_KEEP, PROTOCOL_VERSION
from maestro.security.redaction import redact, redact_json

log = logging.getLogger(__name__)
KINDS = {"auth", "transient", "not_found", "mismatch", "unsupported"}
# Default for the one-shot Runner processes alive at once (``hub_runner_slots`` in
# config.toml); the rest wait their turn.
MAX_RUNNERS = 4


class HubSource:
    kind = "hub"

    def __init__(
        self,
        runner_python: Path,
        work_root: Path,
        *,
        timeout: float = 600.0,
        max_runners: int = MAX_RUNNERS,
    ):
        self.runner_python = runner_python
        self.work_root = work_root
        self.timeout = timeout
        self.max_runners = max_runners
        self._slots: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None

    def _runner_slots(self) -> asyncio.Semaphore:
        """Bound to the running loop, like the preparation restore slots."""
        loop = asyncio.get_running_loop()
        if self._slots is None or self._slots[0] is not loop:
            self._slots = (loop, asyncio.Semaphore(self.max_runners))
        return self._slots[1]

    async def _call(self, op: str, payload: dict, credential: str | None, timeout=None):
        async with self._runner_slots():
            return await self._run(op, payload, credential, timeout)

    async def _run(self, op: str, payload: dict, credential: str | None, timeout=None):
        self.work_root.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix="hub-", dir=self.work_root))
        env = {k: v for k, v in os.environ.items() if k in ENV_KEEP}
        env.setdefault("PATH", os.defpath)
        (workdir / "home").mkdir()
        env.update(HOME=str(workdir / "home"), TMPDIR=str(workdir), PYTHONDONTWRITEBYTECODE="1")
        if credential:
            env["HARBOR_API_KEY"] = credential
        request = {"protocol_version": PROTOCOL_VERSION, "command": "hub", "op": op, **payload}
        try:
            proc = await asyncio.create_subprocess_exec(
                str(self.runner_python),
                "-I",
                "-m",
                "maestro_runner",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
                env=env,
                start_new_session=True,
            )
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(json.dumps(request).encode()), timeout or self.timeout
                )
            except (TimeoutError, asyncio.CancelledError) as error:
                process.signal_group(proc.pid, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), 10)
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise SourceError(TRANSIENT, f"Hub {op} timed out") from None
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        secrets = [credential] if credential else []
        result = None
        for line in out.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "result" and event.get("protocol_version") == PROTOCOL_VERSION:
                result = event.get("payload") or {}
        if result is None:
            log.warning(
                "hub %s: no result (exit %s): %s",
                op,
                proc.returncode,
                redact(err[-2000:].decode(errors="replace"), secrets),
            )
            raise SourceError(TRANSIENT, f"Hub {op} ended without an answer")
        if result.get("status") == "ok":
            return result.get("data")
        kind = result.get("kind") if result.get("kind") in KINDS else TRANSIENT
        raise SourceError(
            kind,
            redact(str(result.get("message") or ""), secrets),
            redact_json(result.get("evidence"), secrets),
        )

    async def search(self, query: str, credential: str | None) -> list[dict]:
        return await self._call("search", {"query": query}, credential, timeout=60)

    async def list_versions(self, namespace: str, name: str, credential: str | None) -> list:
        return await self._call(
            "versions", {"namespace": namespace, "name": name}, credential, timeout=60
        )

    async def describe(self, namespace: str, name: str, ref: str, credential: str | None):
        data = await self._call(
            "describe", {"namespace": namespace, "name": name, "ref": ref}, credential, timeout=120
        )
        try:
            return SourceVersion(
                namespace=data["namespace"],
                name=data["name"],
                upstream_ref=data["upstream_ref"],
                ref_kind=data["ref_kind"],
                resolved_digest=data["resolved_digest"],
                description=data.get("description") or "",
                detail=data.get("detail") or {},
                tasks=tuple(
                    SourceTask(t["task_key"], t["content_digest"], t["locator"])
                    for t in data["tasks"]
                ),
            )
        except (KeyError, TypeError) as error:
            raise SourceError(UNSUPPORTED, f"unexpected Hub answer: {error}") from None

    async def fetch(self, task: SourceTask, destination: Path, credential: str | None):
        data = await self._call(
            "fetch",
            {
                "locator": task.locator,
                "content_digest": task.content_digest,
                "destination": str(destination),
            },
            credential,
        )
        if data.get("content_digest") != task.content_digest or not destination.is_dir():
            raise SourceError(TRANSIENT, "Hub fetch did not produce the requested task")
        return FetchedTask(destination, task.content_digest)
