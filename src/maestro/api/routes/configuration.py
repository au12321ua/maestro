"""Model configurations, harness versions, the team policy and configuration resolution.

Reading is open to every signed-in principal; changing is for administrators in a web
session. Resolution is the step before submission that web and SDK share (03).
"""

from fastapi import APIRouter, Depends, Request

from maestro.api.deps import admin_session, maestro, ok, principal
from maestro.api.models import (
    ConfigRequest,
    ConfigResolution,
    CredentialName,
    EnabledChange,
    Envelope,
    Harness,
    HarnessAdapter,
    HarnessCreate,
    ModelConfig,
    ModelConfigCreate,
    ModelConfigRevise,
    RecycleEntry,
    SetupNetwork,
    SetupNetworkUpdate,
    TeamPolicy,
    TeamPolicyRevision,
    TeamPolicyUpdate,
    UpstreamVersions,
)
from maestro.services.principal import Principal, require_admin_session

router = APIRouter(prefix="/api/v1", tags=["configuration"])
ADMIN = [Depends(admin_session)]


# --- reading ----------------------------------------------------------------------------


@router.get("/model-configs", response_model=Envelope[list[ModelConfig]])
def list_model_configs(
    request: Request, include_disabled: bool = False, who: Principal = Depends(principal)
):
    return ok(request, maestro(request).models.list_models(who, include_disabled))


@router.get("/model-configs/{config_id}", response_model=Envelope[ModelConfig])
def get_model_config(config_id: str, request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).models.get_model(who, config_id))


@router.get("/harness-versions", response_model=Envelope[list[Harness]])
def list_harnesses(request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).models.list_harnesses(who))


@router.get("/harness-versions/{harness_id}", response_model=Envelope[Harness])
def get_harness(harness_id: str, request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).models.get_harness(who, harness_id))


@router.get("/credentials", response_model=Envelope[list[CredentialName]])
def list_credential_names(request: Request, who: Principal = Depends(principal)):
    """Enabled team credentials by name, to reference secret parameters; no values."""
    return ok(request, maestro(request).credentials.directory(who))


@router.get("/team-policy", response_model=Envelope[TeamPolicy])
def get_team_policy(request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).policies.get(who))


@router.get("/setup-network", response_model=Envelope[SetupNetwork])
def get_setup_network(request: Request, who: Principal = Depends(principal)):
    """The setup network in force (N-08): read when setup work starts."""
    return ok(request, maestro(request).setup_networks.get(who))


@router.get("/setup-network/revisions", response_model=Envelope[list[SetupNetwork]])
def list_setup_network_revisions(request: Request, who: Principal = Depends(principal)):
    """Every revision, newest first and ending with revision 0; attempts name theirs."""
    return ok(request, maestro(request).setup_networks.history(who))


# --- resolution ---------------------------------------------------------------------


@router.post("/config-resolutions", response_model=Envelope[ConfigResolution], status_code=201)
def resolve_config(body: ConfigRequest, request: Request, who: Principal = Depends(principal)):
    """Effective configuration, where each value came from, and what blocks it. Valid
    for a few minutes; submit it with ``resolution_id``. Reserves nothing."""
    # Unset fields stay absent: an experiment plan may fill them in.
    return ok(request, maestro(request).runs.resolve(who, body.model_dump(exclude_unset=True)))


@router.get("/config-resolutions/{resolution_id}", response_model=Envelope[ConfigResolution])
def get_resolution(resolution_id: str, request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).runs.get_resolution(who, resolution_id))


# --- administration ---------------------------------------------------------------------


@router.get(
    "/admin/harness-adapters",
    response_model=Envelope[list[HarnessAdapter]],
    tags=["admin"],
    dependencies=ADMIN,
)
def list_adapters(request: Request, who: Principal = Depends(principal)):
    require_admin_session(who)
    return ok(request, maestro(request).models.public_adapters())


