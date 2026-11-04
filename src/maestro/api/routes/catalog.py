"""Dataset sources and the team-readable catalog.

Every authenticated caller may read the datasets, versions, tasks and caches already
in the platform. Searching the external source uses the team Hub credential, so it
needs an administrator's web session like the rest of ``admin``.
"""

from fastapi import APIRouter, Depends, Request

from maestro.api.deps import admin, maestro, ok, principal
from maestro.api.models import (
    Dataset,
    DatasetSource,
    DatasetVersionDetail,
    Envelope,
    Preparation,
    SourceDataset,
    SourceVersion,
    SourceVersionDetail,
    Task,
)
from maestro.domain.states import PatPermission
from maestro.services.principal import Principal

router = APIRouter(prefix="/api/v1", tags=["catalog"])
ADMIN = [Depends(admin(PatPermission.DATASETS))]


@router.get("/dataset-sources", response_model=Envelope[list[DatasetSource]])
def list_sources(request: Request, _: Principal = Depends(principal)):
    return ok(request, maestro(request).datasets.source_list())


@router.get("/dataset-search", response_model=Envelope[list[SourceDataset]], dependencies=ADMIN)
async def search_source(
    request: Request,
    q: str,
    source_kind: str = "hub",
    credential_id: str | None = None,
    who: Principal = Depends(principal),
):
    return ok(request, await maestro(request).datasets.search(who, source_kind, q, credential_id))


@router.get(
    "/dataset-search/versions", response_model=Envelope[list[SourceVersion]], dependencies=ADMIN
)
async def source_versions(
    request: Request,
    namespace: str,
    name: str,
    source_kind: str = "hub",
    credential_id: str | None = None,
    who: Principal = Depends(principal),
):
    service = maestro(request).datasets
    return ok(request, await service.versions(who, source_kind, namespace, name, credential_id))


@router.get(
    "/dataset-search/version", response_model=Envelope[SourceVersionDetail], dependencies=ADMIN
)
async def source_version(
    request: Request,
    namespace: str,
    name: str,
    ref: str,
    source_kind: str = "hub",
    credential_id: str | None = None,
    who: Principal = Depends(principal),
):
    service = maestro(request).datasets
    return ok(
        request, await service.describe(who, source_kind, namespace, name, ref, credential_id)
    )


@router.get("/datasets", response_model=Envelope[list[Dataset]])
def list_datasets(
    request: Request,
    cursor: str | None = None,
    limit: int | None = None,
    _: Principal = Depends(principal),
):
    items, next_cursor = maestro(request).catalog.list_datasets(cursor, limit)
    return ok(request, items, next_cursor)


@router.get("/dataset-versions/{version_id}", response_model=Envelope[DatasetVersionDetail])
def get_dataset_version(version_id: str, request: Request, _: Principal = Depends(principal)):
    return ok(request, maestro(request).catalog.get_version(version_id))


@router.get("/dataset-versions/{version_id}/tasks", response_model=Envelope[list[Task]])
def list_tasks(version_id: str, request: Request, _: Principal = Depends(principal)):
    return ok(request, maestro(request).catalog.list_tasks(version_id))


@router.get("/preparations/{preparation_id}", response_model=Envelope[Preparation])
def get_preparation(preparation_id: str, request: Request, _: Principal = Depends(principal)):
    return ok(request, maestro(request).catalog.get_preparation(preparation_id))
