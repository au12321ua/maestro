"""Synchronous client for the Maestro ``/api/v1`` contract.

The models come from the OpenAPI document (``models.py``); this module adds what a
generator cannot: authentication, idempotent submission, paging, waiting and verified
downloads. It never runs anything locally and needs nothing from the service's code.
"""

from __future__ import annotations

import hashlib
import os
import random
import shutil
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
        self.comparisons = Comparisons(self)
        self.collections = Collections(self)
        self.snapshots = Snapshots(self)
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
        retry: bool = False,
    ) -> httpx.Response:
        url = path if path.startswith("/") else f"/api/v1/{path}"
        sent = {"Accept": "application/json", **(headers or {})}
        if self._token:
            sent["Authorization"] = f"Bearer {self._token}"
        if idempotency_key:
            sent["Idempotency-Key"] = idempotency_key
        # Repeating is safe for reads and for submissions under the same key; anything else
        # (a plain POST) is sent once and an unknown outcome is reported, not guessed.
        # ``retry`` marks the other requests that are safe to repeat by their nature.
        repeatable = method == "GET" or idempotency_key is not None or retry
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

    def publish(
        self,
        directory: str | os.PathLike[str],
        *,
        label: str | None = None,
        parent: models.Attachment | str | None = None,
        exclude: tuple[str, ...] = (".git",),
    ) -> models.Attachment:
        """Publish a directory as a snapshot, uploading only the files the service lacks.

        The same content published again returns the existing snapshot (its label stays
        as first given). ``label`` names the snapshot within its attachment and never
        moves; ``parent`` records the snapshot this one was made from. Directories named
        in ``exclude`` are left out. Nothing in the directory runs on upload.
        """
        root = Path(directory)
        if not root.is_dir():
            raise ValueError(f"Not a directory: {root}")
        files = _describe(root, exclude)
        by_sha = {f["sha256"]: f["source"] for f in files}
        missing = self._c._call(
            "POST", "attachment-contents/missing", json={"sha256": sorted(by_sha)}, retry=True
        ).json()["data"]["missing"]
        for sha in missing:
            with by_sha[sha].open("rb") as content:
                self._c._call(
                    "PUT",
                    f"attachment-contents/{sha}",
                    content=content.read(),
                    headers={"Content-Type": "application/octet-stream"},
                    retry=True,
                )
        parent_id = parent if isinstance(parent, str) or parent is None else parent.id
        body = {
            "files": [{k: f[k] for k in ("path", "sha256", "executable")} for f in files],
            "label": label,
            "parent_id": parent_id,
        }
        # Publishing the same content twice returns the same snapshot: safe to repeat.
        response = self._c._call("POST", "attachments", json=body, retry=True)
        return self._c._parse(models.Attachment, response.json()["data"])

    def fetch(
        self,
        attachment: models.Attachment | str,
        directory: str | os.PathLike[str],
        *,
        prune: bool = False,
    ) -> Path:
        """Make ``directory`` hold the snapshot's files, downloading only those missing
        or different here. With ``prune``, extra files and file/directory conflicts are
        removed too. Existing links in a destination path are refused."""
        attachment_id = attachment if isinstance(attachment, str) else attachment.id
        snapshot = self.get(attachment_id)
        root = Path(directory)
        for parent in (root.absolute(), *root.absolute().parents):
            if parent.is_symlink():
                raise ValueError(f"Fetch destination cannot contain a link: {parent}")
        root.mkdir(parents=True, exist_ok=True)
        wanted = {f.path for f in snapshot.files or []}
        conflicts: set[Path] = set()
        # Check every destination before modifying anything. The server only publishes
        # relative paths, but an existing local parent can still point outside root.
        for entry in snapshot.files or []:
            parts = PurePosixPath(entry.path).parts
            if not parts or entry.path.startswith("/") or any(p in {".", ".."} for p in parts):
                raise ValueError(f"Unsafe attachment path: {entry.path}")
            target = root
            for index, part in enumerate(parts):
                target = target / part
                if target.is_symlink():
                    raise ValueError(f"Fetch destination cannot contain a link: {target}")
                if target.exists() and target.is_dir() != (index < len(parts) - 1):
                    conflicts.add(target)
                    break
        if conflicts and not prune:
            raise ValueError("File/directory conflicts in fetch destination; use prune=True")
        for target in sorted(conflicts, key=lambda p: len(p.parts)):
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
        for entry in snapshot.files or []:
            target = root.joinpath(*PurePosixPath(entry.path).parts)
            # Replacing a hard-linked file also keeps chmod local to the destination.
            if not (
                target.is_file() and target.stat().st_nlink == 1 and _sha256(target) == entry.sha256
            ):
                _save(self._c, f"attachments/{attachment_id}/files/{entry.path}", target)
            target.chmod(0o755 if entry.executable else 0o644)
        if prune:
            for path in sorted(root.rglob("*"), reverse=True):
                relative = path.relative_to(root).as_posix()
                if path.is_dir() and not path.is_symlink():
                    if not any(path.iterdir()):
                        path.rmdir()
                elif relative not in wanted:
                    path.unlink()
        return root

    def download(
        self, attachment: models.Attachment | str, destination: str | os.PathLike[str]
    ) -> Path:
        """Save the whole snapshot as a zip file (paths and executable bits kept)."""
        attachment_id = attachment if isinstance(attachment, str) else attachment.id
        return _save(self._c, f"attachments/{attachment_id}/archive", Path(destination))

    def diff(
        self, attachment: models.Attachment | str, base: models.Attachment | str | None = None
    ) -> models.AttachmentDiff:
        """Files added, removed and changed since ``base`` (default: the parent)."""
        attachment_id = attachment if isinstance(attachment, str) else attachment.id
        base_id = base if isinstance(base, str) or base is None else base.id
        return self._c._get(
            f"attachments/{attachment_id}/diff", models.AttachmentDiff, base_id=base_id
        )

    def set_enabled(self, attachment_id: str, enabled: bool) -> models.Attachment:
        return self._c._send(
            "POST", f"attachments/{attachment_id}/enabled", models.Attachment, {"enabled": enabled}
        )

    def delete(self, attachment_id: str) -> models.RecycleEntry:
        return self._c._send("DELETE", f"attachments/{attachment_id}", models.RecycleEntry)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _describe(root: Path, exclude: tuple[str, ...]) -> list[dict]:
    """Every regular file below ``root`` in path order; links are refused."""
    files = []
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if any(part in exclude for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"Attachments cannot hold links: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"Not a regular file: {path}")
        files.append(
            {
                "path": relative.as_posix(),
                "sha256": _sha256(path),
                "executable": bool(path.stat().st_mode & 0o100),
                "source": path,
            }
        )
    return files


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


class Comparisons(_Resource):
    """Runs side by side, computed on demand and never saved."""

    def compare(
        self,
        run_ids: list[str],
        *,
        exclude: list[str] | None = None,
        scope: str | None = None,
        baseline_run_id: str | None = None,
    ) -> models.Comparison:
        """``exclude``: failure groups to leave out (default: the external ones); failures
        of the other groups count as failed. ``scope``: ``common`` or ``all`` tasks."""
        body = {
            "run_ids": run_ids,
            **_query({"exclude": exclude, "scope": scope, "baseline_run_id": baseline_run_id}),
        }
        return self._c._send("POST", "comparisons", models.Comparison, body)


class Collections(_Resource):
    """Run lists the team keeps up. The specification is fixed at creation: which
    dimensions may vary, and the values of the others; only matching runs get in."""

    def list(self) -> list[models.Collection]:
        return self._c._get("collections", list[models.Collection])

    def get(self, collection_id: str) -> models.Collection:
        return self._c._get(f"collections/{collection_id}", models.Collection)

    def create(
        self,
        name: str,
        scenario: str,
        *,
        base_run_id: str | None = None,
        base_request: Mapping[str, Any] | None = None,
        variable_dimensions: list[str] | None = None,
        run_ids: list[str] | None = None,
        description: str = "",
        settings: Mapping[str, Any] | None = None,
    ) -> models.Collection:
        """Fixed values come from ``base_run_id`` or a full ``base_request``. Scenarios:
        models, harnesses, model_harness, attachments, repeats, custom (with
        ``variable_dimensions``)."""
        base = {"run_id": base_run_id} if base_run_id else {"request": _body(base_request)}
        body = {
            "name": name,
            "scenario": scenario,
            "base": base,
            "description": description,
            "run_ids": run_ids or [],
            **_query({"variable_dimensions": variable_dimensions, "settings": settings}),
        }
        return self._c._send("POST", "collections", models.Collection, body)

    def update(self, collection_id: str, **changes: Any) -> models.Collection:
        """``name``, ``description`` and/or ``settings``."""
        return self._c._send(
            "PUT", f"collections/{collection_id}", models.Collection, _query(changes)
        )

    def copy(self, collection_id: str, **changes: Any) -> models.Collection:
        """A new collection: ``name``, ``scenario``, ``variable_dimensions``, ``base``
        (``{"run_id": ...}`` or ``{"request": ...}``); ``not_carried`` lists runs left out."""
        return self._c._send(
            "POST", f"collections/{collection_id}/copy", models.Collection, _query(changes)
        )

    def delete(self, collection_id: str) -> models.RecycleEntry:
        return self._c._send("DELETE", f"collections/{collection_id}", models.RecycleEntry)

    def compare(self, collection_id: str, **settings: Any) -> models.Comparison:
        """With the saved settings, or these for this call only."""
        return self._c._send(
            "POST",
            f"collections/{collection_id}/comparison",
            models.Comparison,
            _query(settings) or None,
        )

    def candidates(self, collection_id: str) -> list[models.CollectionCandidate]:
        """Recent runs not in the collection; ``mismatches`` is empty when one can join."""
        return self._c._get(
            f"collections/{collection_id}/candidates", list[models.CollectionCandidate]
        )

    def add_runs(
        self, collection_id: str, run_ids: list[str], *, labels: Mapping[str, str] | None = None
    ) -> models.Collection:
        runs = [_query({"run_id": x, "label": (labels or {}).get(x)}) for x in run_ids]
        return self._c._send(
            "POST", f"collections/{collection_id}/runs", models.Collection, {"runs": runs}
        )

    def label_run(self, collection_id: str, run_id: str, label: str | None) -> models.Collection:
        return self._c._send(
            "PUT", f"collections/{collection_id}/runs/{run_id}", models.Collection, {"label": label}
        )

    def remove_run(self, collection_id: str, run_id: str) -> models.Collection:
        return self._c._send(
            "DELETE", f"collections/{collection_id}/runs/{run_id}", models.Collection
        )

    def events(self, collection_id: str) -> list[models.CollectionEvent]:
        return self._c._get(f"collections/{collection_id}/events", list[models.CollectionEvent])


class Snapshots(_Resource):
    """Frozen comparisons of ended runs, with notes written by people or agents."""

    def list(self, *, collection_id: str | None = None) -> list[models.SnapshotSummary]:
        return self._c._get(
            "analysis-snapshots", list[models.SnapshotSummary], collection_id=collection_id
        )

    def get(self, snapshot_id: str) -> models.Snapshot:
        return self._c._get(f"analysis-snapshots/{snapshot_id}", models.Snapshot)

    def create(
        self,
        name: str,
        *,
        collection_id: str | None = None,
        run_ids: list[str] | None = None,
        settings: Mapping[str, Any] | None = None,
        note: str | None = None,
    ) -> models.Snapshot:
        body = {
            "name": name,
            **_query(
                {
                    "collection_id": collection_id,
                    "run_ids": run_ids,
                    "settings": settings,
                    "note": note,
                }
            ),
        }
        return self._c._send("POST", "analysis-snapshots", models.Snapshot, body)

    def delete(self, snapshot_id: str) -> models.RecycleEntry:
        return self._c._send("DELETE", f"analysis-snapshots/{snapshot_id}", models.RecycleEntry)

    def add_note(self, snapshot_id: str, body: str) -> models.Snapshot:
        return self._c._send(
            "POST", f"analysis-snapshots/{snapshot_id}/notes", models.Snapshot, {"body": body}
        )

    def update_note(self, snapshot_id: str, note_id: str, body: str) -> models.Snapshot:
        """Only the note's author may change it."""
        return self._c._send(
            "PUT",
            f"analysis-snapshots/{snapshot_id}/notes/{note_id}",
            models.Snapshot,
            {"body": body},
        )


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
        collection_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Submission:
        """Submit a resolved configuration (``resolution_id``) or one given inline.
        With ``collection_id`` the collection fills in its fixed fields and the run joins
        it; a run that does not match is refused.

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
                    "collection_id": collection_id,
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

    def download_archive(
        self,
        run_id: str,
        destination: str | os.PathLike[str],
        *,
        task_key: str | None = None,
        trial_id: str | None = None,
        attempt_id: str | None = None,
    ) -> Path:
        """Save the run's artifacts as one zip (``destination`` is the file path), or only
        one task's, trial's or attempt's. Ended runs only; its manifest.json lists the
        files left out and why."""
        params = _query({"task_key": task_key, "trial_id": trial_id, "attempt_id": attempt_id})
        return _save(self._c, f"runs/{run_id}/artifacts/archive", Path(destination), params)

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


def _save(
    client: MaestroClient, path: str, target: Path, params: Mapping[str, Any] | None = None
) -> Path:
    """Stream to a temporary file next to ``target``; keep it only if the digest matches."""
    target.parent.mkdir(parents=True, exist_ok=True)
    response = client._call(
        "GET", path, params=params, headers={"Accept": "application/octet-stream"}, stream=True
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
