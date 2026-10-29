"""Attachment snapshots: members and experiment PATs publish, everyone reads (03 section 6).

Publishing is incremental: ask which contents are missing, upload each as the raw
request body under its SHA-256, then commit the file list. The web page and the SDK
follow the same steps.
"""

import hashlib
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from maestro.api.deps import maestro, ok, principal, respond
from maestro.api.models import (
    Attachment,
    AttachmentContents,
    AttachmentDiff,
    AttachmentMissing,
    AttachmentPublish,
    EnabledChange,
    Envelope,
    RecycleEntry,
)
from maestro.api.routes.runs import attachment as disposition
from maestro.core.errors import MaestroError
from maestro.domain import limits
from maestro.execution.attachments import SHA256
from maestro.services.principal import Principal, require_experiment

router = APIRouter(prefix="/api/v1", tags=["attachments"])


@router.get("/attachments", response_model=Envelope[list[Attachment]])
def list_attachments(
    request: Request,
    name: str | None = None,
    include_disabled: bool = True,
    who: Principal = Depends(principal),
):
    return ok(request, maestro(request).attachments.list(who, name, include_disabled))


@router.get("/attachments/{attachment_id}", response_model=Envelope[Attachment])
def get_attachment(attachment_id: str, request: Request, who: Principal = Depends(principal)):
    """With the file list."""
    return ok(request, maestro(request).attachments.get(who, attachment_id))


@router.post("/attachment-contents/missing", response_model=Envelope[AttachmentMissing])
def missing_contents(
    body: AttachmentContents, request: Request, who: Principal = Depends(principal)
):
    """Which of these contents a publish still needs uploaded; the rest are stored."""
    return ok(request, {"missing": maestro(request).attachments.missing(who, body.sha256)})


@router.put(
    "/attachment-contents/{sha256}",
    status_code=204,
    openapi_extra={
        "requestBody": {
            "required": True,
            "description": "The file's bytes",
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    },
)
async def upload_content(sha256: str, request: Request, who: Principal = Depends(principal)):
    """One file's content, checked against its SHA-256; it waits for the uploader's
    publish for 24 hours."""
    limit = limits.ATTACHMENT_FILE_MAX_BYTES
    too_large = MaestroError("ATTACHMENT_TOO_LARGE", f"A file is at most {limit} bytes", 413)
    if not SHA256.fullmatch(sha256):
        raise MaestroError("VALIDATION_FAILED", "Not a lowercase hex SHA-256", 422)
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise too_large
    # Before anything is written to disk.
    require_experiment(who)
    if maestro(request).disk:
        maestro(request).disk.refuse_if_low("attachments")
    service = maestro(request).attachments
    allowance = await run_in_threadpool(service.upload_allowance, who, sha256)
    staging_full = MaestroError(
        "ATTACHMENT_TOO_LARGE",
        "Unpublished uploads are over the limit; publish or wait for them to expire",
        413,
    )
    if declared and declared.isdigit() and int(declared) > allowance:
        raise staging_full
    spool = service.store.receive()
    path = Path(spool.name)
    try:
        digest = hashlib.sha256()
        size = 0
        with spool:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise too_large
                if size > allowance:
                    raise staging_full
                digest.update(chunk)
                spool.write(chunk)
        if digest.hexdigest() != sha256:
            raise MaestroError(
                "ATTACHMENT_DIGEST_MISMATCH", "The body's SHA-256 is not the one named", 422
            )
        await run_in_threadpool(service.add_upload, who, sha256, path, size)
    finally:
        path.unlink(missing_ok=True)


@router.post(
    "/attachments",
    response_model=Envelope[Attachment],
    status_code=201,
    responses={200: {"description": "A snapshot with this content was already published"}},
)
def publish_attachment(
    body: AttachmentPublish, request: Request, who: Principal = Depends(principal)
):
    """Every content must be stored: held by another snapshot or uploaded by you."""
    data, created = maestro(request).attachments.publish(who, body.model_dump())
    return respond(request, Envelope[Attachment], data, 201 if created else 200)


@router.get(
    "/attachments/{attachment_id}/files/{path:path}",
    responses={200: {"content": {"application/octet-stream": {}}}},
)
def download_file(
    attachment_id: str, path: str, request: Request, who: Principal = Depends(principal)
):
    blob, entry = maestro(request).attachments.file(who, attachment_id, path)
    # Opened now: a purge that removes the content later does not cut this response.
    reader = blob.open("rb")

    def chunks():
        with reader:
            while chunk := reader.read(1 << 16):
                yield chunk

    return StreamingResponse(
        chunks(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": disposition(entry["path"].rsplit("/", 1)[-1]),
            "Content-Length": str(entry["size"]),
            "X-Content-Digest": f"sha256:{entry['sha256']}",
            "X-Executable": "true" if entry["executable"] else "false",
        },
    )


@router.get(
    "/attachments/{attachment_id}/archive",
    responses={200: {"content": {"application/zip": {}}}},
)
async def download_archive(
    attachment_id: str, request: Request, who: Principal = Depends(principal)
):
    """The whole snapshot as a zip, inside one folder named after it."""
    path, name = await run_in_threadpool(maestro(request).attachments.archive, who, attachment_id)
    return FileResponse(
        path,
        media_type="application/zip",
        filename=name,
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


@router.get("/attachments/{attachment_id}/diff", response_model=Envelope[AttachmentDiff])
def attachment_diff(
    attachment_id: str,
    request: Request,
    base_id: str | None = None,
    who: Principal = Depends(principal),
):
    """Files added, removed and changed since ``base_id`` (default: the parent)."""
    return ok(request, maestro(request).attachments.diff(who, attachment_id, base_id))


@router.post("/attachments/{attachment_id}/enabled", response_model=Envelope[Attachment])
def attachment_enabled(
    attachment_id: str, body: EnabledChange, request: Request, who: Principal = Depends(principal)
):
    """The owner or an administrator; disabled snapshots cannot be chosen for new runs."""
    return ok(request, maestro(request).attachments.set_enabled(who, attachment_id, body.enabled))


@router.delete("/attachments/{attachment_id}", response_model=Envelope[RecycleEntry])
def delete_attachment(attachment_id: str, request: Request, who: Principal = Depends(principal)):
    """Into the recycle bin; the owner or an administrator."""
    return ok(request, maestro(request).recycle.delete(who, "attachment_version", attachment_id))
