"""Errors raised by the SDK. None of them carries the access token."""

from __future__ import annotations

from typing import Any


class MaestroError(Exception):
    """Base class of every SDK error."""


class ContractDriftWarning(UserWarning):
    """The service's OpenAPI document differs from the one this SDK was generated from."""


class ApiError(MaestroError):
    """The service answered with its structured error (10 section 2).

    ``retryable`` says whether the same business request may succeed later; it never
    means "submit again with a new idempotency key".
    """

    def __init__(self, status: int, body: dict[str, Any], request_id: str | None = None):
        self.status = status
        self.code: str = body.get("code", "HTTP_ERROR")
        self.message: str = body.get("message", f"HTTP {status}")
        self.stage: str | None = body.get("stage")
        self.field_errors: list[dict[str, Any]] = body.get("field_errors") or []
        self.retryable: bool = bool(body.get("retryable"))
        self.suggested_actions: list[dict[str, Any]] = body.get("suggested_actions") or []
        self.request_id = request_id
        self.body = body
        super().__init__(f"{status} {self.code}: {self.message}")


class TransportError(MaestroError):
    """No answer from the service: the outcome of the request is unknown.

    For a submission, retry with the same ``idempotency_key`` (kept on this error) or look
    the run up by its ``client_request_id``; a new key may start a second run.
    """

    def __init__(self, message: str, *, idempotency_key: str | None = None):
        self.idempotency_key = idempotency_key
        super().__init__(message)


class UnsupportedCapability(MaestroError):
    """The service does not offer what the caller needs (API version or feature)."""

    code = "UNSUPPORTED_CAPABILITY"


class BlockersPresent(MaestroError):
    """A configuration resolution has blockers; ``resolution.blockers`` lists them."""

    def __init__(self, resolution: Any):
        self.resolution = resolution
        lines = [f"{b.code} at {b.path}: {b.message}" for b in resolution.blockers]
        super().__init__("Configuration has blockers:\n  " + "\n  ".join(lines))


class WaitTimeout(MaestroError):
    """The client stopped waiting; the server-side work goes on (``last`` is what was seen)."""

    def __init__(self, last: Any, timeout: float):
        self.last = last
        super().__init__(f"Still {getattr(last, 'status', '?')} after {timeout:g} s")


class ContentMismatch(MaestroError):
    """A downloaded file does not have the digest the service reported."""
