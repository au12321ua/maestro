"""Dataset sources and the team-readable catalog.

Every authenticated caller may read the datasets, versions, tasks and caches already
in the platform. Searching the external source uses the team Hub credential, so it
needs an administrator session or an administrator PAT with datasets permission.
"""

from fastapi import APIRouter, Depends, Request

from maestro.api.deps import admin, maestro, ok, principal
from maestro.api.models import (
    Dataset,
    DatasetSource,
    DatasetUpload,
    DatasetVersionDetail,
    DatasetVersionFile,
    Envelope,
    Operation,
    Preparation,
    SourceDataset,
    SourceVersion,
    SourceVersionDetail,
    Task,
    TaskDetail,
    TaskRead,
    TaskReadQuery,
)
from maestro.domain.states import PatPermission
from maestro.services import task_reads
from maestro.services.principal import Principal

router = APIRouter(prefix="/api/v1", tags=["catalog"])
ADMIN = [Depends(admin(PatPermission.DATASETS))]


@router.post(
    "/dataset-uploads", response_model=Envelope[Operation], status_code=202, dependencies=ADMIN
)
def upload_dataset(body: DatasetUpload, request: Request, who: Principal = Depends(principal)):
    """Import a file list, validate tasks with the Runner, then start first preparation."""
    service = maestro(request)
    service.disk.refuse_if_low("datasets")
    return ok(request, service.datasets.request_upload(who, body.model_dump()))


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


@router.get(
    "/dataset-versions/{version_id}/files", response_model=Envelope[list[DatasetVersionFile]]
)
def list_dataset_version_files(
    version_id: str, request: Request, _: Principal = Depends(principal)
):
    """List original upload metadata without reading or returning task contents."""
    return ok(request, maestro(request).catalog.files(version_id))


@router.get("/preparations/{preparation_id}", response_model=Envelope[Preparation])
def get_preparation(preparation_id: str, request: Request, _: Principal = Depends(principal)):
    return ok(request, maestro(request).catalog.get_preparation(preparation_id))


@router.get("/tasks/{task_id}", response_model=Envelope[TaskDetail])
def get_task(task_id: str, request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).catalog.task(who, task_id))


@router.post("/tasks/reads", response_model=Envelope[list[TaskRead]])
def get_task_reads(body: TaskReadQuery, request: Request, _: Principal = Depends(principal)):
    return ok(request, task_reads.query(maestro(request).db, body.task_ids))
