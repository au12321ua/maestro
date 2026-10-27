"""Synchronous client for the Maestro ``/api/v1`` contract.

The models come from the OpenAPI document (``models.py``); this module adds what a
generator cannot: authentication, idempotent submission, paging, waiting and verified
downloads. It never runs anything locally and needs nothing from the service's code.
"""

from __future__ import annotations

import hashlib
import io
import os
import random
import tarfile
import tempfile
import time
import uuid
import warnings
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, RootModel, TypeAdapter, ValidationError

from maestro_sdk import models
from maestro_sdk._contract import CONTRACT_DIGEST
from maestro_sdk.errors import (
    ApiError,
    BlockersPresent,
    ContentMismatch,
    MaestroError,
    TransportError,
    UnsupportedCapability,
    WaitTimeout,
)

API_VERSION = "v1"
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
# Answers that mean "not handled, try again": safe to repeat for reads and keyed submissions.
RETRY_STATUSES = frozenset({502, 503, 504})
ARCHIVE_TYPES = {
    ".zip": "application/zip",
    ".tar": "application/x-tar",
    ".tar.gz": "application/gzip",
    ".tgz": "application/gzip",
}
T = TypeVar("T")


class ContractDriftWarning(UserWarning):
    """The service's OpenAPI document differs from the one this SDK was generated from."""


class Resolution(models.ConfigResolution):
    """A configuration resolution with the check the design calls for (10 section 7)."""

    def raise_for_blockers(self) -> Resolution:
        if self.blockers:
            raise BlockersPresent(self)
        return self


class Submission(BaseModel):
    """What a submission returned, with the key that makes repeating it safe."""

    accepted: models.RunAccepted
    idempotency_key: str
    replayed: bool

    @property
    def run_id(self) -> str:
        return self.accepted.run_id

    @property
    def operation_id(self) -> str:
        return self.accepted.operation_id


class BatchSubmission(BaseModel):
    accepted: models.BatchAccepted
    idempotency_key: str
    replayed: bool

    @property
    def batch_id(self) -> str:
        return self.accepted.batch_id

    @property
    def operation_id(self) -> str:
        return self.accepted.operation_id


class Download(BaseModel):
    """One artifact of ``runs.download_artifacts``: ``path`` is set when it was saved."""

    artifact: models.Artifact
    path: str | None
    skipped_reason: str | None = None


def _body(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_unset=True)
    return value


def _query(params: Mapping[str, Any]) -> dict[str, Any]:
    return {
        k: (str(v).lower() if isinstance(v, bool) else v)
        for k, v in params.items()
        if v is not None
    }


