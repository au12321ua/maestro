"""Batches: several runs accepted together, all or none."""

from fastapi import APIRouter, Depends, Header, Request

from maestro.api.deps import maestro, ok, principal, respond
from maestro.api.models import Batch, BatchAccepted, BatchCreate, BatchPreview, Envelope
from maestro.core.errors import MaestroError
from maestro.services.principal import Principal

router = APIRouter(prefix="/api/v1", tags=["runs"])


@router.post("/run-batches/preview", response_model=Envelope[BatchPreview])
def preview_batch(body: BatchCreate, request: Request, who: Principal = Depends(principal)):
    """Resolve every member without accepting or charging anything."""
    return ok(
        request, maestro(request).runs.preview_batch(who, body.model_dump(exclude_unset=True))
    )


@router.post("/run-batches", response_model=Envelope[BatchAccepted], status_code=202)
def submit_batch(
    body: BatchCreate,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    who: Principal = Depends(principal),
):
    """One blocked member or too little PAT quota refuses the whole batch; a retry with
    the same key returns the same batch."""
    service = maestro(request)
    if service.worker is None:
        raise MaestroError(
            "EXECUTION_UNAVAILABLE", "No executor is configured", 503, stage="acceptance"
        )
    status, data, replayed = service.runs.submit_batch(
        who, idempotency_key, body.model_dump(exclude_unset=True)
    )
    service.worker.wake()
    headers = {"Idempotent-Replayed": "true"} if replayed else {}
    return respond(request, Envelope[BatchAccepted], data, status, headers)


@router.get("/run-batches", response_model=Envelope[list[Batch]])
def list_batches(
    request: Request,
    owner_id: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
    who: Principal = Depends(principal),
):
    items, next_cursor = maestro(request).runs.list_batches(who, owner_id, cursor, limit)
    return ok(request, items, next_cursor)


@router.get("/run-batches/{batch_id}", response_model=Envelope[Batch])
def get_batch(batch_id: str, request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).runs.get_batch(who, batch_id))


@router.post("/run-batches/{batch_id}/cancel", response_model=Envelope[Batch])
def cancel_batch(batch_id: str, request: Request, who: Principal = Depends(principal)):
    """Stops every member that has not ended; scored trials keep their results."""
    service = maestro(request)
    result = service.runs.cancel_batch(who, batch_id)
    if service.worker:
        service.worker.wake()
    return ok(request, result)
