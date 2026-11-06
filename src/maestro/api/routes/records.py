"""Shared reads, experiment writes, author-only edits and owned two-step deletion."""

from fastapi import APIRouter, Depends, Request

from maestro.api.deps import experiment, maestro, ok, principal
from maestro.api.models import (
    Envelope,
    RecordCreate,
    RecordRevision,
    RecordTargetType,
    RecordUpdate,
    RecycleEntry,
    WorkflowRecord,
)
from maestro.services.principal import Principal

router = APIRouter(prefix="/api/v1/records", tags=["records"])


@router.post("", response_model=Envelope[WorkflowRecord], status_code=201)
def create_record(body: RecordCreate, request: Request, who: Principal = Depends(experiment)):
    return ok(request, maestro(request).records.create(who, body.model_dump()))


@router.get("", response_model=Envelope[list[WorkflowRecord]])
def list_records(
    request: Request,
    target_type: RecordTargetType,
    target_id: str,
    snapshot_id: str | None = None,
    kind: str | None = None,
    who: Principal = Depends(principal),
):
    """An attachment name lists all its records; snapshot_id selects only pinned ones.

    Recycled targets remain readable. Purged targets return an empty list.
    """
    return ok(
        request,
        maestro(request).records.list(
            who,
            {
                "type": target_type,
                "id": target_id,
                "snapshot_id": snapshot_id,
            },
            kind,
        ),
    )


@router.get("/{record_id}", response_model=Envelope[WorkflowRecord])
def get_record(record_id: str, request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).records.get(who, record_id))


@router.put("/{record_id}", response_model=Envelope[WorkflowRecord])
def update_record(
    record_id: str,
    body: RecordUpdate,
    request: Request,
    who: Principal = Depends(experiment),
):
    return ok(
        request,
        maestro(request).records.update(who, record_id, body.model_dump(exclude_unset=True)),
    )


@router.get("/{record_id}/history", response_model=Envelope[list[RecordRevision]])
def record_history(record_id: str, request: Request, who: Principal = Depends(principal)):
    """All committed versions, including the current version, in revision order."""
    return ok(request, maestro(request).records.history(who, record_id))


@router.delete("/{record_id}", response_model=Envelope[RecycleEntry])
def delete_record(record_id: str, request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).recycle.delete(who, "workflow_record", record_id))