def _base_url(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError("base_url must be http(s)://host[:port][/path] without credentials")
    return value.rstrip("/")


class MaestroClient:
    """Client for one Maestro service.

    ``token`` is a personal access token (PAT). By default the client connects directly
    and ignores proxy variables (``trust_env=False``), so a PAT never travels through a
    proxy the caller did not choose; pass ``trust_env=True`` to use them.
    """

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        timeout: float = 30.0,
        retries: int = 3,
        trust_env: bool = False,
        http: httpx.Client | None = None,
    ):
        self.base_url = _base_url(base_url)
        self.retries = retries
        self._token = token
        self._http = http or httpx.Client(
            base_url=self.base_url, timeout=timeout, trust_env=trust_env, follow_redirects=False
        )
        self._owns_http = http is None
        self._warned = False
        self.catalog = Catalog(self)
        self.configs = Configs(self)
        self.attachments = Attachments(self)
        self.plans = Plans(self)
        self.runs = Runs(self)
        self.batches = Batches(self)
        self.operations = Operations(self)
        self.artifacts = Artifacts(self)
        self.exports = Exports(self)

    @classmethod
    def from_env(cls, **kwargs: Any) -> MaestroClient:
        """``MAESTRO_URL`` and ``MAESTRO_TOKEN`` (or ``MAESTRO_TOKEN_FILE``)."""
        token = os.environ.get("MAESTRO_TOKEN")
        if not token and os.environ.get("MAESTRO_TOKEN_FILE"):
            token = Path(os.environ["MAESTRO_TOKEN_FILE"]).read_text().strip()
        return cls(os.environ["MAESTRO_URL"], token, **kwargs)

    def __repr__(self) -> str:
        return f"MaestroClient({self.base_url!r})"

    def __enter__(self) -> MaestroClient:  # noqa: PYI034 (typing.Self needs 3.11)
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    # --- service ---------------------------------------------------------------------

    def capabilities(self) -> models.Capabilities:
        """Versions, features, limits and which channel may do what (API-01, API-06)."""
        caps = self._get("capabilities", models.Capabilities)
        if caps.contract_digest != CONTRACT_DIGEST and not self._warned:
            self._warned = True
            warnings.warn(
                "The service's API contract differs from this SDK's; "
                "install the SDK that matches the service",
                ContractDriftWarning,
                stacklevel=2,
            )
        return caps

    def require(self, *features: str) -> models.Capabilities:
        """Fail early with UNSUPPORTED_CAPABILITY instead of guessing at fields."""
        caps = self.capabilities()
        if caps.api_version != API_VERSION:
            raise UnsupportedCapability(
                f"Service speaks API {caps.api_version}; this SDK speaks {API_VERSION}"
            )
        missing = [f for f in features if not caps.features.get(f)]
        if missing:
            raise UnsupportedCapability(f"Service does not offer: {', '.join(missing)}")
        return caps

    def me(self) -> models.Me:
        return self._get("me", models.Me)

    def system_status(self) -> models.Health:
        """Readiness, whether runs are accepted now, storage and the worker."""
        return self._get("system-status", models.Health)

    # --- transport -------------------------------------------------------------------

    def _get(self, path: str, model: Any, **params: Any) -> Any:
        return self._parse(model, self._call("GET", path, params=params).json()["data"])

    def _page(self, path: str, model: Any, **params: Any) -> tuple[list[Any], str | None]:
        payload = self._call("GET", path, params=params).json()
        return self._parse(list[model], payload["data"]), payload["meta"].get("next_cursor")

    def _iterate(self, path: str, model: Any, **params: Any) -> Iterator[Any]:
        cursor = None
        while True:
            items, cursor = self._page(path, model, **{**params, "cursor": cursor})
            yield from items
            if not cursor:
                return

    def _send(self, method: str, path: str, model: Any, body: Any = None, **kw: Any) -> Any:
        response = self._call(method, path, json=_body(body), **kw)
        return self._parse(model, response.json()["data"])

    @staticmethod
    def _parse(model: Any, data: Any) -> Any:
        try:
            parsed = TypeAdapter(model).validate_python(data)
        except ValidationError as error:
            raise MaestroError(
                f"Unexpected response shape ({error.error_count()} problems); the SDK may not "
                "match the service version"
            ) from error
        # A union (an Operation of some kind) is a root model: hand out the member itself.
        if isinstance(parsed, RootModel):
            return parsed.root
        if isinstance(parsed, list):
            return [x.root if isinstance(x, RootModel) else x for x in parsed]
        return parsed

    def _call(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        content: Any = None,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
        stream: bool = False,
    ) -> httpx.Response:
        url = path if path.startswith("/") else f"/api/v1/{path}"
        sent = {"Accept": "application/json", **(headers or {})}
        if self._token:
            sent["Authorization"] = f"Bearer {self._token}"
        if idempotency_key:
            sent["Idempotency-Key"] = idempotency_key
        # Repeating is safe for reads and for submissions under the same key; anything else
        # (a plain POST) is sent once and an unknown outcome is reported, not guessed.
        repeatable = method == "GET" or idempotency_key is not None
        attempts = self.retries + 1 if repeatable else 1
        for attempt in range(attempts):
            try:
                request = self._http.build_request(
                    method,
                    url,
                    params=_query(params or {}),
                    json=json,
                    content=content,
                    headers=sent,
                )
                response = self._http.send(request, stream=stream)
            except httpx.HTTPError as error:
                if attempt + 1 < attempts:
                    time.sleep(_backoff(attempt))
                    continue
                raise TransportError(
                    f"No answer from {self.base_url} ({type(error).__name__}); the outcome "
                    "is unknown",
                    idempotency_key=idempotency_key,
                ) from None
            if response.status_code in RETRY_STATUSES and attempt + 1 < attempts:
                if stream:
                    response.close()
                time.sleep(_retry_after(response) or _backoff(attempt))
                continue
            if response.status_code >= 400:
                if stream:
                    response.read()
                raise _api_error(response)
            return response
        raise AssertionError("unreachable")


