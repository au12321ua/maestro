"""Administrator web-session endpoints: members, invitations, credentials, master key,
execution capacity, deployment settings (read only), dataset imports, first preparation, cache eviction, admission,
deletion of team objects and the recycle bin."""

import dataclasses
import os
from pathlib import Path

from fastapi import APIRouter, Depends, Request

from maestro.api.deps import admin_session, maestro, ok, principal
from maestro.api.models import (
    AdmissionRevocation,
    AdmissionRevoked,
    AuditEvent,
    CapacityOverview,
    Credential,
    CredentialCreate,
    CredentialValue,
    DatasetImport,
    DeploymentSettings,
    Envelope,
    FirstPreparation,
    Invitation,
    InvitationCreate,
    IssuedInvitation,
    MasterKeyRotation,
    MasterKeyStatus,
    ObjectType,
    Operation,
    ProviderLimit,
    PurgeRequest,
    RecycleEntry,
    ScoringContract,
    ScoringContractInput,
    StatusChange,
    User,
)
from maestro.domain.states import Role, UserStatus
from maestro.services import audit_queries
from maestro.services.principal import Principal

router = APIRouter(prefix="/api/v1/admin", tags=["admin"], dependencies=[Depends(admin_session)])


@router.get("/members", response_model=Envelope[list[User]])
def list_members(request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).accounts.list_members(who))


@router.post("/members/{user_id}/disable", response_model=Envelope[User])
def disable_member(
    user_id: str, body: StatusChange, request: Request, who: Principal = Depends(principal)
):
    service = maestro(request).accounts
    return ok(
        request, service.set_status(who, user_id, UserStatus.DISABLED, body.expected_revision)
    )


@router.post("/members/{user_id}/enable", response_model=Envelope[User])
def enable_member(
    user_id: str, body: StatusChange, request: Request, who: Principal = Depends(principal)
):
    service = maestro(request).accounts
    return ok(request, service.set_status(who, user_id, UserStatus.ACTIVE, body.expected_revision))


@router.post(
    "/members/{user_id}/password-reset",
    response_model=Envelope[IssuedInvitation],
    status_code=201,
)
def password_reset(user_id: str, request: Request, who: Principal = Depends(principal)):
    service = maestro(request).accounts
    return ok(
        request,
        service.create_invitation(who, Role.MEMBER, "password reset", target_user_id=user_id),
    )


@router.get("/invitations", response_model=Envelope[list[Invitation]])
def list_invitations(request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).accounts.list_invitations(who))


@router.post("/invitations", response_model=Envelope[IssuedInvitation], status_code=201)
def create_invitation(
    body: InvitationCreate, request: Request, who: Principal = Depends(principal)
):
    service = maestro(request).accounts
    return ok(request, service.create_invitation(who, Role(body.role), body.note, body.valid_hours))


@router.post("/invitations/{invitation_id}/revoke", response_model=Envelope[Invitation])
def revoke_invitation(invitation_id: str, request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).accounts.revoke_invitation(who, invitation_id))


@router.get("/credentials", response_model=Envelope[list[Credential]])
def list_credentials(request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).credentials.list(who))


@router.post("/credentials", response_model=Envelope[Credential], status_code=201)
def create_credential(
    body: CredentialCreate, request: Request, who: Principal = Depends(principal)
):
    service = maestro(request).credentials
    return ok(
        request,
        service.create(who, body.name, body.kind, body.provider, body.description, body.value),
    )


@router.put("/credentials/{credential_id}/value", response_model=Envelope[Credential])
def replace_value(
    credential_id: str, body: CredentialValue, request: Request, who: Principal = Depends(principal)
):
    service = maestro(request).credentials
    return ok(
        request, service.replace_value(who, credential_id, body.value, body.expected_revision)
    )


@router.post("/credentials/{credential_id}/disable", response_model=Envelope[Credential])
def disable_credential(
    credential_id: str, body: StatusChange, request: Request, who: Principal = Depends(principal)
):
    service = maestro(request).credentials
    return ok(request, service.set_enabled(who, credential_id, False, body.expected_revision))


@router.post("/credentials/{credential_id}/enable", response_model=Envelope[Credential])
def enable_credential(
    credential_id: str, body: StatusChange, request: Request, who: Principal = Depends(principal)
):
    service = maestro(request).credentials
    return ok(request, service.set_enabled(who, credential_id, True, body.expected_revision))


@router.get("/master-key", response_model=Envelope[MasterKeyStatus])
def get_master_key(request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).credentials.master_status(who))


@router.post("/master-key/rotations", response_model=Envelope[MasterKeyStatus])
def rotate_master_key(
    body: MasterKeyRotation, request: Request, who: Principal = Depends(principal)
):
    return ok(request, maestro(request).credentials.rotate_master(who, body.expected_generation))


@router.get("/audit-events", response_model=Envelope[list[AuditEvent]])
def list_audit_events(
    request: Request,
    object_type: str | None = None,
    object_id: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
    who: Principal = Depends(principal),
):
    items, next_cursor = audit_queries.list_events(
        maestro(request).db,
        who,
        object_type=object_type,
        object_id=object_id,
        cursor=cursor,
        limit=limit,
    )
    return ok(request, items, next_cursor)


