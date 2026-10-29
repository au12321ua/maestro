"""Generated from openapi/maestro-v1.json by tools/generate_clients.py. Do not edit.

Every model allows fields it does not know, so a newer service adds fields without
breaking an older SDK.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel


class ActionRule(BaseModel):
    """
    Who may do an action: a web session (of an admin when ``role`` says so) and PATs
    of the listed scopes.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    pat_scopes: list[Literal["read", "experiment"]]
    role: Literal["admin", "member"] | None
    web_session: bool


class AdmissionRecord(BaseModel):
    """
    The admission and preparation a run's task was accepted with.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    admission_id: str | None
    admission_revision: int | None
    expected_image_id: str | None
    preparation_method_digest: str | None
    spec_digest: str
    task_key: str


class AdmissionRevocation(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    expected_revision: int
    reason: str


class AdmissionRevoked(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    id: str
    revision: int
    state: Literal["revoked"]


class Artifact(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    attempt_id: str | None
    created_at: str
    digest: str | None
    expires_at: Annotated[
        str | None,
        Field(
            description="When the file is reclaimed: trial (or run) end + TTL; null until it ends"
        ),
    ]
    id: str
    kind: str
    relative_path: str
    run_id: str
    size_bytes: int | None
    status: Literal[
        "pending", "available", "failed", "deleting", "expired", "unavailable", "deleted"
    ]
    status_reason: Annotated[
        str | None,
        Field(description="e.g. STORAGE_WRITE_FAILED, STORAGE_CONTENT_MISSING, RETENTION_EXPIRED"),
    ]
    trial_id: str | None


class ArtifactCounts(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    available: int
    expired: int
    failed: Annotated[int, Field(description="Not saved, or found missing from storage")]


class AttachmentContents(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    sha256: list[str]


class AttachmentFile(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    executable: bool
    path: str
    sha256: str
    size: int


class AttachmentFileChange(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    after: AttachmentFile
    before: AttachmentFile
    path: str


class AttachmentFileEntry(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    executable: bool
    path: Annotated[str, Field(description="Relative, made of letters, digits and ._+@-")]
    sha256: Annotated[str, Field(description="Lowercase hex")]


class AttachmentMissing(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    missing: Annotated[list[str], Field(description="Contents to upload before publishing")]


class AttachmentProvides(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    env: list[str]
    tools: list[str]


class AttachmentPublish(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    files: Annotated[
        list[AttachmentFileEntry],
        Field(description="Every file of the snapshot; upload missing contents first"),
    ]
    label: Annotated[
        str | None,
        Field(description="Unique per attachment name; never moves to other content"),
    ] = None
    parent_id: Annotated[str | None, Field(description="The snapshot this one was made from")] = (
        None
    )


class AttemptStep(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    at: str
    step: Annotated[str, Field(description="starting, environment, agent, collecting or verifying")]


class AuthInfo(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    csrf_header: str
    pat_header: str
    session_cookie: bool


class AuthorParameter(BaseModel):
    """
    Author parameter contract (03): defaults, requirement, constraints.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    default: str | None
    description: str
    name: str
    required: bool
    sensitive: bool
    used_in: list[str]