def _backoff(attempt: int) -> float:
    return min(5.0, 0.5 * 2**attempt) * (0.5 + random.random())


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After", "")
    return min(float(value), 60.0) if value.isdigit() else None


def _api_error(response: httpx.Response) -> ApiError:
    request_id = response.headers.get("X-Request-Id")
    try:
        payload = response.json()
        body = payload["error"]
        request_id = payload.get("meta", {}).get("request_id", request_id)
    except (ValueError, KeyError, TypeError):
        body = {"code": "HTTP_ERROR", "message": response.reason_phrase or "Request failed"}
    return ApiError(response.status_code, body, request_id)


class _Resource:
    def __init__(self, client: MaestroClient):
        self._c = client


class Catalog(_Resource):
    """Datasets added by administrators: versions, tasks, admission, content and cache."""

    def sources(self) -> list[models.DatasetSource]:
        return self._c._get("dataset-sources", list[models.DatasetSource])

    def datasets(self, *, limit: int | None = None) -> Iterator[models.Dataset]:
        return self._c._iterate("datasets", models.Dataset, limit=limit)

    def version(self, version_id: str) -> models.DatasetVersionDetail:
        return self._c._get(f"dataset-versions/{version_id}", models.DatasetVersionDetail)

    def tasks(self, version_id: str) -> list[models.Task]:
        return self._c._get(f"dataset-versions/{version_id}/tasks", list[models.Task])

    def preparation(self, preparation_id: str) -> models.Preparation:
        return self._c._get(f"preparations/{preparation_id}", models.Preparation)


class Configs(_Resource):
    """Models, harnesses, credential names, team policy, setup network, and configuration
    resolution."""

    def models(self, *, include_disabled: bool = False) -> list[models.ModelConfig]:
        return self._c._get(
            "model-configs", list[models.ModelConfig], include_disabled=include_disabled
        )

    def model(self, config_id: str) -> models.ModelConfig:
        return self._c._get(f"model-configs/{config_id}", models.ModelConfig)

    def harnesses(self) -> list[models.Harness]:
        return self._c._get("harness-versions", list[models.Harness])

    def harness(self, harness_id: str) -> models.Harness:
        return self._c._get(f"harness-versions/{harness_id}", models.Harness)

    def credential_names(self) -> list[models.CredentialName]:
        """Names only, for choosing a credential for a secret author parameter."""
        return self._c._get("credentials", list[models.CredentialName])

    def team_policy(self) -> models.TeamPolicy:
        return self._c._get("team-policy", models.TeamPolicy)

    def setup_network(self) -> models.SetupNetwork:
        """How setup work reaches the network on this deployment: proxy, package mirrors
        and the harness setup time limit in force."""
        return self._c._get("setup-network", models.SetupNetwork)

    def setup_network_revisions(self) -> list[models.SetupNetwork]:
        """Every revision, newest first; an attempt's ``setup_network_revision`` names one."""
        return self._c._get("setup-network/revisions", list[models.SetupNetwork])

    def resolve(self, config: models.ConfigRequestInput | Mapping[str, Any]) -> Resolution:
        """Effective values, their sources, blockers and ``config_digest``; reserves nothing."""
        return self._c._send("POST", "config-resolutions", Resolution, config)

    def resolution(self, resolution_id: str) -> Resolution:
        return self._c._get(f"config-resolutions/{resolution_id}", Resolution)


class Attachments(_Resource):
    def list(
        self, *, name: str | None = None, include_disabled: bool = False
    ) -> list[models.Attachment]:
        return self._c._get(
            "attachments", list[models.Attachment], name=name, include_disabled=include_disabled
        )

    def get(self, attachment_id: str) -> models.Attachment:
        return self._c._get(f"attachments/{attachment_id}", models.Attachment)

    def publish(self, package: str | os.PathLike[str]) -> models.Attachment:
        """Publish a directory (packed here as tar.gz) or a .zip / .tar / .tar.gz file.

        Publishing the same name, version and content again returns the existing version.
        Nothing in the package runs on upload.
        """
        path = Path(package)
        if path.is_dir():
            data, kind = _pack(path), "application/gzip"
        else:
            suffix = ".tar.gz" if path.name.endswith(".tar.gz") else path.suffix
            if suffix not in ARCHIVE_TYPES:
                raise ValueError(f"Not a package archive: {path.name}")
            data, kind = path.read_bytes(), ARCHIVE_TYPES[suffix]
        response = self._c._call(
            "POST", "attachments", content=data, headers={"Content-Type": kind}
        )
        return self._c._parse(models.Attachment, response.json()["data"])

    def set_enabled(self, attachment_id: str, enabled: bool) -> models.Attachment:
        return self._c._send(
            "POST", f"attachments/{attachment_id}/enabled", models.Attachment, {"enabled": enabled}
        )

    def delete(self, attachment_id: str) -> models.RecycleEntry:
        return self._c._send("DELETE", f"attachments/{attachment_id}", models.RecycleEntry)


