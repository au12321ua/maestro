"""FastAPI application: envelope, errors, authentication and static web pages.

The same HTTP service serves the built web pages and the relative ``/api/v1``
API; TLS, proxies and domains belong to the deployment environment.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException

from maestro import __version__
from maestro.api import routes
from maestro.api.deps import new_request_id
from maestro.api.models import ErrorResponse
from maestro.application import Maestro
from maestro.core import context
from maestro.core.digests import digest_json as digest
from maestro.core.errors import MaestroError

log = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parent.parent / "static"
# On every response. No script/style CSP: the built pages and the Swagger page load assets
# the policy would have to enumerate; only framing is forbidden outright.
HARDENING_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": "frame-ancestors 'none'",
}


def error_response(error: MaestroError, request_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=error.status,
        headers=error.headers,
        content={
            "error": {
                "code": error.code,
                "message": error.message,
                "stage": error.stage,
                "field_errors": [
                    {k: v for k, v in vars(f).items() if v is not None} for f in error.field_errors
                ],
                "retryable": error.retryable,
                "suggested_actions": error.suggested_actions,
            },
            "meta": {"request_id": request_id},
        },
    )


def operation_id(route: APIRoute) -> str:
    # Handler names are the operation names the web client and the SDK see; keep them unique.
    return route.name


def public_openapi(app: FastAPI) -> dict:
    """The generated document, corrected where FastAPI's defaults differ from the wire.

    Every error, request-validation failures included, is the ErrorResponse envelope;
    FastAPI would describe a 422 HTTPValidationError body that is never sent.
    """
    if app.openapi_schema:
        return app.openapi_schema
    document = get_openapi(
        title=app.title, version=app.version, routes=app.routes, servers=app.servers
    )
    schemas = document["components"]["schemas"]
    for name in ("HTTPValidationError", "ValidationError"):
        schemas.pop(name, None)
    # No route returns the error envelope as its response model; add it and its parts.
    error_schema = ErrorResponse.model_json_schema(ref_template="#/components/schemas/{model}")
    schemas.update(error_schema.pop("$defs", {}))
    schemas["ErrorResponse"] = error_schema
    error = {
        "description": "Structured error",
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
    }
    seen: set[str] = set()
    for path in document["paths"].values():
        for operation in path.values():
            if operation["operationId"] in seen:
                raise RuntimeError(f"duplicate operationId {operation['operationId']}")
            seen.add(operation["operationId"])
            operation["responses"].pop("422", None)
            operation["responses"]["default"] = error
    app.openapi_schema = document
    app.state.contract_digest = digest(document)
    return document


def create_app(maestro: Maestro, *, run_worker: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = upkeep = updating = None
        if run_worker and maestro.schema_state()["compatible"]:
            # Reconcile stored files before anything can write new ones.
            maestro.maintenance.start()
            upkeep = asyncio.create_task(maestro.maintenance.run_forever())
            updating = asyncio.create_task(maestro.updates.run_forever())
            if maestro.worker is not None:
                # Before anything is accepted for execution: model runs need it.
                await maestro.executor.probe()
                report = maestro.worker.start_epoch()
                if report["interrupted_runs"]:
                    log.warning("interrupted runs after restart: %s", report["interrupted_runs"])
                task = asyncio.create_task(maestro.worker.run_forever())
        app.state.worker_task = task
        try:
            yield
        finally:
            if task is not None:
                await maestro.worker.stop()
            if upkeep is not None:
                await maestro.maintenance.stop()
            if updating is not None:
                await maestro.updates.stop()
            for running in (task, upkeep, updating):
                if running is None:
                    continue
                running.cancel()
                try:
                    await running
                except asyncio.CancelledError:
                    pass

    app = FastAPI(
        title="Maestro API",
        version=__version__,
        lifespan=lifespan,
        root_path=maestro.settings.root_path,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
        generate_unique_id_function=operation_id,
    )
    app.state.maestro = maestro

    @app.middleware("http")
    async def request_ids(request: Request, call_next):
        request_id = new_request_id()
        request.state.request_id = request_id
        token = context.request_id.set(request_id)
        try:
            response = await call_next(request)
        finally:
            context.request_id.reset(token)
        response.headers["X-Request-Id"] = request_id
        response.headers.setdefault("Cache-Control", "no-store")
        for name, value in HARDENING_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.exception_handler(MaestroError)
    async def maestro_error(request: Request, error: MaestroError):
        return error_response(error, request.state.request_id)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        from maestro.core.errors import FieldError

        fields = [
            FieldError(
                "/" + "/".join(str(p) for p in item["loc"][1:]),
                item["type"].upper(),
                item.get("msg"),
            )
            for item in error.errors()
        ]
        return error_response(
            MaestroError(
                "REQUEST_INVALID", "Request structure is invalid", 422, field_errors=fields
            ),
            request.state.request_id,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException):
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(error.status_code, "HTTP_ERROR")
        return error_response(
            MaestroError(code, str(error.detail), error.status_code), request.state.request_id
        )

    @app.exception_handler(Exception)
    async def unexpected(request: Request, error: Exception):
        log.exception("unhandled error", exc_info=error)
        response = error_response(
            MaestroError("INTERNAL_ERROR", "Unexpected server error", 500, retryable=True),
            request.state.request_id,
        )
        # The server-error layer sits outside the middleware above: add the headers here.
        response.headers.update(HARDENING_HEADERS)
        return response

    for router in routes.routers():
        app.include_router(router)
    app.openapi = lambda: public_openapi(app)

    @app.get("/{path:path}", include_in_schema=False)
    async def web(path: str):
        if path.startswith("api/") or path == "api":
            raise MaestroError("NOT_FOUND", "Unknown API path", 404)
        try:
            candidate = (STATIC / path).resolve()
            is_asset = bool(path) and candidate.is_file() and candidate.is_relative_to(STATIC)
        except (OSError, ValueError):  # a NUL byte or a name too long: not a file, so the page
            is_asset = False
        if is_asset:
            return FileResponse(candidate)
        index = STATIC / "index.html"
        if index.is_file():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        raise MaestroError("WEB_NOT_BUILT", "Web pages are not built", 404)

    return app
