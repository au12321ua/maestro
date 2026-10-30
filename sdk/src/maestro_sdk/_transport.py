"""HTTP for the resources: authentication, retries, paging, parsing and verified downloads.

The resources call these; only ``MaestroClient`` is public.
"""

from __future__ import annotations

import hashlib
import os
import random
import tempfile
import time
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, RootModel, TypeAdapter, ValidationError

from maestro_sdk.errors import ApiError, ContentMismatch, MaestroError, TransportError

# Answers that mean "not handled, try again": safe to repeat for reads and keyed submissions.
RETRY_STATUSES = frozenset({502, 503, 504})


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


class Transport:
    """One service's HTTP connection; ``MaestroClient`` adds the resources on top."""

    def __init__(
        self,
        base_url: str,
        token: str | None,
        *,
        timeout: float,
        retries: int,
        trust_env: bool,
        http: httpx.Client | None,
    ):
        self.base_url = _base_url(base_url)
        self.retries = retries
        self._token = token
        self._http = http or httpx.Client(
            base_url=self.base_url, timeout=timeout, trust_env=trust_env, follow_redirects=False
        )
        self._owns_http = http is None

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

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


def _key(value: str | None) -> str:
    return value or f"sdk-{uuid.uuid4()}"


class _Resource:
    def __init__(self, client: Transport):
        self._c = client


def _save(
    client: Transport, path: str, target: Path, params: Mapping[str, Any] | None = None
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
