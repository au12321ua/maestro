"""Experiment plans: saved partial configurations.

Members and experiment PATs create plans; the creator edits; everyone reads and copies;
the creator or an administrator archives or deletes.
"""

from fastapi import APIRouter, Depends, Request

from maestro.api.deps import maestro, ok, principal
from maestro.api.models import (
    Envelope,
    Plan,
    PlanArchive,
    PlanCopy,
    PlanCreate,
    PlanUpdate,
    RecycleEntry,
)
from maestro.services.principal import Principal

router = APIRouter(prefix="/api/v1", tags=["plans"])


@router.get("/plans", response_model=Envelope[list[Plan]])
def list_plans(
    request: Request, include_archived: bool = False, who: Principal = Depends(principal)
):
    return ok(request, maestro(request).plans.list(who, include_archived))


@router.get("/plans/{plan_id}", response_model=Envelope[Plan])
def get_plan(plan_id: str, request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).plans.get(who, plan_id))


@router.post("/plans", response_model=Envelope[Plan], status_code=201)
def create_plan(body: PlanCreate, request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).plans.create(who, body.model_dump()))


@router.put("/plans/{plan_id}", response_model=Envelope[Plan])
def update_plan(
    plan_id: str, body: PlanUpdate, request: Request, who: Principal = Depends(principal)
):
    """A new revision; runs that used an earlier one keep their values."""
    return ok(
        request, maestro(request).plans.update(who, plan_id, body.model_dump(exclude_unset=True))
    )


@router.post("/plans/{plan_id}/copy", response_model=Envelope[Plan], status_code=201)
def copy_plan(plan_id: str, body: PlanCopy, request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).plans.copy(who, plan_id, body.model_dump()))


@router.post("/plans/{plan_id}/archive", response_model=Envelope[Plan])
def archive_plan(
    plan_id: str, body: PlanArchive, request: Request, who: Principal = Depends(principal)
):
    """Archived plans cannot be used for new runs; runs that used them are unaffected."""
    return ok(request, maestro(request).plans.set_archived(who, plan_id, body.archived))


@router.delete("/plans/{plan_id}", response_model=Envelope[RecycleEntry])
def delete_plan(plan_id: str, request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).recycle.delete(who, "experiment_plan", plan_id))