def _pack(directory: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"Packages cannot hold links: {path}")
            archive.add(path, arcname=str(path.relative_to(directory)), recursive=False)
    return buffer.getvalue()


class Plans(_Resource):
    """Saved partial configurations; a run fixes the revision it used."""

    def list(self, *, include_archived: bool = False) -> list[models.Plan]:
        return self._c._get("plans", list[models.Plan], include_archived=include_archived)

    def get(self, plan_id: str) -> models.Plan:
        return self._c._get(f"plans/{plan_id}", models.Plan)

    def create(
        self, name: str, template: Mapping[str, Any], *, description: str = ""
    ) -> models.Plan:
        body = {"name": name, "template": _body(template), "description": description}
        return self._c._send("POST", "plans", models.Plan, body)

    def update(self, plan_id: str, expected_revision: int, **changes: Any) -> models.Plan:
        """``name``, ``description`` and/or ``template``; only the creator may change it."""
        body = {"expected_revision": expected_revision, **_query(changes)}
        return self._c._send("PUT", f"plans/{plan_id}", models.Plan, body)

    def copy(self, plan_id: str, *, name: str | None = None) -> models.Plan:
        return self._c._send("POST", f"plans/{plan_id}/copy", models.Plan, {"name": name})

    def archive(self, plan_id: str, archived: bool = True) -> models.Plan:
        return self._c._send(
            "POST", f"plans/{plan_id}/archive", models.Plan, {"archived": archived}
        )

    def delete(self, plan_id: str) -> models.RecycleEntry:
        return self._c._send("DELETE", f"plans/{plan_id}", models.RecycleEntry)


def _key(value: str | None) -> str:
    return value or f"sdk-{uuid.uuid4()}"