class BaselineDifference(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    path: str
    tasks_higher: Annotated[int, Field(description="Tasks whose author value is lower or unset")]
    tasks_lower: Annotated[int, Field(description="Tasks whose author value is higher")]
    value: int | float


class BatchBlocker(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    code: str
    left: int | None = None
    limit: int | None = None
    needed: int | None = None
    path: str


class BatchOperationResult(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    batch_id: str
    cancelled: int
    deleted_runs: Annotated[int, Field(description="Members in the recycle bin or purged")]
    error_trials: int
    finished: int
    interrupted: int
    planned_trials: int
    runs: int
    scored_trials: int
    terminal_runs: int


class BatchProgress(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    cancelled: int
    deleted_runs: Annotated[int, Field(description="Members in the recycle bin or purged")]
    error_trials: int
    finished: int
    interrupted: int
    planned_trials: int
    runs: int
    scored_trials: int
    terminal_runs: int


class CacheObservation(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    action: Literal["restore_on_run"] | None
    cache_state: Literal["ready", "cold"]
    task_key: str


class CapacityUse(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    cpus: float
    memory_mb: int
    slots: int


class CollectionRunLabel(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    label: str | None = None


class CollectionRunRef(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    label: str | None = None
    run_id: str


class CollectionRunsAdd(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    runs: list[CollectionRunRef]


class ComparisonCell(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    excluded_by_group: dict[str, int]
    excluded_trials: int
    failed_by_group: dict[str, int]
    failed_trials: Annotated[
        int, Field(description="Failures counted as failed (0 when higher is better)")
    ]
    mean: int | float | None
    passed_trials: int | None
    pending_trials: int
    planned_trials: int
    scored_trials: int
    scores: list[int | float | None]
    trial_ids: list[str]
    valid_trials: int


class ComparisonDifference(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    mean: int | float | None
    pass_rate: int | float | None


class ComparisonNotice(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    all: int | None = None
    code: str
    common: int | None = None
    dimension: (
        Literal[
            "dataset",
            "scoring",
            "model",
            "harness",
            "attachments",
            "resources",
            "repetitions",
            "parameters",
            "other",
        ]
        | None
    ) = None
    run_ids: list[str] | None = None


class ComparisonUsage(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    known_totals: dict[str, int | float]
    known_trials: int


class ConfigWarning(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    code: str
    message: str
    path: str


class ContentEvidence(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    message: str | None = None
    reason: str | None = None


class ContentResource(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    availability: Literal["unknown", "available", "unavailable"]
    checked_at: str | None
    digest: str
    resource_kind: Literal["task_content", "environment_image"]


class Credential(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    created_at: str
    created_by: str
    description: str
    enabled: bool
    id: str
    key_version: int
    kind: Literal["model_api_key", "hub_token", "generic_secret"]
    name: str
    provider: str
    revision: int
    updated_at: str


class CredentialCreate(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    description: str | None = ""
    kind: Literal["model_api_key", "hub_token", "generic_secret"]
    name: str
    provider: str
    value: str


class CredentialName(BaseModel):
    """
    A usable team credential as everyone sees it: the name, never the value.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    description: str
    id: str
    kind: str
    name: str
    provider: str


class CredentialRef(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    credential_id: str


class CredentialValue(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    expected_revision: int
    value: str


class DatasetImport(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    accept_changed_content: Annotated[
        bool | None,
        Field(description="Record a moved tag as a new version (CONTENT_REFERENCE_CHANGED)"),
    ] = False
    credential_id: str | None = None
    name: str
    namespace: str
    prepare: Annotated[
        bool | None, Field(description="Start the first preparation after the import")
    ] = True
    ref: Annotated[
        str,
        Field(description="Tag, revision or sha256 digest; always pinned to a digest"),
    ]
    source_kind: str | None = "hub"


class DatasetSource(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    available: bool
    kind: str


class DatasetVersion(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    admitted_task_count: int
    cache_ready_task_count: int
    created_at: str
    created_by: str | None
    dataset_id: str
    description: str
    id: str
    materialization_digest: str | None
    operation_id: str | None
    ref_kind: str
    resolved_digest: str
    status: Literal["ready"]
    submittable_task_count: int
    task_count: int
    upstream_ref: str


class DeletionState(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    deleted_at: str
    purged_at: str | None
    state: Annotated[
        Literal["recycled", "purged"],
        Field(description="recycled: in the recycle bin; purged: content gone, receipt kept"),
    ]


class EffectiveAttachment(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    content_digest: str
    effects: list[str]
    has_bootstrap: bool
    label: str | None
    name: str
    provides: AttachmentProvides
    version_id: Annotated[str, Field(description="The snapshot's id")]


class EffectiveDataset(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    name: str
    namespace: str
    resolved_digest: str
    source_kind: str
    version_id: str


class EffectiveHarness(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    agent: str
    content_digest: str
    name: str
    params: dict[str, str | int | float | bool]
    upstream_version: Annotated[
        str | None,
        Field(description="Program version installed in the task container, when any"),
    ]
    version: str
    version_id: str


class EffectiveOverrides(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    agent_timeout_sec: int | float | None = None
    cpus: int | None = None
    memory_mb: int | None = None
    verifier_timeout_sec: int | float | None = None


class EffectiveTask(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    agent_timeout_sec: Annotated[
        int | float | None, Field(description="null: the executor's default")
    ]
    content_digest: str
    cpus: int | float
    memory_mb: int
    spec_digest: str
    task_key: str
    verifier_timeout_sec: int | float | None


class EnabledChange(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    enabled: bool


class ExportCreate(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    artifact_kinds: list[str] | None = None
    include_artifacts: bool | None = True
    run_ids: list[str]
    task_keys: list[str] | None = None


class ExportOperationResult(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    artifact_bytes: int | None
    digest: str | None
    export_id: str
    files: int | None
    missing: int | None
    runs: int | None
    size_bytes: int | None
    warnings: int | None


class ExportProgress(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    files: int
    runs: int


class ExportRequest(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    artifact_kinds: list[str] | None
    include_artifacts: bool
    run_ids: list[str]
    task_keys: list[str] | None


class ExportSummary(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    artifact_bytes: int
    digest: str
    files: int
    missing: Annotated[int, Field(description="Files listed in the manifest's missing section")]
    runs: int
    size_bytes: Annotated[int, Field(description="Size of the package")]
    warnings: int


class FailureGroupCount(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    excluded: bool
    key: Literal[
        "provider_unavailable",
        "provider_access",
        "environment_setup",
        "preparation",
        "executor_unsupported",
        "platform_stop",
        "out_of_memory",
        "verifier",
        "attachment",
        "provider_request",
        "unclassified",
    ]
    side: Literal["external", "subject"]
    trials: int


class FieldErrorModel(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    code: str
    limit: int | float | None = None
    message: str | None = None
    path: str


class FieldSource(BaseModel):
    """
    Which layer gave a value: author, team policy, plan, collection, harness default or
    override.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    layer: Literal["author", "team", "plan", "collection", "override", "harness"]
    revision: int | None = None
    source_id: str | None = None


class FirstPreparation(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    task_ids: Annotated[
        list[str] | None,
        Field(description="Default: every task without a successful first preparation"),
    ] = None


class FirstPreparationProgress(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    admitted: int | None
    failed: int | None
    task_ids: list[str]
    tasks_done: int
    tasks_total: int


class HarnessAdapter(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    agent: str
    model_parameters: Annotated[
        list[str] | None,
        Field(description="Model configuration parameters it passes on; null means any"),
    ]
    package: Annotated[
        str | None,
        Field(description="npm package it installs at a harness version's upstream version"),
    ]
    parameters: Annotated[
        list[str] | None,
        Field(description="Parameters a harness version may declare; null means any"),
    ]
    protocols: list[Literal["openai-compatible", "openai", "anthropic"]]
    tested_version: Annotated[
        str | None,
        Field(description="Upstream version this Maestro was smoke-tested with"),
    ]


class HarnessParameterSpec(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    default: str | int | float | bool | None = None
    description: str | None = None
    enum: list[str | int | float | bool] | None = None
    maximum: int | float | None = None
    minimum: int | float | None = None
    type: Literal["string", "integer", "number", "boolean"]


class Health(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    accepting_runs: bool | None
    status: Literal["alive", "ready", "maintenance"]


class HostCapacityView(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    cpus: float
    memory_mb: int
    slots: int


class ImportRequest(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    accept_changed_content: bool
    credential_id: str | None
    name: str
    namespace: str
    prepare: bool
    ref: str
    source_kind: str


class ImportResult(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    dataset_version_id: str
    existing: Annotated[bool, Field(description="The same version was imported before")]
    preparation_operation_id: str | None = None


class Invitation(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    created_at: str
    created_by: str
    expires_at: str
    id: str
    kind: Literal["invitation", "password_reset"]
    note: str
    role: Literal["admin", "member"]
    state: Literal["pending", "used", "revoked", "expired"]
    target_user_id: str | None
    used_at: str | None
    used_by: str | None


class DisplayName(RootModel[str]):
    root: Annotated[str, Field(max_length=1024)]


class Username(RootModel[str]):
    root: Annotated[str, Field(max_length=128)]


class InvitationAccept(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    display_name: DisplayName | None = None
    password: Annotated[str, Field(max_length=256)]
    token: Annotated[str, Field(max_length=256)]
    username: Username | None = None


class InvitationCreate(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    note: str | None = ""
    role: Literal["admin", "member"] | None = "member"
    valid_hours: int | None = None


class InvitationInfo(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    expires_at: str
    kind: Literal["invitation", "password_reset"]
    role: Literal["admin", "member"]
    username: str | None


class InvitationLookup(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    token: Annotated[str, Field(max_length=256)]


class IssuedInvitation(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    created_at: str
    created_by: str
    expires_at: str
    id: str
    kind: Literal["invitation", "password_reset"]
    note: str
    role: Literal["admin", "member"]
    state: Literal["pending", "used", "revoked", "expired"]
    target_user_id: str | None
    token: Annotated[str, Field(description="Shown once")]
    used_at: str | None
    used_by: str | None


class IssuedPat(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    accepted_runs: int
    created_at: str
    expires_at: str
    id: str
    last_used_at: str | None
    name: str
    remaining_runs: int
    revision: int
    revoked_at: str | None
    run_limit: int
    scope: Literal["read", "experiment"]
    state: Literal["active", "revoked", "expired"]
    token: Annotated[str, Field(description="Shown once; store it securely")]
    token_prefix: str


class JsonValue(RootModel[Any]):
    root: Any


class LayoutSortInput(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    descending: bool | None = False
    key: str


class LayoutSortOutput(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    descending: bool
    key: str


class LoginRequest(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    password: Annotated[str, Field(max_length=256)]
    username: Annotated[str, Field(max_length=128)]


class MasterKeyRotation(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    expected_generation: int


class MasterKeyStatus(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    available: bool
    generation: int | None
    initialized: bool
    reencrypted_credentials: int | None
    rotated_at: str | None


class Me(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    auth_channel: Literal["web_session", "pat"]
    created_at: str
    disabled_at: str | None
    display_name: str
    id: str
    pat_id: str | None
    pat_scope: Literal["read", "experiment"] | None
    revision: int
    role: Literal["admin", "member"]
    status: Literal["active", "disabled"]
    username: str


class Meta(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    next_cursor: str | None = None
    request_id: str


class Mismatch(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    dimension: Literal[
        "dataset",
        "scoring",
        "model",
        "harness",
        "attachments",
        "resources",
        "repetitions",
        "parameters",
        "other",
    ]
    path: str


class ModelConfigCreate(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    credential_id: str | None = None
    description: str | None = ""
    endpoint: Annotated[str, Field(description="http(s) base URL, no credentials or query")]
    model_id: str
    name: str
    params: Annotated[
        dict[str, JsonValue] | None,
        Field(description="Provider request parameters; no secrets", validate_default=True),
    ] = {}
    protocol: Literal["openai-compatible", "openai", "anthropic"]
    provider: Annotated[
        str, Field(description="Provider name; concurrency limits apply per provider")
    ]


class ModelConfigRevise(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    credential_id: str | None = None
    description: str | None = ""
    endpoint: Annotated[str, Field(description="http(s) base URL, no credentials or query")]
    expected_revision: int
    model_id: str
    params: Annotated[
        dict[str, JsonValue] | None,
        Field(description="Provider request parameters; no secrets", validate_default=True),
    ] = {}
    protocol: Literal["openai-compatible", "openai", "anthropic"]
    provider: Annotated[
        str, Field(description="Provider name; concurrency limits apply per provider")
    ]


class ModelRevision(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    created_at: str
    credential_bound: bool
    credential_id: str | None
    endpoint: str
    model_id: str
    params: dict[str, JsonValue]
    protocol: Literal["openai-compatible", "openai", "anthropic"]
    provider: str
    revision: int


class NoProgress(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )


class NotCarried(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    mismatches: list[Mismatch]
    name: str
    run_id: str


class NoteBody(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    body: str


class ObjectRef(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    id: str


class OperationResource(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    id: str
    type: str


class Owner(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    deleted: Annotated[bool, Field(description="The account was deleted; history keeps its name")]
    display_name: str
    username: str


class ParameterChoice(BaseModel):
    """
    An author parameter value: a literal, or the team credential that holds it.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    credential_id: str | None = None
    value: str | None = None


class ParameterIssue(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    code: Literal["CONFLICTING_DEFAULTS", "COMPOSE_DISCOVERY_FAILED", "RESERVED_NAME"]
    file: Annotated[
        str | None, Field(description="The compose file (COMPOSE_DISCOVERY_FAILED)")
    ] = None
    name: Annotated[str | None, Field(description="The parameter")] = None
    used_in: Annotated[str | None, Field(description="RESERVED_NAME: where the task uses it")] = (
        None
    )


class PassRule(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    rule: Literal["threshold"]
    threshold: Annotated[
        int | float, Field(description="A trial passes when its score reaches this")
    ]


class PasswordChange(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    current_password: Annotated[str, Field(max_length=256)]
    new_password: Annotated[str, Field(max_length=256)]


class Pat(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    accepted_runs: int
    created_at: str
    expires_at: str
    id: str
    last_used_at: str | None
    name: str
    remaining_runs: int
    revision: int
    revoked_at: str | None
    run_limit: int
    scope: Literal["read", "experiment"]
    state: Literal["active", "revoked", "expired"]
    token_prefix: str


class PatCreate(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    expires_in_days: int | None = None
    name: str
    run_limit: int | None = None
    scope: Literal["read", "experiment"]


class PatQuota(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    left: int
    needed: int


class PlanArchive(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    archived: bool


class PlanCopy(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    name: str | None = None


class PlanUse(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    fields: Annotated[list[str], Field(description="JSON pointers of the fields the plan supplied")]
    plan_id: str
    revision: int


class PlannedRestore(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    action: Literal["restore_cache"]
    spec_digest: str
    task_id: str
    task_key: str


class PolicySetting(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    built_in: int
    description: str
    group: str
    max: int
    min: int
    name: str


class PreparationArchive(BaseModel):
    """
    The saved original the cache is restored from (U-17).
    """

    model_config = ConfigDict(
        extra="allow",
    )
    digest: str | None
    size_bytes: int | None


class PreparationPin(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    holder_id: str
    holder_kind: str


class PreparationWaiter(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    operation_id: str | None
    run_id: str | None
    since: str
    state: str


class PreparedOutputView(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    archive: Annotated[dict[str, JsonValue] | None, Field(description="The saved original (U-17)")]
    detail: Annotated[
        dict[str, JsonValue],
        Field(description="How it was prepared; depends on the environment"),
    ]
    image_id: str
    image_ref: str


class ProviderCapacity(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    in_use: int
    max_concurrent_trials: Annotated[int | None, Field(description="null means no platform limit")]
    provider: str


class ProviderLimit(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    max_concurrent_trials: Annotated[int | None, Field(description="null removes the limit")]


class PurgeRequest(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    confirm_object_id: Annotated[
        str, Field(description="Repeat the object id: purging is irreversible")
    ]


class RecycleEntry(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    deleted_at: str
    deleted_by: str
    deleted_by_username: str | None
    detail: Annotated[
        dict[str, JsonValue],
        Field(description="What a purge removed; depends on the object type"),
    ]
    id: str
    label: Annotated[str, Field(description="The object's name when it was deleted")]
    object_id: str
    object_type: Literal[
        "run",
        "artifact",
        "dataset",
        "dataset_version",
        "credential",
        "user",
        "pat",
        "model_config",
        "harness_version",
        "attachment_version",
        "experiment_plan",
        "collection",
        "analysis_snapshot",
    ]
    owner_id: str | None
    owner_username: str | None
    purged_at: str | None
    purged_by: str | None
    state: Literal["recycled", "purged", "restored"]


class RequiredPreparation(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    action: Literal["restore_cache"]
    spec_digest: str


class RerunSelection(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    mode: Annotated[
        Literal["all", "unfinished", "explicit"] | None,
        Field(
            description="unfinished: every task with a trial not scored (the source must have ended); explicit: task_ids of the source run"
        ),
    ] = "all"
    task_ids: list[str] | None = None


class ResourceOverrides(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    cpus: int | None = None
    memory_mb: int | None = None


class RestoreProgress(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    spec_digest: str
    task_key: str
    waiters: int | None


class RestoreResult(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    output: PreparedOutputView
    waiters: int


class RevisionRef(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    id: str
    revision: int


class RunCounts(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    active: int
    cancelled: int
    errors: int
    interrupted: int
    not_started: int
    planned: int
    queued: int
    scored: int
    terminal: int


class RunOperationResult(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    counts: RunCounts
    run_id: str
    state: Literal[
        "queued", "preparing", "running", "finalizing", "finished", "cancelled", "interrupted"
    ]


class RunProgress(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    active: int
    errors: int
    planned: int
    scored: int
    terminal: int


class RunRerun(BaseModel):
    """
    A new run from the source run's snapshot; only the task set may shrink.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    client_request_id: str | None = None
    name: str | None = None
    task_selection: Annotated[RerunSelection | None, Field(validate_default=True)] = {"mode": "all"}


class RunScoring(BaseModel):
    """
    Run-list subset of the summary: a mean is never shown without its coverage.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    bench_score: Annotated[float | None, Field(description="Macro mean of per-task means")]
    excluded_error_trials: int
    mean: Annotated[
        float | None,
        Field(description="Mean of valid scores only; null with no sample"),
    ]
    metric_id: str
    pass_rate: float | None
    passed_trials: int | None
    planned_trials: int
    scored_coverage: Annotated[float | None, Field(description="scored_trials / planned_trials")]
    scored_trials: int
    stddev: Annotated[
        float | None,
        Field(description="Sample standard deviation; null below 2 samples"),
    ]
    unfinished_trials: int


class SchemaState(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    compatible: bool
    current: str | None
    head: str


class ScoringContract(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    bench_aggregation: str
    contract_version: int
    count_as_failure: list[str]
    direction: Literal["higher_is_better", "lower_is_better"]
    metric_id: str
    name: str
    passed: Annotated[PassRule | None, Field(description="Explicit pass rule; null: no pass rate")]
    source: Literal["platform_default", "dataset_version"]
    task_aggregation: str
    unit: str | None


class ScoringContractInput(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    direction: Literal["higher_is_better", "lower_is_better"]
    metric_id: str | None = "reward"
    name: str | None = "Reward"
    pass_threshold: Annotated[
        float | None,
        Field(description="A trial passes when its score reaches this value; null: no pass rate"),
    ]
    unit: str | None = None


class SessionInfo(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    csrf_token: Annotated[
        str | None,
        Field(description="Send as X-CSRF-Token on unsafe requests authenticated by the cookie"),
    ]
    user: Me


class SetAsideRun(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    name: str
    reason: Literal["deleted"]
    run_id: str


class SetupNetwork(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    created_at: str | None
    created_by_username: str | None
    env: Annotated[
        dict[str, str],
        Field(
            description="Variables for setup in the task container (package mirrors); no secrets"
        ),
    ]
    harness_setup_timeout_sec: Annotated[
        int | None,
        Field(description="The harness's setup time limit; none keeps the harness's own"),
    ]
    no_proxy: Annotated[str, Field(description="Comma-separated, no spaces")]
    note: str
    proxy: Annotated[
        str,
        Field(
            description="http(s)://host:port for setup work, reachable from containers; empty is off"
        ),
    ]
    revision: Annotated[int, Field(description="0 is nothing set")]
    warnings: Annotated[list[str], Field(description="Valid but probably not what was meant")]


class SetupNetworkUpdate(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    env: Annotated[
        dict[str, str] | None,
        Field(
            description="Variables for setup in the task container (package mirrors); no secrets"
        ),
    ] = None
    expected_revision: int
    harness_setup_timeout_sec: Annotated[
        int | None,
        Field(description="The harness's setup time limit; none keeps the harness's own"),
    ] = None
    no_proxy: Annotated[str | None, Field(description="Comma-separated, no spaces")] = ""
    note: str | None = ""
    proxy: Annotated[
        str | None,
        Field(
            description="http(s)://host:port for setup work, reachable from containers; empty is off"
        ),
    ] = ""


class SetupRequest(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    display_name: DisplayName | None = None
    password: Annotated[str, Field(max_length=256)]
    setup_token: Annotated[str, Field(max_length=256)]
    username: Annotated[str, Field(max_length=128)]


class SetupStatus(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    needs_setup: bool


class SnapshotNote(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    author_id: str
    author_username: str | None
    body: str
    created_at: str
    editable: bool
    id: str
    pat_id: str | None
    updated_at: str


class SnapshotSpec(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    fixed: dict[str, dict[str, JsonValue]]
    scenario: Literal["models", "harnesses", "model_harness", "attachments", "repeats", "custom"]
    variable_dimensions: list[
        Literal["model", "harness", "attachments", "resources", "repetitions", "parameters"]
    ]


class SnapshotSummary(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    allowed_actions: list[str]
    collection_id: str | None
    created_at: str
    id: str
    name: str
    owner_id: str
    owner_username: str | None
    pat_id: str | None
    run_count: int


class SourceDataset(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    name: str
    namespace: str
    visibility: str | None


class SourceTask(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    content_digest: str
    task_key: str


class SourceVersion(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    published_at: str | None
    ref: str
    resolved_digest: str
    revision: int | None
    tags: list[str]
    yanked: bool
    yanked_reason: str | None


class SourceVersionDetail(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    description: str
    existing_version_id: str | None
    name: str
    namespace: str
    ref_kind: str
    resolved_digest: str
    tasks: list[SourceTask]
    upstream_ref: str


class StatusChange(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    expected_revision: int


class StorageThreshold(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    gib: int | float
    percent: int | float


class SuggestedAction(BaseModel):
    """
    A next step; ``type`` says which, and the other fields depend on it.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    type: str


class TaskAdmission(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    approved_at: str | None
    id: str
    revision: int
    state: Literal["pending", "admitted", "first_failed", "revoked"]


class TaskContent(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    availability: Literal["unknown", "available", "unavailable"]
    checked_at: str | None
    digest: str
    material_digest: str | None
    resources: list[ContentResource]


class TaskResources(BaseModel):
    """
    What the task's author asks for; a run may raise CPU and memory, never lower them.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    constraint_strength: str | None
    cpus: int | float | None
    memory_mb: int | None
    storage_mb: int | None


class TaskSelectionInput(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    mode: Literal["all_eligible", "explicit"] | None = "all_eligible"
    task_ids: list[str] | None = None


class TaskSelectionOutput(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    mode: Literal["all_eligible", "explicit"]
    task_ids: list[str] | None


class TaskSummary(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    excluded_trials: int
    failure_causes: list[str]
    mean: float | None
    partial: Annotated[bool, Field(description="Some planned repetitions have no valid score")]
    passed_trials: int | None
    planned_trials: int
    scored_trials: int
    stddev: float | None
    task_key: str


class TaskTimeouts(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    agent_sec: int | float | None
    build_sec: int | float | None
    verifier_sec: int | float | None


class TaskWarning(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    action: str
    code: Literal["PREPARATION_CACHE_MISS"]


class TeamPolicy(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    revision: int
    settings: list[PolicySetting]
    values: dict[str, int]


class TeamPolicyRevision(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    created_at: str
    created_by: str
    created_by_username: str
    revision: int
    values: dict[str, int]


class TeamPolicyUpdate(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    expected_revision: int
    values: dict[str, int]


class TimeoutOverrides(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    agent_sec: float | None = None
    bootstrap_sec: Annotated[
        float | None,
        Field(description="Time for each trial to install all attachments (team default)"),
    ] = None
    verifier_sec: float | None = None


class TrialResult(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    attempt_id: str
    exception_type: str | None
    failure_cause: str | None
    policy_version: str
    raw_score: Annotated[float | None, Field(description="Verifier output; null when none")]
    reward_valid: bool
    score: Annotated[
        float | None,
        Field(description="Counted score; 0 is a real zero, null is unscored"),
    ]
    verifier_status: str | None


class UpstreamRelease(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    published_at: str | None
    version: str


class UpstreamTag(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    published_at: str | None
    tag: str
    version: str


class UpstreamVersions(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    agent: str
    checked_at: str | None
    error: Annotated[str | None, Field(description="Why the registry could not be asked")]
    package: str
    recent: Annotated[list[UpstreamRelease], Field(description="Newest releases first")]
    registry: Annotated[
        str | None, Field(description="Registry asked (the setup network's npm mirror)")
    ]
    tags: Annotated[list[UpstreamTag], Field(description="Release tags (stable, latest)")]
    tested_version: str | None


class UsageSummary(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    known_totals: dict[str, float]
    known_trials: int
    unknown_trials: Annotated[int, Field(description="Started trials that reported no usage")]


class User(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    created_at: str
    disabled_at: str | None
    display_name: str
    id: str
    revision: int
    role: Literal["admin", "member"]
    status: Literal["active", "disabled"]
    username: str


class Attachment(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    compatible_harnesses: Annotated[
        list[str], Field(description='Harness agents, or ["*"] for any')
    ]
    content_digest: Annotated[
        str,
        Field(description="Identifies the snapshot: every file's path, SHA-256 and mode"),
    ]
    created_at: str
    deletion: DeletionState | None
    description: str
    effects: list[str]
    enabled: bool
    file_count: int
    files: list[AttachmentFile] | None
    has_bootstrap: Annotated[bool, Field(description="Has bootstrap.sh, run in each trial's setup")]
    id: str
    label: str | None
    name: str
    owner_id: str
    owner_username: str | None
    parent_id: str | None
    provides: AttachmentProvides
    size_bytes: int


class AttachmentDiff(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    added: list[AttachmentFile]
    attachment_id: str
    base_id: Annotated[
        str | None, Field(description="Compared with; unset when there is no parent")
    ]
    changed: list[AttachmentFileChange]
    removed: list[AttachmentFile]


class AttemptError(BaseModel):
    """
    Why an attempt did not produce a score, as the platform error policy classified it.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    code: str
    detail: dict[str, JsonValue] | None
    exception_type: str | None
    failure_cause: str | None
    grade_requested: bool | None
    message: str
    policy_version: str
    retry_recommended: bool | None
    reward_valid: bool | None
    stage: str


class AuditEvent(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    action: str
    actor_user_id: str | None
    auth_channel: str | None
    created_at: str
    detail: Annotated[dict[str, JsonValue] | None, Field(description="Depends on the action")]
    id: str
    object_id: str | None
    object_type: str | None
    outcome: str
    pat_id: str | None
    request_id: str | None


class CapacityOverview(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    host: HostCapacityView
    in_use: CapacityUse
    providers: list[ProviderCapacity]


class CollectionCandidate(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    accepted_at: str
    harness: str
    mismatches: Annotated[list[Mismatch], Field(description="Empty when the run can be added")]
    model: str
    name: str
    owner_username: str
    relation: str
    run_id: str
    state: Literal[
        "queued", "preparing", "running", "finalizing", "finished", "cancelled", "interrupted"
    ]


class CollectionEvent(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    action: str
    actor_username: str | None
    at: str
    detail: dict[str, JsonValue]
    id: str
    pat_id: str | None
    run_id: str | None


class CollectionMember(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    added_at: str
    added_by_username: str | None
    deletion: DeletionState | None
    label: str | None
    run_id: str
    run_name: str
    run_state: str


class ComparedRunRow(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    accepted_at: str
    actual_models: list[str]
    batch_id: str | None
    compared_trials: Annotated[int, Field(description="Planned trials of the compared tasks")]
    difference: Annotated[
        ComparisonDifference | None,
        Field(
            description="To the baseline; only for the same dataset content and scoring over common tasks"
        ),
    ]
    evidence: Literal["available", "none", "deleted", "purged"]
    excluded_by_group: dict[str, int]
    excluded_trials: int
    failed_by_group: dict[str, int]
    failed_trials: int
    finished_at: str | None
    label: str | None
    mean: Annotated[
        int | float | None,
        Field(
            description="Macro mean of compared task means; original score mean when incomparable"
        ),
    ]
    name: str
    owner_username: str | None
    pass_rate: int | float | None
    passed_trials: int | None
    pending_trials: int
    planned_trials: int
    relation: str
    run_id: str
    scored_trials: int
    source_run_id: str | None
    state: str
    stddev: Annotated[
        int | float | None,
        Field(
            description="Sample standard deviation of task means; original scores when incomparable"
        ),
    ]
    task_count: int
    terminal: bool
    title: Annotated[str, Field(description="The run's values of the varying dimensions")]
    usage: ComparisonUsage
    valid_trials: Annotated[
        int, Field(description="Scored plus counted-as-failed, in compared tasks")
    ]


class ComparedTask(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    cells: dict[str, ComparisonCell | None]
    status: Literal["compared", "no_valid_trials", "not_common"]
    task_key: str


class ComparisonDimension(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    key: Literal[
        "dataset",
        "scoring",
        "model",
        "harness",
        "attachments",
        "resources",
        "repetitions",
        "parameters",
        "other",
    ]
    labels: Annotated[
        dict[str, str], Field(description="A short name of each run's value, by run id")
    ]
    paths: Annotated[
        list[str],
        Field(description="JSON pointers where some run differs from the first"),
    ]
    values: Annotated[
        dict[str, dict[str, JsonValue]],
        Field(description="Complete comparison values by run id; empty in version 1 snapshots"),
    ]
    varies: bool


class ConfigDifference(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    after: JsonValue
    before: JsonValue
    hidden: Annotated[
        bool | None,
        Field(description="true: the values are not shown to you (U-27); otherwise absent"),
    ] = None
    path: Annotated[str, Field(description="JSON pointer into the effective configuration")]


class ConfigRequestInput(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    attachments: Annotated[
        list[str] | None,
        Field(description="Attachment snapshot ids in load order (model runs only)"),
    ] = []
    collection_id: Annotated[
        str | None,
        Field(
            description="Join this collection: its fixed fields fill in what is left out, and the run must match them"
        ),
    ] = None
    concurrency: int | None = None
    dataset_version_id: Annotated[
        str | None, Field(description="Required unless the plan has it")
    ] = None
    execution_mode: Annotated[
        Literal["agent", "oracle"] | None,
        Field(
            description="agent: a model through a harness; oracle: each task's reference solution, a test mode never compared with model results. Required unless the plan has it"
        ),
    ] = None
    harness_params: dict[str, str | int | float | bool] | None = {}
    harness_version_id: str | None = None
    model_config_id: str | None = None
    model_revision: Annotated[
        int | None,
        Field(description="An earlier revision of the model configuration; unset: current"),
    ] = None
    parameters: Annotated[
        dict[str, str | CredentialRef] | None,
        Field(
            description="Author parameter values; secret ones as a credential reference",
            validate_default=True,
        ),
    ] = {}
    plan_id: Annotated[
        str | None,
        Field(description="An experiment plan; fields set here win, the plan fills in the rest"),
    ] = None
    plan_revision: Annotated[
        int | None,
        Field(description="Refuse unless the plan is still at this revision"),
    ] = None
    repetitions: int | None = None
    resources: Annotated[ResourceOverrides | None, Field(validate_default=True)] = {}
    run_deadline_hours: int | None = None
    task_selection: Annotated[TaskSelectionInput | None, Field(validate_default=True)] = {
        "mode": "all_eligible"
    }
    timeouts: Annotated[TimeoutOverrides | None, Field(validate_default=True)] = {}


class ConfigRequestOutput(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    attachments: Annotated[
        list[str],
        Field(description="Attachment snapshot ids in load order (model runs only)"),
    ]
    collection_id: Annotated[
        str | None,
        Field(
            description="Join this collection: its fixed fields fill in what is left out, and the run must match them"
        ),
    ]
    concurrency: int | None
    dataset_version_id: Annotated[str | None, Field(description="Required unless the plan has it")]
    execution_mode: Annotated[
        Literal["agent", "oracle"] | None,
        Field(
            description="agent: a model through a harness; oracle: each task's reference solution, a test mode never compared with model results. Required unless the plan has it"
        ),
    ]
    harness_params: dict[str, str | int | float | bool]
    harness_version_id: str | None
    model_config_id: str | None
    model_revision: Annotated[
        int | None,
        Field(description="An earlier revision of the model configuration; unset: current"),
    ]
    parameters: Annotated[
        dict[str, str | CredentialRef],
        Field(description="Author parameter values; secret ones as a credential reference"),
    ]
    plan_id: Annotated[
        str | None,
        Field(description="An experiment plan; fields set here win, the plan fills in the rest"),
    ]
    plan_revision: Annotated[
        int | None,
        Field(description="Refuse unless the plan is still at this revision"),
    ]
    repetitions: int | None
    resources: ResourceOverrides
    run_deadline_hours: int | None
    task_selection: TaskSelectionOutput
    timeouts: TimeoutOverrides


class Dataset(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    credential_id: str | None
    description: str
    id: str
    name: str
    namespace: str
    source_kind: str
    versions: list[DatasetVersion]


class DatasetVersionDetail(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    admitted_task_count: int
    cache_ready_task_count: int
    created_at: str
    created_by: str | None
    dataset_id: str
    deletion: Annotated[
        DeletionState | None,
        Field(description="Set when the version or its dataset was deleted; it still resolves"),
    ]
    description: str
    first_preparation_operation_id: str | None
    id: str
    materialization_digest: str | None
    name: str
    namespace: str
    operation_id: str | None
    ref_kind: str
    resolved_digest: str
    scoring_contract: Annotated[
        ScoringContract,
        Field(
            description="Contract new runs freeze; source is dataset_version or platform_default"
        ),
    ]
    status: Literal["ready"]
    submittable_task_count: int
    task_count: int
    upstream_ref: str


class DeploymentSettings(BaseModel):
    """
    What ``config.toml`` and the service environment set; read only on the web.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    retired: Annotated[list[str], Field(description="Keys in config.toml that are no longer used")]
    service_proxy: Annotated[
        bool, Field(description="The service process has a proxy in its environment")
    ]
    values: Annotated[
        dict[str, JsonValue],
        Field(description="config.toml settings in force, with defaults"),
    ]


class EffectiveModel(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    config_id: str
    credential_id: Annotated[
        str | None,
        Field(
            description="The bound team credential; null: none. Left out for everyone but administrators' web sessions (U-27)"
        ),
    ] = None
    endpoint: str
    model_id: str
    params: Annotated[
        dict[str, JsonValue],
        Field(description="Provider request parameters, as configured"),
    ]
    protocol: Literal["openai-compatible", "openai", "anthropic"]
    provider: str
    revision: int


class EnvelopeAdmissionRevoked(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: AdmissionRevoked
    meta: Meta


class EnvelopeAttachmentDiff(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: AttachmentDiff
    meta: Meta


class EnvelopeAttachmentMissing(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: AttachmentMissing
    meta: Meta


class EnvelopeAttachment(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: Attachment
    meta: Meta


class EnvelopeCapacityOverview(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: CapacityOverview
    meta: Meta


class EnvelopeConfigRequest(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: ConfigRequestOutput
    meta: Meta


class EnvelopeCredential(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: Credential
    meta: Meta


class EnvelopeDatasetVersionDetail(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: DatasetVersionDetail
    meta: Meta


class EnvelopeDeploymentSettings(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: DeploymentSettings
    meta: Meta


class EnvelopeInvitationInfo(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: InvitationInfo
    meta: Meta


class EnvelopeInvitation(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: Invitation
    meta: Meta


class EnvelopeIssuedInvitation(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: IssuedInvitation
    meta: Meta


class EnvelopeIssuedPat(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: IssuedPat
    meta: Meta


class EnvelopeMasterKeyStatus(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: MasterKeyStatus
    meta: Meta


class EnvelopeMe(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: Me
    meta: Meta


class EnvelopePat(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: Pat
    meta: Meta


class EnvelopeRecycleEntry(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: RecycleEntry
    meta: Meta


class EnvelopeScoringContract(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: ScoringContract
    meta: Meta


class EnvelopeSessionInfo(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: SessionInfo
    meta: Meta


class EnvelopeSetupNetwork(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: SetupNetwork
    meta: Meta


class EnvelopeSetupStatus(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: SetupStatus
    meta: Meta


class EnvelopeSourceVersionDetail(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: SourceVersionDetail
    meta: Meta


class EnvelopeTeamPolicy(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: TeamPolicy
    meta: Meta


class EnvelopeUpstreamVersions(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: UpstreamVersions
    meta: Meta


class EnvelopeUser(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: User
    meta: Meta


class EnvelopeListArtifact(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[Artifact]
    meta: Meta


class EnvelopeListAttachment(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[Attachment]
    meta: Meta


class EnvelopeListAuditEvent(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[AuditEvent]
    meta: Meta


class EnvelopeListCollectionCandidate(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[CollectionCandidate]
    meta: Meta


class EnvelopeListCollectionEvent(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[CollectionEvent]
    meta: Meta


class EnvelopeListCredentialName(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[CredentialName]
    meta: Meta


class EnvelopeListCredential(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[Credential]
    meta: Meta


class EnvelopeListDatasetSource(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[DatasetSource]
    meta: Meta


class EnvelopeListDataset(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[Dataset]
    meta: Meta


class EnvelopeListHarnessAdapter(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[HarnessAdapter]
    meta: Meta


class EnvelopeListInvitation(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[Invitation]
    meta: Meta


class EnvelopeListPat(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[Pat]
    meta: Meta


class EnvelopeListRecycleEntry(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[RecycleEntry]
    meta: Meta


class EnvelopeListSetupNetwork(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[SetupNetwork]
    meta: Meta


class EnvelopeListSnapshotSummary(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[SnapshotSummary]
    meta: Meta


class EnvelopeListSourceDataset(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[SourceDataset]
    meta: Meta


class EnvelopeListSourceVersion(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[SourceVersion]
    meta: Meta


class EnvelopeListTeamPolicyRevision(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[TeamPolicyRevision]
    meta: Meta


class EnvelopeListUser(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[User]
    meta: Meta


class ErrorBody(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    code: str
    field_errors: Annotated[list[FieldErrorModel] | None, Field(validate_default=True)] = []
    message: str
    retryable: bool | None = False
    stage: str | None = None
    suggested_actions: Annotated[list[SuggestedAction] | None, Field(validate_default=True)] = []


class ErrorResponse(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    error: ErrorBody
    meta: Meta


class ExecutionInfo(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    capacity: HostCapacityView
    executor: str | None
    harness_adapters: list[HarnessAdapter]
    modes: list[Literal["agent", "oracle"]]
    policy_version: str


class HarnessParameterSchemaInput(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    parameters: Annotated[dict[str, HarnessParameterSpec] | None, Field(validate_default=True)] = {}


class HarnessParameterSchemaOutput(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    parameters: dict[str, HarnessParameterSpec]


class ImportProgress(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    request: ImportRequest
    resolved_digest: str | None
    tasks_done: int | None
    tasks_total: int | None


class LayoutInput(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    columns: (
        Literal[
            "dataset",
            "scoring",
            "model",
            "harness",
            "attachments",
            "resources",
            "repetitions",
            "parameters",
            "other",
        ]
        | None
    ) = None
    mode: Annotated[
        Literal["leaderboard", "matrix"] | None,
        Field(
            description="leaderboard: a row per run; matrix: two varying dimensions as rows and columns"
        ),
    ] = "leaderboard"
    rows: (
        Literal[
            "dataset",
            "scoring",
            "model",
            "harness",
            "attachments",
            "resources",
            "repetitions",
            "parameters",
            "other",
        ]
        | None
    ) = None
    sort: LayoutSortInput | None = None


class LayoutOutput(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    columns: (
        Literal[
            "dataset",
            "scoring",
            "model",
            "harness",
            "attachments",
            "resources",
            "repetitions",
            "parameters",
            "other",
        ]
        | None
    )
    mode: Annotated[
        Literal["leaderboard", "matrix"],
        Field(
            description="leaderboard: a row per run; matrix: two varying dimensions as rows and columns"
        ),
    ]
    rows: (
        Literal[
            "dataset",
            "scoring",
            "model",
            "harness",
            "attachments",
            "resources",
            "repetitions",
            "parameters",
            "other",
        ]
        | None
    )
    sort: LayoutSortOutput | None


class ModelConfig(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    created_at: str
    credential_bound: bool
    credential_id: Annotated[str | None, Field(description="Administrators only")]
    credential_name: Annotated[str | None, Field(description="Administrators only")]
    credential_problem: str | None
    deletion: DeletionState | None
    description: str
    enabled: bool
    endpoint: str
    id: str
    model_id: str
    name: str
    params: dict[str, JsonValue]
    protocol: Literal["openai-compatible", "openai", "anthropic"]
    provider: str
    revision: int
    revisions: list[ModelRevision] | None
    updated_at: str


class NormalizedRequest(BaseModel):
    """
    A configuration request after checking: what resolution and reruns start from.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    attachments: list[str]
    concurrency: int | None
    dataset_version_id: str
    execution_mode: Literal["agent", "oracle"]
    harness_params: dict[str, str | int | float | bool]
    harness_version_id: str | None
    model_config_id: str | None
    model_revision: int | None
    parameters: dict[str, ParameterChoice]
    plan: PlanUse | None
    repetitions: int | None
    resources: ResourceOverrides
    run_deadline_hours: int | None
    task_selection: TaskSelectionOutput
    timeouts: TimeoutOverrides


class PlanTemplateInput(BaseModel):
    """
    Any fields of a configuration request (see ConfigRequest). A field the plan does
    not set is left out; a run's own value wins over the plan's.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    attachments: list[str] | None = None
    concurrency: int | None = None
    dataset_version_id: str | None = None
    execution_mode: Literal["agent", "oracle"] | None = None
    harness_params: dict[str, str | int | float | bool] | None = None
    harness_version_id: str | None = None
    model_config_id: str | None = None
    model_revision: int | None = None
    parameters: dict[str, str | CredentialRef] | None = None
    repetitions: int | None = None
    resources: ResourceOverrides | None = None
    run_deadline_hours: int | None = None
    task_selection: TaskSelectionInput | None = None
    timeouts: TimeoutOverrides | None = None


class PlanTemplateOutput(BaseModel):
    """
    Any fields of a configuration request (see ConfigRequest). A field the plan does
    not set is left out; a run's own value wins over the plan's.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    attachments: list[str] | None = None
    concurrency: int | None = None
    dataset_version_id: str | None = None
    execution_mode: Literal["agent", "oracle"] | None = None
    harness_params: dict[str, str | int | float | bool] | None = None
    harness_version_id: str | None = None
    model_config_id: str | None = None
    model_revision: int | None = None
    parameters: dict[str, str | CredentialRef] | None = None
    repetitions: int | None = None
    resources: ResourceOverrides | None = None
    run_deadline_hours: int | None = None
    task_selection: TaskSelectionOutput | None = None
    timeouts: TimeoutOverrides | None = None


class PlanUpdate(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    description: str | None = None
    expected_revision: int
    name: str | None = None
    template: PlanTemplateInput | None = None


class Problem(BaseModel):
    """
    A recorded failure: a stable code and a message, with the cause's kind, evidence
    and next steps when it has them. Further fields depend on the code.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    code: str
    evidence: dict[str, JsonValue] | None = None
    kind: str | None = None
    message: str | None = None
    suggested_actions: list[SuggestedAction] | None = None


class ReferenceRevisions(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    attachments: list[str] | None = None
    collection: ObjectRef | None = None
    harness_version: str | None = None
    model_config_: Annotated[RevisionRef | None, Field(alias="model_config")] = None
    plan: RevisionRef | None = None
    team_policy: int | None = None


class RestoreOperation(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    allowed_actions: list[str]
    created_at: str
    error: Problem | None
    finished_at: str | None
    id: str
    kind: Literal["preparation_restore"]
    links: dict[str, str]
    owner_id: str | None
    phase: str | None
    progress: RestoreProgress
    resource: OperationResource | None
    result: RestoreResult | None
    revision: int
    status: Literal["accepted", "running", "succeeded", "failed", "cancelled", "interrupted"]
    updated_at: str


class Run(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    accepted_at: str
    allowed_actions: list[str]
    artifact_state: Literal["pending", "saving", "complete", "partial", "failed", "expired"]
    batch_id: str | None
    cleanup_state: Literal["pending", "running", "complete", "failed", "not_required"]
    client_request_id: str | None
    counts: RunCounts
    dataset_version_deletion: DeletionState | None
    dataset_version_id: str
    deadline_at: str
    deletion: Annotated[
        DeletionState | None,
        Field(description="Set when the run is in the recycle bin"),
    ]
    finished_at: str | None
    heartbeat_at: Annotated[
        str | None, Field(description="Last sign of life from the run's Runner")
    ]
    id: str
    links: dict[str, str]
    name: str
    operation_id: str
    owner: Owner
    owner_id: str
    pat_id: str | None
    plan_id: str | None
    plan_revision: int | None
    queue_reason: Annotated[
        str | None,
        Field(
            description="Why a queued run waits: CAPACITY_SLOTS, CAPACITY_CPU, CAPACITY_MEMORY or PROVIDER_LIMIT"
        ),
    ]
    relation: Literal["initial", "rerun", "modified"]
    result_completeness: Literal["none", "partial", "complete"]
    scoring: RunScoring | None
    source_run_id: str | None
    started_at: str | None
    state: Literal[
        "queued", "preparing", "running", "finalizing", "finished", "cancelled", "interrupted"
    ]
    stop_reason: str | None
    stop_requested: bool


class RunCreate(BaseModel):
    """
    Either ``resolution_id`` of a resolution made before, or the configuration inline.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    attachments: Annotated[
        list[str] | None,
        Field(description="Attachment snapshot ids in load order (model runs only)"),
    ] = []
    client_request_id: str | None = None
    collection_id: Annotated[
        str | None,
        Field(
            description="Join this collection: its fixed fields fill in what is left out, and the run must match them"
        ),
    ] = None
    concurrency: int | None = None
    dataset_version_id: str | None = None
    execution_mode: Literal["agent", "oracle"] | None = None
    harness_params: dict[str, str | int | float | bool] | None = {}
    harness_version_id: str | None = None
    model_config_id: str | None = None
    model_revision: Annotated[
        int | None,
        Field(description="An earlier revision of the model configuration; unset: current"),
    ] = None
    name: str
    parameters: Annotated[
        dict[str, str | CredentialRef] | None,
        Field(
            description="Author parameter values; secret ones as a credential reference",
            validate_default=True,
        ),
    ] = {}
    plan_id: Annotated[
        str | None,
        Field(description="An experiment plan; fields set here win, the plan fills in the rest"),
    ] = None
    plan_revision: Annotated[
        int | None,
        Field(description="Refuse unless the plan is still at this revision"),
    ] = None
    repetitions: int | None = None
    resolution_id: str | None = None
    resources: Annotated[ResourceOverrides | None, Field(validate_default=True)] = {}
    run_deadline_hours: int | None = None
    source_run_id: Annotated[
        str | None,
        Field(
            description="Makes this a modified run of that run: its differences are recorded (start from GET /runs/{run_id}/request)"
        ),
    ] = None
    task_selection: Annotated[TaskSelectionInput | None, Field(validate_default=True)] = {
        "mode": "all_eligible"
    }
    timeouts: Annotated[TimeoutOverrides | None, Field(validate_default=True)] = {}


class RunOperation(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    allowed_actions: list[str]
    created_at: str
    error: Problem | None
    finished_at: str | None
    id: str
    kind: Literal["run"]
    links: dict[str, str]
    owner_id: str | None
    phase: str | None
    progress: RunProgress
    resource: OperationResource | None
    result: RunOperationResult | None
    revision: int
    status: Literal["accepted", "running", "succeeded", "failed", "cancelled", "interrupted"]
    updated_at: str


class RunPolicy(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    aggregation_version: int
    executor_id: str | None
    policy_version: str
    scoring: ScoringContract
    team_policy_revision: int


class RunSource(BaseModel):
    """
    Where a rerun or modified run came from (RUN-06).
    """

    model_config = ConfigDict(
        extra="allow",
    )
    config_digest: str
    differences: list[ConfigDifference] | None = None
    relation: Literal["rerun", "modified"]
    run_id: str
    snapshot_digest: str
    source_field_sources: dict[str, FieldSource] | None = None
    task_causes: Annotated[
        dict[str, list[str]] | None,
        Field(description="Rerun: why each chosen task of the source was not scored"),
    ] = None


class RunSummary(BaseModel):
    """
    06 section 5. At a terminal run planned = scored + count_as_failure + excluded +
    unfinished; while running, active and queued make up the rest.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    active_trials: int
    attempts: int
    bench_score: float | None
    bench_scored_tasks: int
    bench_task_coverage: Annotated[
        float | None, Field(description="Run tasks / tasks in the version")
    ]
    contract: ScoringContract
    count_as_failure_trials: int
    evaluation_coverage: float | None
    excluded_by_cause: dict[str, int]
    excluded_error_trials: int
    mean: float | None
    pass_rate: float | None
    passed_trials: int | None
    planned_trials: int
    queued_trials: int
    result_completeness: Literal["none", "partial", "complete"]
    retried_trials: int
    retry_count: Annotated[
        int, Field(description="Extra attempts beyond the first, over all trials")
    ]
    run_id: str
    scored_coverage: float | None
    scored_trials: int
    stddev: float | None
    task_count: int
    tasks: list[TaskSummary]
    unfinished_trials: int
    usage: UsageSummary


class StorageStatus(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    accept_min_free: StorageThreshold
    dispatch_min_free: StorageThreshold
    free_bytes: int
    level: Literal["ok", "accept_paused", "dispatch_paused"]
    total_bytes: int


class SystemDetail(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    executor: str | None
    master_key_available: bool
    needs_setup: bool | None
    schema_: Annotated[SchemaState, Field(alias="schema")]
    storage: StorageStatus
    worker_running: bool


class SystemStatus(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    accepting_runs: bool | None
    detail: SystemDetail
    status: Literal["alive", "ready", "maintenance"]


class TaskBlocker(BaseModel):
    """
    Why a task cannot be chosen; the fields that apply depend on the code.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    admission_state: str | None = None
    code: Literal[
        "TASK_NOT_ADMITTED",
        "TASK_ADMISSION_REVOKED",
        "RESOURCE_CONTENT_UNAVAILABLE",
        "CONFIG_REQUIRED_MISSING",
        "AUTHOR_PARAMETERS_UNRESOLVED",
        "TEAM_LIMIT_EXCEEDED",
        "NOT_FOUND",
    ]
    evidence: ContentEvidence | None = None
    expected_digest: str | None = None
    field: Annotated[str | None, Field(description="TEAM_LIMIT_EXCEEDED: the task setting")] = None
    first_preparation_error: Problem | None = None
    issues: list[ParameterIssue] | None = None
    limit: int | float | None = None
    parameters: list[str] | None = None
    resource_kind: str | None = None
    value: int | float | None = None


class Attempt(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    attempt_index: int
    ended_at: str | None
    error: AttemptError | None
    executor_ref: Annotated[
        dict[str, JsonValue] | None,
        Field(description="Executor-specific, e.g. Harbor's ids"),
    ]
    id: str
    phase: str | None
    setup_network_revision: Annotated[
        int | None,
        Field(description="Setup network revision this attempt's setup was given; none for Oracle"),
    ]
    started_at: str
    state: Literal[
        "starting",
        "preparing",
        "executing",
        "verifying",
        "completed",
        "failed",
        "cancelled",
        "interrupted",
    ]
    steps: Annotated[
        list[AttemptStep],
        Field(description="Step start times; a step ends when the next starts or the attempt ends"),
    ]


class Batch(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    allowed_actions: list[str]
    client_request_id: str | None
    created_at: str
    id: str
    name: str
    operation_id: str
    owner_id: str
    owner_username: str | None
    pat_id: str | None
    progress: BatchProgress
    runs: list[Run]
    status: str
    stop_requested: bool


class BatchCreate(BaseModel):
    """
    Runs accepted together, all or none; each costs one unit of PAT quota.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    client_request_id: str | None = None
    name: str
    runs: list[RunCreate]


class BatchOperation(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    allowed_actions: list[str]
    created_at: str
    error: Problem | None
    finished_at: str | None
    id: str
    kind: Literal["batch"]
    links: dict[str, str]
    owner_id: str | None
    phase: str | None
    progress: BatchProgress
    resource: OperationResource | None
    result: BatchOperationResult | None
    revision: int
    status: Literal["accepted", "running", "succeeded", "failed", "cancelled", "interrupted"]
    updated_at: str


class BlockedTask(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    blockers: list[TaskBlocker]
    task_id: str


class Capabilities(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    actions: dict[str, ActionRule]
    api_version: str
    auth: AuthInfo
    contract_digest: Annotated[
        str,
        Field(
            description="sha256 of the OpenAPI document; clients generated from another one warn"
        ),
    ]
    execution: ExecutionInfo
    features: dict[str, bool]
    limits: dict[str, int]
    schema_revision: str
    service_version: str
    team_policy_revision: int


class CollectionBase(BaseModel):
    """
    Where the fixed values come from: a run, or a full configuration request.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    request: ConfigRequestInput | None = None
    run_id: str | None = None


class ComparisonRequest(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    baseline_run_id: str | None = None
    exclude: Annotated[
        list[
            Literal[
                "provider_unavailable",
                "provider_access",
                "environment_setup",
                "preparation",
                "executor_unsupported",
                "platform_stop",
                "out_of_memory",
                "verifier",
                "attachment",
                "provider_request",
                "unclassified",
            ]
        ]
        | None,
        Field(description="Failure groups left out; failures of other groups count as failed"),
    ] = None
    layout: LayoutInput | None = None
    run_ids: Annotated[list[str], Field(description="Runs in display order")]
    scope: Annotated[
        Literal["common", "all"] | None,
        Field(description="common: tasks every run has; all: every task, no differences"),
    ] = None


class ComparisonSettings(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    baseline_run_id: str | None
    exclude: list[
        Literal[
            "provider_unavailable",
            "provider_access",
            "environment_setup",
            "preparation",
            "executor_unsupported",
            "platform_stop",
            "out_of_memory",
            "verifier",
            "attachment",
            "provider_request",
            "unclassified",
        ]
    ]
    layout: LayoutOutput
    scope: Literal["common", "all"]


class ComparisonSettingsInput(BaseModel):
    """
    Unset fields keep the saved (or default) value.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    baseline_run_id: str | None = None
    exclude: Annotated[
        list[
            Literal[
                "provider_unavailable",
                "provider_access",
                "environment_setup",
                "preparation",
                "executor_unsupported",
                "platform_stop",
                "out_of_memory",
                "verifier",
                "attachment",
                "provider_request",
                "unclassified",
            ]
        ]
        | None,
        Field(description="Failure groups left out; failures of other groups count as failed"),
    ] = None
    layout: LayoutInput | None = None
    scope: Annotated[
        Literal["common", "all"] | None,
        Field(description="common: tasks every run has; all: every task, no differences"),
    ] = None


class ConfigBlocker(BaseModel):
    """
    Why this configuration cannot be submitted; ``path`` points at the field.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    code: str
    kind: Annotated[str | None, Field(description="ATTACHMENT_CONFLICT: name, tool or env")] = None
    limit: int | float | None = None
    message: str
    name: Annotated[str | None, Field(description="ATTACHMENT_CONFLICT: the claimed name")] = None
    path: str
    reason: str | None = None
    task_count: Annotated[int | None, Field(description="RUN_TOO_LARGE")] = None
    tasks: Annotated[list[BlockedTask] | None, Field(description="TASKS_NOT_SUBMITTABLE")] = None
    trial_count: Annotated[int | None, Field(description="RUN_TOO_LARGE")] = None


class DatasetImportOperation(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    allowed_actions: list[str]
    created_at: str
    error: Problem | None
    finished_at: str | None
    id: str
    kind: Literal["dataset_import"]
    links: dict[str, str]
    owner_id: str | None
    phase: str | None
    progress: ImportProgress
    resource: OperationResource | None
    result: ImportResult | None
    revision: int
    status: Literal["accepted", "running", "succeeded", "failed", "cancelled", "interrupted"]
    updated_at: str


class EffectiveConfig(BaseModel):
    """
    The fixed configuration of a run (03 section 5); ``config_digest`` is its digest.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    attachments: Annotated[list[EffectiveAttachment], Field(description="In load order")]
    bootstrap_timeout_sec: int | float | None
    concurrency: int
    dataset: EffectiveDataset
    deviates_from_author_baseline: bool
    execution_mode: Literal["agent", "oracle"]
    harness: EffectiveHarness | None
    model: EffectiveModel | None
    overrides: Annotated[
        EffectiveOverrides, Field(description="Values this run set for every task")
    ]
    parameters: dict[str, ParameterChoice]
    repetitions: int
    run_deadline_hours: int
    tasks: list[EffectiveTask]


class EnvelopeBatch(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: Batch
    meta: Meta


class EnvelopeCapabilities(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: Capabilities
    meta: Meta


class EnvelopeModelConfig(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: ModelConfig
    meta: Meta


class EnvelopeRunSummary(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: RunSummary
    meta: Meta


class EnvelopeRun(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: Run
    meta: Meta


class EnvelopeSystemStatus(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: SystemStatus
    meta: Meta


class EnvelopeListBatch(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[Batch]
    meta: Meta


class EnvelopeListModelConfig(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[ModelConfig]
    meta: Meta


class EnvelopeListRun(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[Run]
    meta: Meta


class EvictionOperation(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    allowed_actions: list[str]
    created_at: str
    error: Problem | None
    finished_at: str | None
    id: str
    kind: Literal["preparation_eviction"]
    links: dict[str, str]
    owner_id: str | None
    phase: str | None
    progress: NoProgress
    resource: OperationResource | None
    result: None
    revision: int
    status: Literal["accepted", "running", "succeeded", "failed", "cancelled", "interrupted"]
    updated_at: str


class ExcludedTask(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    blockers: list[TaskBlocker]
    task_id: str
    task_key: str


class Export(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    created_at: str
    digest: str | None
    error: Problem | None
    expires_at: str | None
    finished_at: str | None
    id: str
    links: dict[str, str]
    operation_id: str
    owner_id: str
    request: ExportRequest
    size_bytes: int | None
    state: Literal["queued", "building", "ready", "failed", "expired"]
    summary: ExportSummary | None


class ExportOperation(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    allowed_actions: list[str]
    created_at: str
    error: Problem | None
    finished_at: str | None
    id: str
    kind: Literal["export"]
    links: dict[str, str]
    owner_id: str | None
    phase: str | None
    progress: ExportProgress
    resource: OperationResource | None
    result: ExportOperationResult | None
    revision: int
    status: Literal["accepted", "running", "succeeded", "failed", "cancelled", "interrupted"]
    updated_at: str


class FirstPreparationTask(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    error: Problem | None = None
    image_id: str | None = None
    skipped: Annotated[bool | None, Field(description="Already decided by an earlier attempt")] = (
        None
    )
    state: Literal["pending", "admitted", "first_failed", "revoked"]


class Harness(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    agent: str
    content_digest: str
    created_at: str
    deletion: DeletionState | None
    description: str
    enabled: bool
    id: str
    name: str
    parameter_schema: HarnessParameterSchemaOutput
    protocols: list[Literal["openai-compatible", "openai", "anthropic"]]
    upstream_version: Annotated[
        str | None,
        Field(description="Exact version of the program installed in the task container"),
    ]
    version: str


class HarnessCreate(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    agent: Annotated[str, Field(description="Executor adapter; see /admin/harness-adapters")]
    description: str | None = ""
    name: str
    parameter_schema: Annotated[
        HarnessParameterSchemaInput | None, Field(validate_default=True)
    ] = {"parameters": {}}
    protocols: list[Literal["openai-compatible", "openai", "anthropic"]]
    upstream_version: Annotated[
        str | None,
        Field(
            description="Exact version of the program the adapter installs; required exactly when the adapter has a package"
        ),
    ] = None
    version: str


class LocalPreparation(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    cache_state: Literal["absent", "restoring", "ready", "stale"]
    id: str | None
    last_restore_error: Problem | None
    last_used_at: str | None
    location_id: str
    operation_id: str | None
    prepared_at: str | None
    setup_network_revision: Annotated[
        int | None,
        Field(description="Setup network revision the image was built with; none for a pull"),
    ]


class Plan(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    allowed_actions: list[str]
    archived_at: str | None
    copied_from: str | None
    created_at: str
    deletion: DeletionState | None
    description: str
    id: str
    name: str
    owner_id: str
    owner_username: str | None
    revision: int
    template: PlanTemplateOutput
    updated_at: str


class PlanCreate(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    description: str | None = ""
    name: str
    template: Annotated[PlanTemplateInput | None, Field(validate_default=True)] = {}


class Preparation(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    archive: PreparationArchive | None
    cache_state: Literal["absent", "restoring", "ready", "stale"]
    generation: int
    id: str | None
    image_id: str | None
    last_restore_error: Problem | None
    last_used_at: str | None
    location_id: str
    operation_id: str | None
    pins: list[PreparationPin]
    prepared_at: str | None
    setup_network_revision: Annotated[
        int | None,
        Field(description="Setup network revision the image was built with; none for a pull"),
    ]
    spec_digest: str
    waiters: list[PreparationWaiter]


class RunAccepted(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    cache_observations: list[CacheObservation]
    config_digest: str
    excluded_tasks: list[ExcludedTask]
    links: dict[str, str]
    operation_id: str
    planned_trials: int
    run_id: str
    snapshot_digest: str
    state: str


class RunProvenance(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    admissions: list[AdmissionRecord]
    baseline_differences: list[BaselineDifference]
    credential_ids: Annotated[list[str], Field(description="Bound credentials, by reference only")]
    excluded_tasks: list[ExcludedTask]
    field_sources: dict[str, FieldSource]
    labels: dict[str, str]
    reference_revisions: ReferenceRevisions
    request: NormalizedRequest
    resolution_id: str | None
    source: RunSource | None
    submitted_via: Literal["web_session", "pat"]
    task_selection: TaskSelectionOutput


class RunSnapshot(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    comparison_digest: str
    config_digest: str
    effective: EffectiveConfig
    policy: RunPolicy
    provenance: RunProvenance
    schema_version: int
    snapshot_digest: str


class SnapshotCreate(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    collection_id: str | None = None
    name: str
    note: str | None = None
    run_ids: Annotated[list[str] | None, Field(description="Without a collection")] = None
    settings: ComparisonSettingsInput | None = None


class Task(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    admission: TaskAdmission | None
    blockers: list[TaskBlocker]
    can_submit: bool
    content: TaskContent
    dataset_version_id: str
    id: str
    instruction: str
    local_preparation: LocalPreparation
    parameters: list[AuthorParameter]
    required_parameters: list[str]
    required_preparation: list[RequiredPreparation]
    resources: TaskResources
    spec_digest: str | None
    task_key: str
    timeouts: TaskTimeouts
    warnings: list[TaskWarning]


class Trial(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    artifact_counts: ArtifactCounts
    artifact_state: Literal["pending", "saving", "complete", "partial", "failed", "expired"]
    attempts: list[Attempt]
    cleanup_state: Literal["pending", "running", "complete", "failed", "not_required"]
    id: str
    phase: str | None
    reason_code: str | None
    repetition_index: int
    result: TrialResult | None
    state: Literal[
        "queued",
        "preparing",
        "executing",
        "verifying",
        "scored",
        "execution_error",
        "not_started",
        "cancelled",
        "interrupted",
    ]
    task_id: str
    task_key: str


class BatchAccepted(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    batch_id: str
    links: dict[str, str]
    operation_id: str
    planned_trials: int
    runs: list[RunAccepted]


class BatchPreviewRun(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    blockers: list[ConfigBlocker] | None = None
    config_digest: str | None = None
    index: int
    labels: dict[str, str] | None = None
    planned_trials: int
    warnings: list[ConfigWarning] | None = None


class Collection(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    allowed_actions: list[str]
    base_run_id: str | None
    copied_from: str | None
    created_at: str
    deletion: DeletionState | None
    description: str
    fixed: Annotated[
        dict[str, dict[str, JsonValue]],
        Field(description="Fixed dimensions and their values"),
    ]
    fixed_labels: dict[str, str]
    id: str
    members: list[CollectionMember] | None
    name: str
    not_carried: Annotated[
        list[NotCarried] | None,
        Field(description="After a copy: runs left behind and what differs"),
    ]
    owner_id: str
    owner_username: str | None
    run_count: int
    scenario: Literal["models", "harnesses", "model_harness", "attachments", "repeats", "custom"]
    settings: ComparisonSettings
    template: Annotated[
        PlanTemplateOutput,
        Field(description="Fills in runs submitted from the collection"),
    ]
    updated_at: str
    variable_dimensions: list[
        Literal["model", "harness", "attachments", "resources", "repetitions", "parameters"]
    ]


class CollectionCopy(BaseModel):
    """
    A new collection with a changed specification; matching runs are carried over.
    """

    model_config = ConfigDict(
        extra="allow",
    )
    base: Annotated[
        CollectionBase | None,
        Field(description="Required when a dimension that varied becomes fixed"),
    ] = None
    description: str | None = None
    name: str | None = None
    scenario: (
        Literal["models", "harnesses", "model_harness", "attachments", "repeats", "custom"] | None
    ) = None
    settings: ComparisonSettingsInput | None = None
    variable_dimensions: (
        list[Literal["model", "harness", "attachments", "resources", "repetitions", "parameters"]]
        | None
    ) = None


class CollectionCreate(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    base: CollectionBase
    description: str | None = ""
    name: str
    run_ids: list[str] | None = []
    scenario: Literal["models", "harnesses", "model_harness", "attachments", "repeats", "custom"]
    settings: ComparisonSettingsInput | None = None
    variable_dimensions: Annotated[
        list[Literal["model", "harness", "attachments", "resources", "repetitions", "parameters"]]
        | None,
        Field(description="Only for the custom scenario"),
    ] = None


class CollectionUpdate(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    description: str | None = None
    name: str | None = None
    settings: ComparisonSettingsInput | None = None


class Comparison(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    all_tasks: int
    blockers: list[ComparisonNotice]
    common_tasks: int
    comparable: Annotated[bool, Field(description="Same dataset content and scoring contract")]
    comparison_version: int
    dimensions: list[ComparisonDimension]
    dropped_tasks: Annotated[
        int, Field(description="Common tasks left out: a run had no valid trial")
    ]
    grouping_version: int
    groups: list[FailureGroupCount]
    runs: list[ComparedRunRow]
    set_aside: list[SetAsideRun]
    settings: ComparisonSettings
    tasks: list[ComparedTask]
    varying: list[
        Literal[
            "dataset",
            "scoring",
            "model",
            "harness",
            "attachments",
            "resources",
            "repetitions",
            "parameters",
            "other",
        ]
    ]
    warnings: list[ComparisonNotice]


class ConfigResolution(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    baseline_differences: list[BaselineDifference]
    blockers: list[ConfigBlocker]
    cache_observations: list[CacheObservation]
    config_digest: str | None
    created_at: str
    effective_config: EffectiveConfig | None
    excluded_tasks: list[ExcludedTask]
    expires_at: str
    field_sources: Annotated[dict[str, FieldSource], Field(description="Keyed by JSON pointer")]
    labels: Annotated[
        dict[str, str], Field(description="Display names: model, harness, attachments")
    ]
    planned_trials: int
    reference_revisions: ReferenceRevisions
    request: NormalizedRequest
    required_preparation: list[PlannedRestore]
    resolution_id: str
    warnings: list[ConfigWarning]


class EnvelopeBatchAccepted(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: BatchAccepted
    meta: Meta


class EnvelopeCollection(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: Collection
    meta: Meta


class EnvelopeComparison(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: Comparison
    meta: Meta


class EnvelopeConfigResolution(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: ConfigResolution
    meta: Meta


class EnvelopeExport(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: Export
    meta: Meta


class EnvelopeHarness(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: Harness
    meta: Meta


class EnvelopePlan(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: Plan
    meta: Meta


class EnvelopePreparation(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: Preparation
    meta: Meta


class EnvelopeRunAccepted(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: RunAccepted
    meta: Meta


class EnvelopeListCollection(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[Collection]
    meta: Meta


class EnvelopeListExport(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[Export]
    meta: Meta


class EnvelopeListHarness(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[Harness]
    meta: Meta


class EnvelopeListPlan(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[Plan]
    meta: Meta


class EnvelopeListTask(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[Task]
    meta: Meta


class EnvelopeListTrial(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[Trial]
    meta: Meta


class FirstPreparationResult(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    tasks: Annotated[dict[str, FirstPreparationTask], Field(description="Keyed by task id")]


class RunDetail(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    accepted_at: str
    allowed_actions: list[str]
    artifact_state: Literal["pending", "saving", "complete", "partial", "failed", "expired"]
    batch_id: str | None
    cleanup_state: Literal["pending", "running", "complete", "failed", "not_required"]
    client_request_id: str | None
    counts: RunCounts
    dataset_version_deletion: DeletionState | None
    dataset_version_id: str
    deadline_at: str
    deletion: Annotated[
        DeletionState | None,
        Field(description="Set when the run is in the recycle bin"),
    ]
    finished_at: str | None
    heartbeat_at: Annotated[
        str | None, Field(description="Last sign of life from the run's Runner")
    ]
    id: str
    links: dict[str, str]
    name: str
    operation_id: str
    owner: Owner
    owner_id: str
    pat_id: str | None
    plan_id: str | None
    plan_revision: int | None
    queue_reason: Annotated[
        str | None,
        Field(
            description="Why a queued run waits: CAPACITY_SLOTS, CAPACITY_CPU, CAPACITY_MEMORY or PROVIDER_LIMIT"
        ),
    ]
    relation: Literal["initial", "rerun", "modified"]
    result_completeness: Literal["none", "partial", "complete"]
    scoring: RunScoring | None
    snapshot: RunSnapshot
    source_run_id: str | None
    started_at: str | None
    state: Literal[
        "queued", "preparing", "running", "finalizing", "finished", "cancelled", "interrupted"
    ]
    stop_reason: str | None
    stop_requested: bool


class Snapshot(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    allowed_actions: list[str]
    collection_id: str | None
    created_at: str
    deletion: DeletionState | None
    evidence: Annotated[
        dict[str, Literal["available", "none", "deleted", "purged"]],
        Field(description="Whether each run's files still open"),
    ]
    id: str
    name: str
    notes: list[SnapshotNote]
    owner_id: str
    owner_username: str | None
    pat_id: str | None
    result: Comparison
    run_count: int
    settings: ComparisonSettings
    spec: SnapshotSpec | None


class BatchPreview(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    blockers: list[BatchBlocker]
    can_submit: bool
    duplicate_configs: Annotated[list[str], Field(description="Configuration digests given twice")]
    pat_quota: PatQuota | None
    planned_trials: int
    runs: list[BatchPreviewRun]


class EnvelopeBatchPreview(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: BatchPreview
    meta: Meta


class EnvelopeRunDetail(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: RunDetail
    meta: Meta


class EnvelopeSnapshot(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: Snapshot
    meta: Meta


class FirstPreparationOperation(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    allowed_actions: list[str]
    created_at: str
    error: Problem | None
    finished_at: str | None
    id: str
    kind: Literal["preparation_first"]
    links: dict[str, str]
    owner_id: str | None
    phase: str | None
    progress: FirstPreparationProgress
    resource: OperationResource | None
    result: FirstPreparationResult | None
    revision: int
    status: Literal["accepted", "running", "succeeded", "failed", "cancelled", "interrupted"]
    updated_at: str


class Operation(
    RootModel[
        RunOperation
        | BatchOperation
        | ExportOperation
        | DatasetImportOperation
        | FirstPreparationOperation
        | RestoreOperation
        | EvictionOperation
    ]
):
    root: Annotated[
        RunOperation
        | BatchOperation
        | ExportOperation
        | DatasetImportOperation
        | FirstPreparationOperation
        | RestoreOperation
        | EvictionOperation,
        Field(
            description="A long operation; ``kind`` decides the shape of ``progress`` and ``result``.",
            discriminator="kind",
        ),
    ]


class EnvelopeOperation(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: Operation
    meta: Meta


class EnvelopeListOperation(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    data: list[Operation]
    meta: Meta
