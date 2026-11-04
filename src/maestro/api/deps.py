"""Authentication dependencies.

Bearer tokens are PATs only. Browser sessions use an HttpOnly cookie plus a
per-session CSRF header on unsafe methods. A PAT reaches an administrator route only
with the permission that route declares; session-only routes refuse every PAT.
"""

import secrets

from fastapi import Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter

from maestro.application import Maestro
from maestro.core.errors import MaestroError
from maestro.domain.states import PatPermission
from maestro.security import tokens
from maestro.services.principal import Principal, require_admin, require_admin_session

COOKIE = "maestro_session"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def new_request_id() -> str:
    return "req_" + secrets.token_hex(8)


def maestro(request: Request) -> Maestro:
    return request.app.state.maestro


def client_source(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def principal(request: Request) -> Principal:
    service = maestro(request)
    header = request.headers.get("authorization")
    if header:
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise MaestroError("UNAUTHENTICATED", "Use 'Authorization: Bearer <token>'", 401)
        return service.accounts.authenticate_pat(token.strip())
    cookie = request.cookies.get(COOKIE)
    if not cookie:
        raise MaestroError("UNAUTHENTICATED", "Sign in or provide an access token", 401)
    found = service.accounts.authenticate_session(cookie)
    if request.method not in SAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER, "")
        if not sent or not tokens.same(sent, tokens.csrf_for(cookie)):
            raise MaestroError("CSRF_FAILED", "Missing or invalid CSRF token", 403)
    return found


def admin_session(who: Principal = Depends(principal)) -> Principal:
    """Route-level guard for session-only administration (members, credentials, the
    master key, permanent deletion), so a new route cannot miss the check.

    The services still check for themselves; this makes the boundary visible per route
    and lets a test walk the routes (test_permissions).
    """
    require_admin_session(who)
    return who


_ADMIN_GUARDS: dict[PatPermission, object] = {}


def admin(permission: PatPermission):
    """Route-level guard for administration a PAT may do with ``permission``.

    One function per permission, carrying it as ``.permission`` for the route test.
    """
    guard = _ADMIN_GUARDS.get(permission)
    if guard is None:

        def guard(who: Principal = Depends(principal)) -> Principal:
            require_admin(who, permission)
            return who

        guard.permission = permission
        _ADMIN_GUARDS[permission] = guard
    return guard


def set_session_cookie(response: Response, service: Maestro, token: str) -> None:
    response.set_cookie(
        COOKIE,
        token,
        max_age=service.settings.session_hours * 3600,
        httponly=True,
        secure=service.settings.cookie_secure,
        samesite="lax",
        path=service.settings.root_path or "/",
    )


def clear_session_cookie(response: Response, service: Maestro) -> None:
    response.delete_cookie(COOKIE, path=service.settings.root_path or "/")


def ok(request: Request, data, next_cursor: str | None = None) -> dict:
    meta = {"request_id": request.state.request_id}
    if next_cursor is not None:
        meta["next_cursor"] = next_cursor
    return {"data": data, "meta": meta}


def respond(
    request: Request, model, data, status: int = 200, headers: dict | None = None
) -> JSONResponse:
    """A response with its own status or headers, checked against the declared model.

    FastAPI checks returned dicts against ``response_model`` but not a JSONResponse built
    by hand; this keeps every answer to the published contract.
    """
    body = TypeAdapter(model).validate_python(ok(request, data))
    return JSONResponse(
        status_code=status,
        content=TypeAdapter(model).dump_python(body, mode="json", by_alias=True),
        headers=headers,
    )
