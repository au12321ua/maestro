"""Shared experiment topics and their explicit and derived references."""

from fastapi import APIRouter, Depends, Request

from maestro.api.deps import experiment, maestro, ok, principal
from maestro.api.models import (
    Envelope,
    RecycleEntry,
    Topic,
    TopicCreate,
    TopicMaterial,
    TopicMaterialKind,
    TopicOverview,
    TopicReference,
    TopicReferenceChanges,
    TopicReferenceKind,
    TopicReferenceResult,
    TopicUpdate,
)
from maestro.services.principal import Principal

router = APIRouter(prefix="/api/v1/topics", tags=["topics"])


@router.get("", response_model=Envelope[list[Topic]])
def list_topics(
    request: Request,
    q: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
    who: Principal = Depends(principal),
):
    """Search by name or stable ID. Deleted topics are hidden."""
    items, next_cursor = maestro(request).topics.list(who, q, cursor, limit)
    return ok(request, items, next_cursor)


@router.post("", response_model=Envelope[Topic], status_code=201)
def create_topic(body: TopicCreate, request: Request, who: Principal = Depends(experiment)):
    return ok(request, maestro(request).topics.create(who, body.model_dump()))


@router.get("/{topic_id}", response_model=Envelope[Topic])
def get_topic(topic_id: str, request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).topics.get(who, topic_id))


@router.put("/{topic_id}", response_model=Envelope[Topic])
def update_topic(
    topic_id: str, body: TopicUpdate, request: Request, who: Principal = Depends(experiment)
):
    return ok(
        request, maestro(request).topics.update(who, topic_id, body.model_dump(exclude_none=True))
    )


@router.delete("/{topic_id}", response_model=Envelope[RecycleEntry])
def delete_topic(topic_id: str, request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).recycle.delete(who, "topic", topic_id))


@router.get("/{topic_id}/overview", response_model=Envelope[TopicOverview])
def topic_overview(topic_id: str, request: Request, who: Principal = Depends(principal)):
    """Recent and active runs, selected objects, actual materials and the latest handoff."""
    return ok(request, maestro(request).topics.overview(who, topic_id))


@router.get("/{topic_id}/references", response_model=Envelope[list[TopicReference]])
def list_topic_references(
    topic_id: str,
    request: Request,
    kind: TopicReferenceKind,
    cursor: str | None = None,
    limit: int | None = None,
    _: Principal = Depends(principal),
):
    """Explicit references, including the deletion state of their original objects."""
    items, next_cursor = maestro(request).topics.references(topic_id, kind, cursor, limit)
    return ok(request, items, next_cursor)


@router.post("/{topic_id}/references", response_model=Envelope[TopicReferenceResult])
def change_topic_references(
    topic_id: str,
    body: TopicReferenceChanges,
    request: Request,
    who: Principal = Depends(experiment),
):
    """Add or remove a batch atomically. Repeating an add or remove is safe."""
    return ok(request, maestro(request).topics.change_references(who, topic_id, body.model_dump()))


@router.get("/{topic_id}/materials", response_model=Envelope[list[TopicMaterial]])
def list_topic_materials(
    topic_id: str,
    request: Request,
    kind: TopicMaterialKind,
    cursor: str | None = None,
    limit: int | None = None,
    _: Principal = Depends(principal),
):
    """Versions and snapshots frozen by the explicitly selected live runs."""
    items, next_cursor = maestro(request).topics.materials(topic_id, kind, cursor, limit)
    return ok(request, items, next_cursor)
