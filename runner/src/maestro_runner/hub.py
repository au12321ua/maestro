"""Harbor Hub catalog commands: search, versions, describe, fetch.

One command per Runner process; the answer is a single ``result`` event. Failures
are classified for the platform (auth / transient / not_found / mismatch /
unsupported) with a short, redacted message: a timeout, 429, 5xx or missing
permission is never reported as content that is gone for good.
"""

import os
import shutil
import tempfile
from pathlib import Path

MESSAGE_LIMIT = 1000
SEARCH_LIMIT = 50


class HubError(Exception):
    def __init__(self, kind: str, message: str, evidence: dict | None = None):
        super().__init__(message)
        self.kind = kind
        self.evidence = evidence or {}


def classify(error: BaseException) -> HubError:
    """Map Harbor / Supabase / httpx failures to platform kinds by type and status."""
    if isinstance(error, HubError):
        return error
    import httpx
    from harbor.auth.errors import AuthenticationError, NotAuthenticatedError
    from harbor.db.client import TaskVersionNotFoundError
    from harbor.registry.client.base import UnavailableDatasetTasksError

    message = f"{type(error).__name__}: {error}"[:MESSAGE_LIMIT]
    if isinstance(error, NotAuthenticatedError | AuthenticationError):
        return HubError("auth", message)
    if isinstance(error, UnavailableDatasetTasksError):
        # Inaccessible tasks are hidden rows: permission, not proof of deletion.
        return HubError("auth", message)
    if isinstance(error, TaskVersionNotFoundError):
        return HubError("not_found", message)
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status in (401, 403):
            return HubError("auth", message, {"status": status})
        if status == 404:
            return HubError("not_found", message, {"status": status})
        return HubError("transient", message, {"status": status})
    if isinstance(error, httpx.TransportError | TimeoutError | ConnectionError | OSError):
        return HubError("transient", message)
    code = getattr(error, "code", None)  # postgrest APIError
    if code is not None:
        text = str(code)
        if text in {"401", "403", "PGRST301", "PGRST302", "PGRST303", "42501"}:
            return HubError("auth", message, {"code": text})
        return HubError("transient", message, {"code": text})
    if isinstance(error, ValueError) and "not found" in str(error).lower():
        return HubError("not_found", message)
    return HubError("transient", message)


async def search(request: dict) -> list[dict]:
    from harbor.auth.client import create_authenticated_client

    query = str(request.get("query") or "").strip()
    org, _, name = query.rpartition("/")
    client = await create_authenticated_client()
    builder = (
        client.table("package")
        .select("name, type, visibility, org:org_id!inner(name)")
        .eq("type", "dataset")
    )
    if org:
        builder = builder.eq("org.name", org)
    if name:
        builder = builder.ilike("name", f"%{name}%")
    response = await builder.order("name").limit(SEARCH_LIMIT).execute()
    return [
        {
            "namespace": row["org"]["name"],
            "name": row["name"],
            "visibility": row.get("visibility"),
        }
        for row in response.data or []
    ]


async def versions(request: dict) -> list[dict]:
    from harbor.db.client import RegistryDB

    org, name = request["namespace"], request["name"]
    package, rows = await RegistryDB().list_package_versions(
        org=org, name=name, include_yanked=True
    )
    if package.get("type") != "dataset":
        raise HubError("unsupported", f"{org}/{name} is not a dataset")
    return [
        {
            "ref": str(row["revision"]),
            "revision": row["revision"],
            "tags": row.get("tags") or [],
            "resolved_digest": row["content_hash"],
            "published_at": row.get("published_at"),
            "yanked": bool(row.get("yanked_at")),
            "yanked_reason": row.get("yanked_reason"),
        }
        for row in rows
    ]


async def describe(request: dict) -> dict:
    from harbor.models.package.version_ref import VersionRef
    from harbor.registry.client.package import PackageDatasetClient

    org, name = request["namespace"], request["name"]
    ref = str(request["ref"])
    try:
        parsed = VersionRef.parse(ref)
    except ValueError as error:
        raise HubError("unsupported", f"invalid version reference: {error}") from None
    metadata = await PackageDatasetClient().get_dataset_metadata(f"{org}/{name}@{ref}")
    if not metadata.version.startswith("sha256:"):
        raise HubError("unsupported", "the dataset did not resolve to a fixed digest")
    if metadata.files or metadata.metrics:
        # Dataset-level metrics and files are outside the platform contract for now.
        raise HubError("unsupported", "dataset-level files or metrics are not supported")
    tasks = []
    for task in metadata.task_ids:
        if not task.ref or not task.ref.startswith("sha256:"):
            raise HubError("unsupported", f"task {task.get_name()} is not pinned to a digest")
        tasks.append(
            {
                "task_key": task.get_name(),
                "content_digest": task.ref,
                "locator": {"org": task.org, "name": task.name, "ref": task.ref},
            }
        )
    keys = [t["task_key"] for t in tasks]
    if len(keys) != len(set(keys)):
        raise HubError("unsupported", "task names are not unique in this version")
    return {
        "namespace": org,
        "name": name,
        "upstream_ref": ref,
        "ref_kind": parsed.type.value,
        "resolved_digest": metadata.version,
        "description": metadata.description or "",
        "detail": {"hub_version_id": metadata.dataset_version_id},
        "tasks": tasks,
    }


async def fetch(request: dict) -> dict:
    """Download one task by digest into ``destination``, verified with Harbor's own
    package hash; the service separately records the digest of the files."""
    from harbor.models.task.config import TaskConfig as TaskFile
    from harbor.models.task.id import PackageTaskId
    from harbor.publisher.packager import Packager
    from harbor.tasks.client import TaskClient

    locator = request["locator"]
    expected = request["content_digest"]
    destination = Path(request["destination"])
    if destination.exists():
        raise HubError("unsupported", "destination already exists")
    task_id = PackageTaskId(org=locator["org"], name=locator["name"], ref=locator["ref"])
    if locator["ref"] != expected:
        raise HubError("unsupported", "locator ref differs from the expected digest")
    scratch = Path(tempfile.mkdtemp(prefix=".hub-", dir=destination.parent))
    try:
        result = await TaskClient().download_tasks([task_id], output_dir=scratch, export=True)
        path = result.results[0].path
        actual = "sha256:" + Packager.compute_content_hash(path)[0]
        if actual != expected:
            raise HubError(
                "mismatch",
                "downloaded task differs from the fixed digest",
                {"expected_digest": expected, "actual_digest": actual},
            )
        try:
            TaskFile.model_validate_toml((path / "task.toml").read_text())
        except Exception as error:  # noqa: BLE001 - any schema failure is unsupported
            raise HubError("unsupported", f"task.toml rejected by Harbor: {error}"[:1000]) from None
        os.rename(path, destination)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return {"content_digest": expected, "path": str(destination)}


COMMANDS = {"search": search, "versions": versions, "describe": describe, "fetch": fetch}
