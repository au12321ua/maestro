"""Health, readiness and capability discovery (10 section 3)."""

from fastapi import APIRouter, Depends, Request

from maestro import __version__
from maestro.api.deps import maestro, ok, principal
from maestro.api.models import Capabilities, Envelope, Health, SystemStatus
from maestro.domain import limits
from maestro.execution.policy import POLICY_VERSION
from maestro.services.principal import Principal
from maestro.services.runs import EXECUTION_MODES

router = APIRouter()


def contract_digest(app) -> str:
    """Digest of this service's OpenAPI document; the SDK compares it with its own."""
    app.openapi()
    return app.state.contract_digest


def readiness(request: Request) -> dict:
    service = maestro(request)
    schema = service.schema_state()
    keys = service.keys.status()
    worker_task = getattr(request.app.state, "worker_task", None)
    executing = worker_task is not None and not worker_task.done()
    ready = schema["compatible"]
    storage = service.disk.status()
    return {
        "status": "ready" if ready else "maintenance",
        "accepting_runs": bool(
            ready and keys["available"] and executing and storage["level"] == "ok"
        ),
        "detail": {
            "storage": storage,
            "schema": schema,
            "master_key_available": keys["available"],
            "executor": service.executor.executor_id if service.executor else None,
            "worker_running": executing,
            "needs_setup": service.accounts.needs_setup() if ready else None,
        },
    }


@router.get("/health/live", response_model=Health, tags=["system"])
def health_live():
    return {"status": "alive"}


@router.get("/health/ready", response_model=Health, tags=["system"])
def health_ready(request: Request):
    state = readiness(request)
    # No sensitive details here; the authenticated system-status carries them.
    return {"status": state["status"], "accepting_runs": state["accepting_runs"]}


@router.get("/api/v1/system-status", response_model=Envelope[SystemStatus], tags=["system"])
def system_status(request: Request, _: Principal = Depends(principal)):
    return ok(request, readiness(request))


@router.get("/api/v1/capabilities", response_model=Envelope[Capabilities], tags=["system"])
def capabilities(request: Request):
    service = maestro(request)
    experiment = {"web_session": True, "pat_scopes": ["experiment"]}
    read = {"web_session": True, "pat_scopes": ["read", "experiment"]}
    admin = {"web_session": True, "pat_scopes": [], "role": "admin"}
    return ok(
        request,
        {
            "api_version": "v1",
            "service_version": __version__,
            "schema_revision": service.schema_state()["head"],
            "contract_digest": contract_digest(request.app),
            "features": {
                "runs": True,
                "execution": service.executor is not None,
                # Model runs need it; false or unknown before the host has been probed.
                "restricted_network": bool(
                    service.executor and service.executor.network_restriction
                ),
                "batches": True,
                "configuration_resolution": True,
                "model_configs": True,
                "team_policy": True,
                "plans": True,
                "attachments": True,
                "exports": True,
                "deletion": True,
                "collections": True,
                "comparisons": True,
                "analysis_snapshots": True,
                "dataset_import": True,
            },
            "limits": {
                **limits.structural(),
                **service.policies.current().values,
                **service.settings.effective_limits(),
            },
            "team_policy_revision": service.policies.current().revision,
            "auth": {
                "pat_header": "Authorization: Bearer <token>",
                "session_cookie": True,
                "csrf_header": "X-CSRF-Token",
            },
            "actions": {
                "runs.read": read,
                "runs.submit": experiment,
                "runs.submit_batch": experiment,
                "runs.rerun": experiment,
                "runs.cancel_own": experiment,
                "runs.cancel_any": admin,
                "runs.delete_own": experiment,
                "runs.delete_any": admin,
                "exports.create": read,
                "artifacts.delete": admin,
                "admin.recycle_bin": admin,
                "catalog.read": read,
                "catalog.search_source": admin,
                "datasets.import": admin,
                "operations.read": read,
                "pats.manage_own": {"web_session": True, "pat_scopes": []},
                "admin.members": admin,
                "admin.credentials": admin,
                "admin.master_key": admin,
                "admin.audit": admin,
                "admin.capacity": admin,
                "admin.model_configs": admin,
                "admin.harness_versions": admin,
                "admin.team_policy": admin,
                "config.resolve": read,
                "config.read": read,
                "credentials.names": read,
                "attachments.read": read,
                "attachments.publish": experiment,
                "attachments.manage_own": experiment,
                "attachments.manage_any": admin,
                "plans.read": read,
                "plans.create": experiment,
                "plans.edit_own": experiment,
                "plans.archive_any": admin,
                "comparisons.read": read,
                "collections.read": read,
                "collections.create": experiment,
                "collections.copy": experiment,
                "collections.edit": experiment,
                "collections.delete_own": experiment,
                "collections.delete_any": admin,
                "snapshots.read": read,
                "snapshots.create": experiment,
                "snapshots.note": experiment,
                "snapshots.edit_own_note": experiment,
                "snapshots.delete_own": experiment,
                "snapshots.delete_any": admin,
            },
            "execution": {
                "executor": service.executor.executor_id if service.executor else None,
                "modes": sorted(EXECUTION_MODES),
                "harness_adapters": service.models.public_adapters(),
                "policy_version": POLICY_VERSION,
                "capacity": service.capacity.host.public(),
            },
        },
    )