@router.get("/capacity", response_model=Envelope[CapacityOverview])
def get_capacity(request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).capacity.overview(who))


@router.get("/deployment", response_model=Envelope[DeploymentSettings])
def get_deployment(request: Request, who: Principal = Depends(principal)):
    """config.toml in force and whether the service has a proxy; changed on the host."""
    settings = maestro(request).settings
    values = {
        f.name: str(v) if isinstance(v := getattr(settings, f.name), Path) else v
        for f in dataclasses.fields(settings)
        if f.name != "retired"
    }
    proxy = any(
        os.environ.get(n) for n in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
    )
    return ok(
        request,
        {"values": values, "retired": list(settings.retired), "service_proxy": proxy},
    )


@router.put("/capacity/providers/{provider}", response_model=Envelope[CapacityOverview])
def provider_limit(
    provider: str, body: ProviderLimit, request: Request, who: Principal = Depends(principal)
):
    service = maestro(request).capacity
    return ok(request, service.set_provider_limit(who, provider, body.max_concurrent_trials))


# --- deletion (07 section 8) ------------------------------------------------------------


@router.delete("/members/{user_id}", response_model=Envelope[RecycleEntry])
def delete_member(user_id: str, request: Request, who: Principal = Depends(principal)):
    """Sessions and access tokens stop working at once; history keeps the name."""
    return ok(request, maestro(request).recycle.delete(who, "user", user_id))


@router.delete("/credentials/{credential_id}", response_model=Envelope[RecycleEntry])
def delete_credential(credential_id: str, request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).recycle.delete(who, "credential", credential_id))


@router.delete("/datasets/{dataset_id}", response_model=Envelope[RecycleEntry])
def delete_dataset(dataset_id: str, request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).recycle.delete(who, "dataset", dataset_id))


@router.delete("/dataset-versions/{version_id}", response_model=Envelope[RecycleEntry])
def delete_dataset_version(version_id: str, request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).recycle.delete(who, "dataset_version", version_id))


@router.delete("/pats/{pat_id}", response_model=Envelope[RecycleEntry])
def admin_delete_pat(pat_id: str, request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).recycle.delete(who, "pat", pat_id))


@router.get("/recycle-bin", response_model=Envelope[list[RecycleEntry]])
def list_recycle_bin(
    request: Request,
    state: str = "recycled",
    object_type: ObjectType | None = None,
    cursor: str | None = None,
    limit: int | None = None,
    who: Principal = Depends(principal),
):
    """state=recycled lists the bin; state=purged lists deletion receipts."""
    items, next_cursor = maestro(request).recycle.list(who, state, object_type, cursor, limit)
    return ok(request, items, next_cursor)


@router.post("/recycle-bin/{entry_id}/restore", response_model=Envelope[RecycleEntry])
def restore_entry(entry_id: str, request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).recycle.restore(who, entry_id))


@router.post("/recycle-bin/{entry_id}/purge", response_model=Envelope[RecycleEntry])
def purge_entry(
    entry_id: str, body: PurgeRequest, request: Request, who: Principal = Depends(principal)
):
    """Irreversible: removes content and files; the entry stays as the receipt."""
    service = maestro(request)
    return ok(request, service.recycle.purge(who, entry_id, body.confirm_object_id))


# --- datasets and preparation (02) -----------------------------------------------------


@router.post("/dataset-imports", response_model=Envelope[Operation], status_code=202)
def import_dataset(body: DatasetImport, request: Request, who: Principal = Depends(principal)):
    """Resolve a reference to a fixed digest, fetch and record the version (02 section 3)."""
    return ok(request, maestro(request).datasets.request_import(who, body.model_dump()))


@router.post(
    "/dataset-versions/{version_id}/preparations",
    response_model=Envelope[Operation],
    status_code=202,
)
def first_preparation(
    version_id: str, body: FirstPreparation, request: Request, who: Principal = Depends(principal)
):
    """First preparation, or retry of the tasks whose first preparation failed."""
    service = maestro(request).datasets
    return ok(request, service.request_first_preparation(who, version_id, body.task_ids))


@router.post(
    "/preparations/{preparation_id}/evict", response_model=Envelope[Operation], status_code=202
)
def evict_preparation(preparation_id: str, request: Request, who: Principal = Depends(principal)):
    """Remove the local image; admission and the recoverable original stay."""
    return ok(request, maestro(request).datasets.request_eviction(who, preparation_id))


@router.post("/task-admissions/{admission_id}/revoke", response_model=Envelope[AdmissionRevoked])
def revoke_admission(
    admission_id: str,
    body: AdmissionRevocation,
    request: Request,
    who: Principal = Depends(principal),
):
    service = maestro(request).datasets
    return ok(
        request, service.revoke_admission(who, admission_id, body.reason, body.expected_revision)
    )


@router.put(
    "/dataset-versions/{version_id}/scoring-contract", response_model=Envelope[ScoringContract]
)
def scoring_contract(
    version_id: str,
    body: ScoringContractInput,
    request: Request,
    who: Principal = Depends(principal),
):
    """Confirm how this version is scored; new runs freeze it (06 section 2)."""
    service = maestro(request).datasets
    return ok(request, service.confirm_scoring_contract(who, version_id, body.model_dump()))