class Runs(_Resource):
    def submit(
        self,
        config: models.ConfigRequestInput | Mapping[str, Any] | None = None,
        *,
        name: str,
        resolution_id: str | None = None,
        client_request_id: str | None = None,
        source_run_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Submission:
        """Submit a resolved configuration (``resolution_id``) or one given inline.

        One logical submission is one ``idempotency_key`` (generated when omitted, and kept
        on the result and on TransportError): repeating it returns the same run. Transient
        failures are retried here under that key; the run is never submitted twice.
        """
        body: dict[str, Any] = dict(_body(config) or {})
        body.update(
            _query(
                {
                    "name": name,
                    "resolution_id": resolution_id,
                    "client_request_id": client_request_id,
                    "source_run_id": source_run_id,
                }
            )
        )
        key = _key(idempotency_key)
        response = self._c._call("POST", "runs", json=body, idempotency_key=key)
        return Submission(
            accepted=self._c._parse(models.RunAccepted, response.json()["data"]),
            idempotency_key=key,
            replayed=response.headers.get("Idempotent-Replayed") == "true",
        )

    def rerun(
        self,
        run_id: str,
        *,
        mode: str = "all",
        task_ids: list[str] | None = None,
        name: str | None = None,
        client_request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Submission:
        """A new run from the source's snapshot: ``all``, ``unfinished`` or ``explicit``."""
        body = {
            "task_selection": _query({"mode": mode, "task_ids": task_ids}),
            **_query({"name": name, "client_request_id": client_request_id}),
        }
        key = _key(idempotency_key)
        response = self._c._call("POST", f"runs/{run_id}/rerun", json=body, idempotency_key=key)
        return Submission(
            accepted=self._c._parse(models.RunAccepted, response.json()["data"]),
            idempotency_key=key,
            replayed=response.headers.get("Idempotent-Replayed") == "true",
        )

    def request(self, run_id: str) -> models.ConfigRequestOutput:
        """The request that reproduces a run; edit it and submit with ``source_run_id``."""
        return self._c._get(f"runs/{run_id}/request", models.ConfigRequestOutput)

    def get(self, run_id: str) -> models.RunDetail:
        return self._c._get(f"runs/{run_id}", models.RunDetail)

    def list(self, *, limit: int | None = None, **filters: Any) -> Iterator[models.Run]:
        """Newest first, all pages. Filters: owner_id, state, dataset_version_id,
        model_config_id, harness_version_id, attachment_version_id, plan_id,
        client_request_id, accepted_after, accepted_before (RFC 3339 UTC)."""
        return self._c._iterate("runs", models.Run, limit=limit, **filters)

    def find(self, client_request_id: str) -> list[models.Run]:
        """Runs carrying this ``client_request_id``: how to recover after a lost key."""
        return list(self.list(client_request_id=client_request_id))

    def trials(self, run_id: str) -> list[models.Trial]:
        return self._c._get(f"runs/{run_id}/trials", list[models.Trial])

    def summary(self, run_id: str) -> models.RunSummary:
        return self._c._get(f"runs/{run_id}/summary", models.RunSummary)

    def cancel(self, run_id: str) -> models.Run:
        return self._c._send("POST", f"runs/{run_id}/cancel", models.Run)

    def delete(self, run_id: str) -> models.RecycleEntry:
        """Into the recycle bin; queued or running runs must be cancelled first."""
        return self._c._send("DELETE", f"runs/{run_id}", models.RecycleEntry)

    def artifacts(self, run_id: str, *, trial_id: str | None = None) -> list[models.Artifact]:
        return self._c._get(f"runs/{run_id}/artifacts", list[models.Artifact], trial_id=trial_id)

    def download_artifacts(
        self, run_id: str, destination: str | os.PathLike[str], *, trial_id: str | None = None
    ) -> list[Download]:
        """Save every available artifact under ``destination``; report the others.

        Run files go to ``run/<path>``, trial files to ``trials/<trial>/<attempt>/<path>``.
        Expired, failed or deleted files are listed with their status and reason.
        """
        root = Path(destination)
        results = []
        for artifact in self.artifacts(run_id, trial_id=trial_id):
            if artifact.status != "available":
                reason = artifact.status_reason or artifact.status
                results.append(Download(artifact=artifact, path=None, skipped_reason=reason))
                continue
            if artifact.trial_id is None:
                target = root / "run"
            else:
                target = root / "trials" / artifact.trial_id / (artifact.attempt_id or "trial")
            target = _inside(target, artifact.relative_path)
            self._c.artifacts.download(artifact, target)
            results.append(Download(artifact=artifact, path=str(target)))
        return results


def _inside(base: Path, relative: str) -> Path:
    parts = PurePosixPath(relative).parts
    if not parts or PurePosixPath(relative).is_absolute() or ".." in parts:
        raise MaestroError(f"Refusing artifact path {relative!r}")
    return base.joinpath(*parts)


class Batches(_Resource):
    """Runs accepted together, all or none; each run costs one unit of PAT quota."""

    def preview(self, name: str, runs: list[Mapping[str, Any]]) -> models.BatchPreview:
        body = {"name": name, "runs": [_body(r) for r in runs]}
        return self._c._send("POST", "run-batches/preview", models.BatchPreview, body)

    def submit(
        self,
        name: str,
        runs: list[Mapping[str, Any]],
        *,
        client_request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> BatchSubmission:
        body = {
            "name": name,
            "runs": [_body(r) for r in runs],
            **_query({"client_request_id": client_request_id}),
        }
        key = _key(idempotency_key)
        response = self._c._call("POST", "run-batches", json=body, idempotency_key=key)
        return BatchSubmission(
            accepted=self._c._parse(models.BatchAccepted, response.json()["data"]),
            idempotency_key=key,
            replayed=response.headers.get("Idempotent-Replayed") == "true",
        )

    def get(self, batch_id: str) -> models.Batch:
        return self._c._get(f"run-batches/{batch_id}", models.Batch)

    def list(
        self, *, owner_id: str | None = None, limit: int | None = None
    ) -> Iterator[models.Batch]:
        return self._c._iterate("run-batches", models.Batch, owner_id=owner_id, limit=limit)

    def cancel(self, batch_id: str) -> models.Batch:
        return self._c._send("POST", f"run-batches/{batch_id}/cancel", models.Batch)


class Operations(_Resource):
    def get(self, operation_id: str) -> models.Operation:
        return self._c._get(f"operations/{operation_id}", models.Operation)

    def list(
        self, *, kind: str | None = None, owner_id: str | None = None, limit: int | None = None
    ) -> Iterator[models.Operation]:
        return self._c._iterate(
            "operations", models.Operation, kind=kind, owner_id=owner_id, limit=limit
        )

    def wait(
        self,
        operation_id: str,
        *,
        timeout: float = 3600.0,
        interval: float = 1.0,
        max_interval: float = 5.0,
    ) -> models.Operation:
        """Poll until the Operation ends (succeeded, failed, cancelled or interrupted).

        Polling starts at ``interval`` and slows to ``max_interval`` with jitter. On timeout
        WaitTimeout is raised and the work on the service continues; "no progress yet" is
        never a failure.
        """
        deadline = time.monotonic() + timeout
        delay = interval
        while True:
            operation = self.get(operation_id)
            if operation.status in TERMINAL:
                return operation
            left = deadline - time.monotonic()
            if left <= 0:
                raise WaitTimeout(operation, timeout)
            time.sleep(min(left, delay * (0.8 + 0.4 * random.random())))
            delay = min(max_interval, delay * 1.5)


class Artifacts(_Resource):
    def read(self, artifact_id: str, *, offset: int = 0, length: int | None = None) -> bytes:
        """A slice (at most 1 MiB with ``length``), e.g. for following a log."""
        response = self._c._call(
            "GET",
            f"artifacts/{artifact_id}/download",
            params={"offset": offset, "length": length},
            headers={"Accept": "application/octet-stream"},
        )
        return response.content

    def download(
        self, artifact: models.Artifact | str, destination: str | os.PathLike[str]
    ) -> Path:
        """Save one file to ``destination`` (a file path) and check its digest."""
        artifact_id = artifact if isinstance(artifact, str) else artifact.id
        return _save(self._c, f"artifacts/{artifact_id}/download", Path(destination))

    def delete(self, artifact_id: str) -> models.RecycleEntry:
        """Administrators only (for example a confirmed leak)."""
        return self._c._send("DELETE", f"artifacts/{artifact_id}", models.RecycleEntry)


class Exports(_Resource):
    def create(
        self,
        run_ids: list[str],
        *,
        include_artifacts: bool = True,
        artifact_kinds: list[str] | None = None,
        task_keys: list[str] | None = None,
    ) -> models.Export:
        """An asynchronous package; wait on its ``operation_id`` and then download it."""
        body = {
            "run_ids": run_ids,
            "include_artifacts": include_artifacts,
            **_query({"artifact_kinds": artifact_kinds, "task_keys": task_keys}),
        }
        return self._c._send("POST", "exports", models.Export, body)

    def get(self, export_id: str) -> models.Export:
        return self._c._get(f"exports/{export_id}", models.Export)

    def list(self, *, limit: int | None = None) -> Iterator[models.Export]:
        return self._c._iterate("exports", models.Export, limit=limit)

    def download(self, export_id: str, destination: str | os.PathLike[str]) -> Path:
        """Save the zip package and check its digest."""
        return _save(self._c, f"exports/{export_id}/download", Path(destination))


def _save(client: MaestroClient, path: str, target: Path) -> Path:
    """Stream to a temporary file next to ``target``; keep it only if the digest matches."""
    target.parent.mkdir(parents=True, exist_ok=True)
    response = client._call(
        "GET", path, headers={"Accept": "application/octet-stream"}, stream=True
    )
    expected = response.headers.get("X-Content-Digest") or None
    hasher = hashlib.sha256()
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".download-")
    try:
        with os.fdopen(handle, "wb") as out:
            for chunk in response.iter_bytes():
                hasher.update(chunk)
                out.write(chunk)
        actual = "sha256:" + hasher.hexdigest()
        if expected and expected != actual:
            raise ContentMismatch(f"{target.name}: expected {expected}, received {actual}")
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    finally:
        response.close()
    return target
