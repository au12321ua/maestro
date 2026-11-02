"""The single error type crossing service and API layers."""

from dataclasses import dataclass, field


@dataclass
class FieldError:
    path: str
    code: str
    message: str | None = None
    limit: int | float | None = None


class MaestroError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status: int = 422,
        *,
        stage: str | None = None,
        field_errors: list[FieldError] | None = None,
        retryable: bool = False,
        suggested_actions: list[dict] | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.stage = stage
        self.field_errors = field_errors or []
        self.retryable = retryable
        self.suggested_actions = suggested_actions or []
        self.headers = headers or {}


@dataclass
class Page:
    items: list
    next_cursor: str | None = None
    extra: dict = field(default_factory=dict)


def not_found(kind: str) -> MaestroError:
    return MaestroError("NOT_FOUND", f"{kind} not found or not visible", 404)


def conflict(code: str, message: str) -> MaestroError:
    return MaestroError(code, message, 409)