@router.get(
    "/admin/harness-adapters/{agent}/upstream-versions",
    response_model=Envelope[UpstreamVersions],
    tags=["admin"],
    dependencies=ADMIN,
)
def upstream_versions(
    agent: str, request: Request, refresh: bool = False, who: Principal = Depends(principal)
):
    """Release tags and recent releases of the program an adapter installs, looked up
    through the setup network; cached for a few minutes unless ``refresh``."""
    return ok(request, maestro(request).models.upstream_versions(who, agent, refresh))


@router.post(
    "/admin/model-configs",
    response_model=Envelope[ModelConfig],
    status_code=201,
    tags=["admin"],
    dependencies=ADMIN,
)
def create_model(body: ModelConfigCreate, request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).models.create_model(who, body.model_dump()))


@router.put(
    "/admin/model-configs/{config_id}",
    response_model=Envelope[ModelConfig],
    tags=["admin"],
    dependencies=ADMIN,
)
def revise_model(
    config_id: str, body: ModelConfigRevise, request: Request, who: Principal = Depends(principal)
):
    """A new revision; runs already accepted keep the revision they used."""
    fields = body.model_dump()
    expected = fields.pop("expected_revision")
    return ok(request, maestro(request).models.revise_model(who, config_id, fields, expected))


@router.post(
    "/admin/model-configs/{config_id}/enabled",
    response_model=Envelope[ModelConfig],
    tags=["admin"],
    dependencies=ADMIN,
)
def model_enabled(
    config_id: str, body: EnabledChange, request: Request, who: Principal = Depends(principal)
):
    return ok(request, maestro(request).models.set_model_enabled(who, config_id, body.enabled))


@router.delete(
    "/admin/model-configs/{config_id}",
    response_model=Envelope[RecycleEntry],
    tags=["admin"],
    dependencies=ADMIN,
)
def delete_model(config_id: str, request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).recycle.delete(who, "model_config", config_id))


@router.post(
    "/admin/harness-versions",
    response_model=Envelope[Harness],
    status_code=201,
    tags=["admin"],
    dependencies=ADMIN,
)
def create_harness(body: HarnessCreate, request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).models.create_harness(who, body.model_dump()))


@router.post(
    "/admin/harness-versions/{harness_id}/enabled",
    response_model=Envelope[Harness],
    tags=["admin"],
    dependencies=ADMIN,
)
def harness_enabled(
    harness_id: str, body: EnabledChange, request: Request, who: Principal = Depends(principal)
):
    return ok(request, maestro(request).models.set_harness_enabled(who, harness_id, body.enabled))


@router.delete(
    "/admin/harness-versions/{harness_id}",
    response_model=Envelope[RecycleEntry],
    tags=["admin"],
    dependencies=ADMIN,
)
def delete_harness(harness_id: str, request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).recycle.delete(who, "harness_version", harness_id))


@router.get(
    "/admin/team-policy/revisions",
    response_model=Envelope[list[TeamPolicyRevision]],
    tags=["admin"],
    dependencies=ADMIN,
)
def list_policy_revisions(request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).policies.history(who))


@router.put(
    "/admin/setup-network",
    response_model=Envelope[SetupNetwork],
    tags=["admin"],
    dependencies=ADMIN,
)
def update_setup_network(
    body: SetupNetworkUpdate, request: Request, who: Principal = Depends(principal)
):
    """Save a new revision; setup work that starts from now on uses it."""
    value = body.model_dump(exclude={"note", "expected_revision"})
    return ok(
        request,
        maestro(request).setup_networks.update(who, value, body.note, body.expected_revision),
    )


@router.put(
    "/admin/team-policy", response_model=Envelope[TeamPolicy], tags=["admin"], dependencies=ADMIN
)
def update_policy(body: TeamPolicyUpdate, request: Request, who: Principal = Depends(principal)):
    """Change some settings; a new revision takes effect for new work at once."""
    return ok(
        request,
        maestro(request).policies.update(who, body.values, body.expected_revision),
    )
