"""Comparisons, collections and analysis snapshots (06 section 8).

Everyone reads and compares; members and experiment PATs keep collections up together,
save snapshots and write notes; the creator or an administrator deletes.
"""

from fastapi import APIRouter, Depends, Request

from maestro.api.deps import maestro, ok, principal
from maestro.api.models import (
    Collection,
    CollectionCopy,
    CollectionCreate,
    CollectionEvent,
    CollectionRunLabel,
    CollectionRunsAdd,
    CollectionUpdate,
    Comparison,
    ComparisonRequest,
    ComparisonSettingsInput,
    Envelope,
    NoteBody,
    RecycleEntry,
    Snapshot,
    SnapshotCreate,
    SnapshotSummary,
)
from maestro.services.principal import Principal

router = APIRouter(prefix="/api/v1", tags=["collections"])


@router.post("/comparisons", response_model=Envelope[Comparison])
def compare_runs(body: ComparisonRequest, request: Request, who: Principal = Depends(principal)):
    """Compute a comparison of these runs; nothing is saved."""
    return ok(
        request, maestro(request).comparisons.compute(who, body.model_dump(exclude_unset=True))
    )


@router.get("/collections", response_model=Envelope[list[Collection]])
def list_collections(request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).collections.list(who))


@router.post("/collections", response_model=Envelope[Collection], status_code=201)
def create_collection(
    body: CollectionCreate, request: Request, who: Principal = Depends(principal)
):
    """The specification (scenario and fixed values) cannot change afterwards."""
    return ok(
        request, maestro(request).collections.create(who, body.model_dump(exclude_unset=True))
    )


@router.get("/collections/{collection_id}", response_model=Envelope[Collection])
def get_collection(collection_id: str, request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).collections.get(who, collection_id))


@router.put("/collections/{collection_id}", response_model=Envelope[Collection])
def update_collection(
    collection_id: str,
    body: CollectionUpdate,
    request: Request,
    who: Principal = Depends(principal),
):
    """Name, description and comparison settings."""
    return ok(
        request,
        maestro(request).collections.update(
            who, collection_id, body.model_dump(exclude_unset=True)
        ),
    )


@router.post(
    "/collections/{collection_id}/copy", response_model=Envelope[Collection], status_code=201
)
def copy_collection(
    collection_id: str, body: CollectionCopy, request: Request, who: Principal = Depends(principal)
):
    return ok(
        request,
        maestro(request).collections.copy(who, collection_id, body.model_dump(exclude_unset=True)),
    )


@router.delete("/collections/{collection_id}", response_model=Envelope[RecycleEntry])
def delete_collection(collection_id: str, request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).recycle.delete(who, "collection", collection_id))


@router.post("/collections/{collection_id}/comparison", response_model=Envelope[Comparison])
def compare_collection(
    collection_id: str,
    request: Request,
    body: ComparisonSettingsInput | None = None,
    who: Principal = Depends(principal),
):
    """The collection's comparison with its saved settings, or with these for this view."""
    settings = body.model_dump(exclude_unset=True) if body else None
    return ok(request, maestro(request).collections.compare(who, collection_id, settings))


@router.post("/collections/{collection_id}/runs", response_model=Envelope[Collection])
def add_collection_runs(
    collection_id: str,
    body: CollectionRunsAdd,
    request: Request,
    who: Principal = Depends(principal),
):
    """Runs that differ from the fixed values are refused with what differs."""
    return ok(
        request,
        maestro(request).collections.add_runs(
            who, collection_id, body.model_dump(exclude_unset=True)
        ),
    )


@router.put("/collections/{collection_id}/runs/{run_id}", response_model=Envelope[Collection])
def label_collection_run(
    collection_id: str,
    run_id: str,
    body: CollectionRunLabel,
    request: Request,
    who: Principal = Depends(principal),
):
    return ok(
        request,
        maestro(request).collections.label_run(who, collection_id, run_id, body.model_dump()),
    )


@router.delete("/collections/{collection_id}/runs/{run_id}", response_model=Envelope[Collection])
def remove_collection_run(
    collection_id: str, run_id: str, request: Request, who: Principal = Depends(principal)
):
    return ok(request, maestro(request).collections.remove_run(who, collection_id, run_id))


@router.get("/collections/{collection_id}/events", response_model=Envelope[list[CollectionEvent]])
def list_collection_events(
    collection_id: str, request: Request, who: Principal = Depends(principal)
):
    return ok(request, maestro(request).collections.events(who, collection_id))


@router.get("/analysis-snapshots", response_model=Envelope[list[SnapshotSummary]])
def list_snapshots(
    request: Request, collection_id: str | None = None, who: Principal = Depends(principal)
):
    return ok(request, maestro(request).snapshots.list(who, collection_id))


@router.post("/analysis-snapshots", response_model=Envelope[Snapshot], status_code=201)
def create_snapshot(body: SnapshotCreate, request: Request, who: Principal = Depends(principal)):
    """Freeze the comparison of ended runs, a collection's or the runs named."""
    return ok(request, maestro(request).snapshots.create(who, body.model_dump(exclude_unset=True)))


@router.get("/analysis-snapshots/{snapshot_id}", response_model=Envelope[Snapshot])
def get_snapshot(snapshot_id: str, request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).snapshots.get(who, snapshot_id))


@router.delete("/analysis-snapshots/{snapshot_id}", response_model=Envelope[RecycleEntry])
def delete_snapshot(snapshot_id: str, request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).recycle.delete(who, "analysis_snapshot", snapshot_id))


@router.post(
    "/analysis-snapshots/{snapshot_id}/notes", response_model=Envelope[Snapshot], status_code=201
)
def add_snapshot_note(
    snapshot_id: str, body: NoteBody, request: Request, who: Principal = Depends(principal)
):
    return ok(request, maestro(request).snapshots.add_note(who, snapshot_id, body.model_dump()))


@router.put("/analysis-snapshots/{snapshot_id}/notes/{note_id}", response_model=Envelope[Snapshot])
def update_snapshot_note(
    snapshot_id: str,
    note_id: str,
    body: NoteBody,
    request: Request,
    who: Principal = Depends(principal),
):
    """Only the note's author edits it."""
    return ok(
        request,
        maestro(request).snapshots.update_note(who, snapshot_id, note_id, body.model_dump()),
    )
