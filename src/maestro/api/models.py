"""Public API models. They define the OpenAPI contract shared by web and SDK."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel, model_serializer

# Free-form JSON. Only for values that are open by design (provider request parameters,
# executor references, evidence, audit details); test_api_contract keeps the list.
JsonObject = dict[str, JsonValue]
Number = int | float


class Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        protected_namespaces=(),
        # A response carries every field, defaults included, so the response schema marks
        # them required even where a request may leave them out.
        json_schema_serialization_defaults_required=True,
    )


def omit_unset(model: BaseModel, data: dict, *names: str) -> dict:
    """Leave out these fields when they were never set: their absence carries meaning."""
    for name in names:
        if name not in model.model_fields_set:
            data.pop(name, None)
    return data


class Sparse(Model):
    """Fields that do not apply are left out of the JSON rather than sent as null."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=False)

    @model_serializer(mode="wrap")
    def _without_nulls(self, handler):
        return {k: v for k, v in handler(self).items() if v is not None}


class Meta(Model):
    request_id: str
    next_cursor: str | None = None


class Envelope[T](Model):
    data: T
    meta: Meta


class FieldErrorModel(Model):
    path: str
    code: str
    message: str | None = None
    limit: int | float | None = None


class SuggestedAction(Model):
    """A next step; ``type`` says which, and the other fields depend on it."""

    model_config = ConfigDict(extra="allow")
    type: str


class ErrorBody(Model):
    code: str
    message: str
    stage: str | None = None
    field_errors: list[FieldErrorModel] = []
    retryable: bool = False
    suggested_actions: list[SuggestedAction] = []


class Problem(Sparse):
    """A recorded failure: a stable code and a message, with the cause's kind, evidence
    and next steps when it has them. Further fields depend on the code."""

    model_config = ConfigDict(extra="allow")
    code: str
    message: str | None = None
    kind: str | None = None
    evidence: JsonObject | None = None
    suggested_actions: list[SuggestedAction] | None = None


class ErrorResponse(Model):
    error: ErrorBody
    meta: Meta


# --- identity -----------------------------------------------------------------

Role = Literal["admin", "member"]
Scope = Literal["read", "experiment"]


class User(Model):
    id: str
    username: str
    display_name: str
    role: Role
    status: Literal["active", "disabled"]
    revision: int
    created_at: str
    disabled_at: str | None


class Me(User):
    auth_channel: Literal["web_session", "pat"]
    pat_id: str | None
    pat_scope: Scope | None


class SessionInfo(Model):
    user: Me
    csrf_token: str | None = Field(
        description="Send as X-CSRF-Token on unsafe requests authenticated by the cookie"
    )


class SetupStatus(Model):
    needs_setup: bool


# Sign-in and setup bodies come from anyone who can reach the port: bound every field.
# The password limit is the policy's (security.passwords.MAX_LENGTH).
UsernameText = Annotated[str, Field(max_length=128)]
PasswordText = Annotated[str, Field(max_length=256)]
TokenText = Annotated[str, Field(max_length=256)]


class SetupRequest(Model):
    setup_token: TokenText
    username: UsernameText
    display_name: str | None = Field(None, max_length=1024)
    password: PasswordText


class LoginRequest(Model):
    username: UsernameText
    password: PasswordText


class PasswordChange(Model):
    current_password: PasswordText
    new_password: PasswordText


class InvitationCreate(Model):
    role: Role = "member"
    note: str = ""
    valid_hours: int | None = None


class Invitation(Model):
    id: str
    role: Role
    note: str
    kind: Literal["invitation", "password_reset"]
    target_user_id: str | None
    state: Literal["pending", "used", "revoked", "expired"]
    created_by: str
    created_at: str
    expires_at: str
    used_at: str | None
    used_by: str | None


class IssuedInvitation(Invitation):
    token: str = Field(description="Shown once")


class InvitationLookup(Model):
    token: TokenText


class InvitationInfo(Model):
    kind: Literal["invitation", "password_reset"]
    role: Role
    username: str | None
    expires_at: str


class InvitationAccept(Model):
    token: TokenText
    username: UsernameText | None = None
    display_name: str | None = Field(None, max_length=1024)
    password: PasswordText


class StatusChange(Model):
    expected_revision: int


class PatCreate(Model):
    name: str
    scope: Scope
    expires_in_days: int | None = None
    run_limit: int | None = None


class Pat(Model):
    id: str
    name: str
    scope: Scope
    token_prefix: str
    state: Literal["active", "revoked", "expired"]
    expires_at: str
    run_limit: int
    accepted_runs: int
    remaining_runs: int
    created_at: str
    last_used_at: str | None
    revoked_at: str | None
    revision: int


class IssuedPat(Pat):
    token: str = Field(description="Shown once; store it securely")


# --- credentials ----------------------------------------------------------------

CredentialKind = Literal["model_api_key", "hub_token", "generic_secret"]


class CredentialCreate(Model):
    name: str
    kind: CredentialKind
    provider: str
    description: str = ""
    value: str


class CredentialValue(Model):
    value: str
    expected_revision: int


class Credential(Model):
    id: str
    name: str
    kind: CredentialKind
    provider: str
    description: str
    enabled: bool
    revision: int
    key_version: int
    created_by: str
    created_at: str
    updated_at: str


class MasterKeyStatus(Model):
    initialized: bool
    available: bool
    generation: int | None
    rotated_at: str | None
    reencrypted_credentials: int | None = None


class MasterKeyRotation(Model):
    expected_generation: int


# --- catalog ------------------------------------------------------------------------


class DatasetVersion(Model):
    id: str
    dataset_id: str
    upstream_ref: str
    ref_kind: str
    resolved_digest: str
    materialization_digest: str | None
    status: Literal["ready"]
    description: str
    created_at: str
    created_by: str | None
    operation_id: str | None
    task_count: int
    admitted_task_count: int
    submittable_task_count: int
    cache_ready_task_count: int


class DeletionState(Model):
    state: Literal["recycled", "purged"] = Field(
        description="recycled: in the recycle bin; purged: content gone, receipt kept"
    )
    deleted_at: str
    purged_at: str | None


class PassRule(Model):
    rule: Literal["threshold"]
    threshold: Number = Field(description="A trial passes when its score reaches this")


class ScoringContract(Model):
    model_config = ConfigDict(extra="allow")

    contract_version: int
    source: Literal["platform_default", "dataset_version"]
    metric_id: str
    name: str
    unit: str | None
    direction: Literal["higher_is_better", "lower_is_better"]
    passed: PassRule | None = Field(description="Explicit pass rule; null: no pass rate")
    task_aggregation: str
    bench_aggregation: str
    count_as_failure: list[str]


class DatasetVersionDetail(DatasetVersion):
    namespace: str
    name: str
    first_preparation_operation_id: str | None
    scoring_contract: ScoringContract = Field(
        description="Contract new runs freeze; source is dataset_version or platform_default"
    )
    deletion: DeletionState | None = Field(
        description="Set when the version or its dataset was deleted; it still resolves"
    )


class Dataset(Model):
    id: str
    source_kind: str
    namespace: str
    name: str
    description: str
    credential_id: str | None
    versions: list[DatasetVersion]


class TaskAdmission(Model):
    id: str
    state: Literal["pending", "admitted", "first_failed", "revoked"]
    revision: int
    approved_at: str | None


class ContentResource(Model):
    resource_kind: Literal["task_content", "environment_image"]
    digest: str
    availability: Literal["unknown", "available", "unavailable"]
    checked_at: str | None


class TaskContent(Model):
    digest: str
    material_digest: str | None
    availability: Literal["unknown", "available", "unavailable"]
    checked_at: str | None
    resources: list[ContentResource]


class LocalPreparation(Model):
    id: str | None
    location_id: str
    cache_state: Literal["absent", "restoring", "ready", "stale"]
    last_restore_error: Problem | None
    operation_id: str | None
    prepared_at: str | None
    last_used_at: str | None
    setup_network_revision: int | None = Field(
        description="Setup network revision the image was built with; none for a pull"
    )


class ContentEvidence(Sparse):
    reason: str | None = None
    message: str | None = None


class ParameterIssue(Sparse):
    code: Literal["CONFLICTING_DEFAULTS", "COMPOSE_DISCOVERY_FAILED", "RESERVED_NAME"]
    name: str | None = Field(None, description="The parameter")
    used_in: str | None = Field(None, description="RESERVED_NAME: where the task uses it")
    file: str | None = Field(None, description="The compose file (COMPOSE_DISCOVERY_FAILED)")


class TaskBlocker(Sparse):
    """Why a task cannot be chosen; the fields that apply depend on the code."""

    code: Literal[
        "TASK_NOT_ADMITTED",
        "TASK_ADMISSION_REVOKED",
        "RESOURCE_CONTENT_UNAVAILABLE",
        "CONFIG_REQUIRED_MISSING",
        "AUTHOR_PARAMETERS_UNRESOLVED",
        "TEAM_LIMIT_EXCEEDED",
        "NOT_FOUND",
    ]
    admission_state: str | None = None
    first_preparation_error: Problem | None = None
    resource_kind: str | None = None
    expected_digest: str | None = None
    evidence: ContentEvidence | None = None
    parameters: list[str] | None = None
    issues: list[ParameterIssue] | None = None
    field: str | None = Field(None, description="TEAM_LIMIT_EXCEEDED: the task setting")
    value: Number | None = None
    limit: Number | None = None


class TaskWarning(Model):
    code: Literal["PREPARATION_CACHE_MISS"]
    action: str


class RequiredPreparation(Model):
    action: Literal["restore_cache"]
    spec_digest: str


class AuthorParameter(Model):
    """Author parameter contract (03): defaults, requirement, constraints."""

    name: str
    default: str | None
    required: bool
    sensitive: bool
    description: str
    used_in: list[str]


class EnvironmentResources(Model):
    role: str = Field(
        description="environment/main, verifier, or <directory>/<service> of a Compose service"
    )
    cpus: Number | None = Field(description="null: the team default for services applies")
    memory_mb: int | None
    default: bool | None = Field(
        None, description="In a run: a team default fills in a limit the author left out"
    )


class TaskResources(Model):
    """What the task's author asks for; a run may raise CPU and memory, never lower them.
    ``cpus`` and ``memory_mb`` are the main environment's."""

    cpus: Number | None = None
    memory_mb: int | None = None
    storage_mb: int | None = None
    constraint_strength: str | None = None
    environments: list[EnvironmentResources] = Field(
        [], description="Other containers a trial starts: a separate verifier, Compose services"
    )


class TaskTimeouts(Model):
    agent_sec: Number | None = None
    verifier_sec: Number | None = None
    build_sec: Number | None = None


class Task(Model):
    id: str
    dataset_version_id: str
    task_key: str
    admission: TaskAdmission | None
    content: TaskContent
    local_preparation: LocalPreparation
    spec_digest: str | None
    resources: TaskResources
    timeouts: TaskTimeouts
    parameters: list[AuthorParameter]
    required_parameters: list[str] = []
    instruction: str
    can_submit: bool
    blockers: list[TaskBlocker]
    warnings: list[TaskWarning]
    required_preparation: list[RequiredPreparation]


class PreparationWaiter(Model):
    run_id: str | None
    operation_id: str | None
    state: str
    since: str


class PreparationPin(Model):
    holder_kind: str
    holder_id: str


class PreparationImage(Model):
    """One image of a preparation, kept in the local image registry (N-01)."""

    role: str = Field(description="<directory>/<service>, e.g. environment/main, tests/main")
    digest: str = Field(description="Manifest digest in the local image registry")
    ref: str


class Preparation(LocalPreparation):
    spec_digest: str
    generation: int
    content_id: str | None = Field(description="All images as one content identity")
    images: list[PreparationImage]
    waiters: list[PreparationWaiter]
    pins: list[PreparationPin]


class DatasetSource(Model):
    kind: str
    available: bool


class SourceDataset(Model):
    namespace: str
    name: str
    visibility: str | None = None


class SourceVersion(Model):
    ref: str
    revision: int | None = None
    tags: list[str] = []
    resolved_digest: str
    published_at: str | None = None
    yanked: bool = False
    yanked_reason: str | None = None


class SourceTask(Model):
    task_key: str
    content_digest: str


class SourceVersionDetail(Model):
    namespace: str
    name: str
    upstream_ref: str
    ref_kind: str
    resolved_digest: str
    description: str
    tasks: list[SourceTask]
    existing_version_id: str | None


class DatasetImport(Model):
    source_kind: str = "hub"
    namespace: str
    name: str
    ref: str = Field(description="Tag, revision or sha256 digest; always pinned to a digest")
    credential_id: str | None = None
    accept_changed_content: bool = Field(
        False, description="Record a moved tag as a new version (CONTENT_REFERENCE_CHANGED)"
    )
    prepare: bool = Field(True, description="Start the first preparation after the import")


class FirstPreparation(Model):
    task_ids: list[str] | None = Field(
        None, description="Default: every task without a successful first preparation"
    )


class ScoringContractInput(Model):
    metric_id: str = "reward"
    name: str = "Reward"
    unit: str | None = None
    direction: Literal["higher_is_better", "lower_is_better"]
    pass_threshold: float | None = Field(
        description="A trial passes when its score reaches this value; null: no pass rate"
    )


class AdmissionRevocation(Model):
    reason: str
    expected_revision: int


class AdmissionRevoked(Model):
    id: str
    state: Literal["revoked"]
    revision: int


# --- runs and operations -------------------------------------------------------------


# A harness parameter value; the harness version's parameter_schema says which type.
HarnessParamValue = str | int | float | bool


class TaskSelection(Model):
    mode: Literal["all_eligible", "explicit"] = "all_eligible"
    task_ids: list[str] | None = None


class CredentialRef(Model):
    credential_id: str


class ResourceOverrides(Sparse):
    cpus: int | None = None
    memory_mb: int | None = None


class TimeoutOverrides(Sparse):
    agent_sec: float | None = None
    verifier_sec: float | None = None
    bootstrap_sec: float | None = Field(
        None, description="Time for each trial to install all attachments (team default)"
    )


class RunConfigFields(Model):
    """A run configuration request (03). Unset fields are inherited: author values for
    task resources, timeouts and parameters, the team policy for the rest."""

    plan_id: str | None = Field(
        None, description="An experiment plan; fields set here win, the plan fills in the rest"
    )
    plan_revision: int | None = Field(
        None, description="Refuse unless the plan is still at this revision"
    )
    collection_id: str | None = Field(
        None,
        description="Join this collection: its fixed fields fill in what is left out, and "
        "the run must match them",
    )
    task_selection: TaskSelection = TaskSelection()
    model_config_id: str | None = None
    model_revision: int | None = Field(
        None, description="An earlier revision of the model configuration; unset: current"
    )
    harness_version_id: str | None = None
    harness_params: dict[str, HarnessParamValue] = {}
    attachments: list[str] = Field(
        [], description="Attachment snapshot ids in load order (model runs only)"
    )
    repetitions: int | None = None
    concurrency: int | None = None
    run_deadline_hours: int | None = None
    resources: ResourceOverrides = ResourceOverrides()
    timeouts: TimeoutOverrides = TimeoutOverrides()
    parameters: dict[str, str | CredentialRef] = Field(
        {}, description="Author parameter values; secret ones as a credential reference"
    )


class ConfigRequest(RunConfigFields):
    dataset_version_id: str | None = Field(None, description="Required unless the plan has it")
    execution_mode: Literal["agent", "oracle"] | None = Field(
        None,
        description="agent: a model through a harness; oracle: each task's reference "
        "solution, a test mode never compared with model results. Required unless the "
        "plan has it",
    )


class RunCreate(RunConfigFields):
    """Either ``resolution_id`` of a resolution made before, or the configuration inline."""

    name: str
    resolution_id: str | None = None
    dataset_version_id: str | None = None
    execution_mode: Literal["agent", "oracle"] | None = None
    client_request_id: str | None = None
    source_run_id: str | None = Field(
        None,
        description="Makes this a modified run of that run: its differences are recorded "
        "(start from GET /runs/{run_id}/request)",
    )


class PlanTemplate(Sparse):
    """Any fields of a configuration request (see ConfigRequest). A field the plan does
    not set is left out; a run's own value wins over the plan's."""

    dataset_version_id: str | None = None
    task_selection: TaskSelection | None = None
    execution_mode: Literal["agent", "oracle"] | None = None
    model_config_id: str | None = None
    model_revision: int | None = None
    harness_version_id: str | None = None
    harness_params: dict[str, HarnessParamValue] | None = None
    attachments: list[str] | None = None
    repetitions: int | None = None
    concurrency: int | None = None
    run_deadline_hours: int | None = None
    resources: ResourceOverrides | None = None
    timeouts: TimeoutOverrides | None = None
    parameters: dict[str, str | CredentialRef] | None = None


class PlanCreate(Model):
    name: str
    description: str = ""
    template: PlanTemplate = PlanTemplate()


class PlanUpdate(Model):
    expected_revision: int
    name: str | None = None
    description: str | None = None
    template: PlanTemplate | None = None


class PlanCopy(Model):
    name: str | None = None


class PlanArchive(Model):
    archived: bool


class Plan(Model):
    id: str
    name: str
    description: str
    owner_id: str
    owner_username: str | None
    revision: int
    template: PlanTemplate
    copied_from: str | None
    created_at: str
    updated_at: str
    archived_at: str | None
    allowed_actions: list[str]
    deletion: DeletionState | None = None


class RerunSelection(Model):
    mode: Literal["all", "unfinished", "explicit"] = Field(
        "all",
        description="unfinished: every task with a trial not scored (the source must "
        "have ended); explicit: task_ids of the source run",
    )
    task_ids: list[str] | None = None


class RunRerun(Model):
    """A new run from the source run's snapshot; only the task set may shrink."""

    name: str | None = None
    task_selection: RerunSelection = RerunSelection()
    client_request_id: str | None = None


class BlockedTask(Model):
    task_id: str
    blockers: list[TaskBlocker]


class ConfigBlocker(Sparse):
    """Why this configuration cannot be submitted; ``path`` points at the field."""

    code: str
    path: str
    message: str
    limit: Number | None = None
    reason: str | None = None
    kind: str | None = Field(None, description="ATTACHMENT_CONFLICT: name, tool or env")
    name: str | None = Field(None, description="ATTACHMENT_CONFLICT: the claimed name")
    tasks: list[BlockedTask] | None = Field(None, description="TASKS_NOT_SUBMITTABLE")
    task_count: int | None = Field(None, description="RUN_TOO_LARGE")
    trial_count: int | None = Field(None, description="RUN_TOO_LARGE")


class ConfigWarning(Model):
    code: str
    path: str
    message: str


class FieldSource(Sparse):
    """Which layer gave a value: author, team policy, plan, collection, harness default or
    override."""

    layer: Literal["author", "team", "plan", "collection", "override", "harness"]
    source_id: str | None = None
    revision: int | None = None


class BaselineDifference(Model):
    path: str
    value: Number
    tasks_lower: int = Field(description="Tasks whose author value is higher")
    tasks_higher: int = Field(description="Tasks whose author value is lower or unset")


class ExcludedTask(Model):
    task_id: str
    task_key: str
    blockers: list[TaskBlocker]


class CacheObservation(Model):
    task_key: str
    cache_state: Literal["ready", "cold"]
    action: Literal["restore_on_run"] | None


class PlannedRestore(Model):
    task_id: str
    task_key: str
    action: Literal["restore_cache"]
    spec_digest: str


class RevisionRef(Model):
    id: str
    revision: int


class ObjectRef(Model):
    id: str


class ReferenceRevisions(Sparse):
    team_policy: int | None = None
    model_config_ref: RevisionRef | None = Field(None, alias="model_config")
    harness_version: str | None = None
    attachments: list[str] | None = None
    plan: RevisionRef | None = None
    collection: ObjectRef | None = None


class ParameterChoice(Sparse):
    """An author parameter value: a literal, or the team credential that holds it."""

    value: str | None = None
    credential_id: str | None = None


class PlanUse(Model):
    plan_id: str
    revision: int
    fields: list[str] = Field(description="JSON pointers of the fields the plan supplied")


class NormalizedRequest(Model):
    """A configuration request after checking: what resolution and reruns start from."""

    plan: PlanUse | None = None
    dataset_version_id: str
    task_selection: TaskSelection
    execution_mode: Literal["agent", "oracle"]
    model_config_id: str | None
    model_revision: int | None
    harness_version_id: str | None
    harness_params: dict[str, HarnessParamValue]
    attachments: list[str]
    repetitions: int | None
    concurrency: int | None
    run_deadline_hours: int | None
    resources: ResourceOverrides
    timeouts: TimeoutOverrides
    parameters: dict[str, ParameterChoice]


Protocol = Literal["openai-compatible", "openai", "anthropic"]


class AttachmentProvides(Model):
    tools: list[str]
    env: list[str]


class EffectiveDataset(Model):
    source_kind: str
    namespace: str
    name: str
    version_id: str
    resolved_digest: str


class EffectiveModel(Model):
    model_config = ConfigDict(json_schema_serialization_defaults_required=False)

    config_id: str
    revision: int
    provider: str
    protocol: Protocol
    endpoint: str
    model_id: str
    params: JsonObject = Field(description="Provider request parameters, as configured")
    credential_id: str | None = Field(
        None,
        description="The bound team credential; null: none. Left out for everyone but "
        "administrators' web sessions (U-27)",
    )

    @model_serializer(mode="wrap")
    def _binding_only_when_shown(self, handler):
        return omit_unset(self, handler(self), "credential_id")


class EffectiveHarness(Model):
    version_id: str
    name: str
    version: str
    agent: str
    upstream_version: str | None = Field(
        None, description="Program version installed in the task container, when any"
    )
    content_digest: str
    params: dict[str, HarnessParamValue]


class EffectiveAttachment(Model):
    version_id: str = Field(description="The snapshot's id")
    name: str
    label: str | None
    content_digest: str
    effects: list[str]
    provides: AttachmentProvides
    has_bootstrap: bool


class EffectiveTask(Model):
    task_key: str
    content_digest: str
    spec_digest: str
    cpus: Number = Field(description="The trial's total over every container it starts")
    memory_mb: int
    environments: list[EnvironmentResources] = Field(
        [], description="Each container's share; an override's extra goes to environment/main"
    )
    agent_timeout_sec: Number | None = Field(description="null: the executor's default")
    verifier_timeout_sec: Number | None


class EffectiveOverrides(Sparse):
    cpus: int | None = None
    memory_mb: int | None = None
    agent_timeout_sec: Number | None = None
    verifier_timeout_sec: Number | None = None


class EffectiveConfig(Model):
    """The fixed configuration of a run (03 section 5); ``config_digest`` is its digest."""

    dataset: EffectiveDataset
    execution_mode: Literal["agent", "oracle"]
    repetitions: int
    concurrency: int
    run_deadline_hours: int
    model: EffectiveModel | None
    harness: EffectiveHarness | None
    attachments: list[EffectiveAttachment] = Field(description="In load order")
    bootstrap_timeout_sec: Number | None
    overrides: EffectiveOverrides = Field(description="Values this run set for every task")
    tasks: list[EffectiveTask]
    parameters: dict[str, ParameterChoice]
    deviates_from_author_baseline: bool


class ConfigResolution(Model):
    resolution_id: str
    created_at: str
    expires_at: str
    request: NormalizedRequest
    effective_config: EffectiveConfig | None
    field_sources: dict[str, FieldSource] = Field(description="Keyed by JSON pointer")
    baseline_differences: list[BaselineDifference]
    excluded_tasks: list[ExcludedTask]
    required_preparation: list[PlannedRestore]
    cache_observations: list[CacheObservation]
    warnings: list[ConfigWarning]
    blockers: list[ConfigBlocker]
    config_digest: str | None
    reference_revisions: ReferenceRevisions
    labels: dict[str, str] = Field(description="Display names: model, harness, attachments")
    planned_trials: int


class CredentialName(Model):
    """A usable team credential as everyone sees it: the name, never the value."""

    id: str
    name: str
    kind: str
    provider: str
    description: str


class ModelConfigFields(Model):
    provider: str = Field(description="Provider name; concurrency limits apply per provider")
    protocol: Protocol
    endpoint: str = Field(description="http(s) base URL, no credentials or query")
    model_id: str
    params: JsonObject = Field({}, description="Provider request parameters; no secrets")
    credential_id: str | None = None
    description: str = ""


class ModelConfigCreate(ModelConfigFields):
    name: str


class ModelConfigRevise(ModelConfigFields):
    expected_revision: int


class ModelRevision(Model):
    revision: int
    provider: str
    protocol: Protocol
    endpoint: str
    model_id: str
    params: JsonObject
    credential_bound: bool
    credential_id: str | None = None
    created_at: str


class ModelConfig(Model):
    id: str
    name: str
    description: str
    enabled: bool
    revision: int
    provider: str
    protocol: Protocol
    endpoint: str
    model_id: str
    params: JsonObject
    credential_bound: bool
    credential_problem: str | None
    credential_id: str | None = Field(None, description="Administrators only")
    credential_name: str | None = Field(None, description="Administrators only")
    created_at: str
    updated_at: str
    deletion: DeletionState | None = None
    revisions: list[ModelRevision] | None = None


class HarnessParameterSpec(Sparse):
    type: Literal["string", "integer", "number", "boolean"]
    description: str | None = None
    default: HarnessParamValue | None = None
    enum: list[HarnessParamValue] | None = None
    minimum: Number | None = None
    maximum: Number | None = None


class HarnessParameterSchema(Model):
    parameters: dict[str, HarnessParameterSpec] = {}


class HarnessCreate(Model):
    name: str
    version: str
    agent: str = Field(description="Executor adapter; see /admin/harness-adapters")
    upstream_version: str | None = Field(
        None,
        description="Exact version of the program the adapter installs; required exactly "
        "when the adapter has a package",
    )
    protocols: list[Protocol]
    parameter_schema: HarnessParameterSchema = HarnessParameterSchema()
    description: str = ""


class Harness(Model):
    id: str
    name: str
    version: str
    agent: str
    upstream_version: str | None = Field(
        description="Exact version of the program installed in the task container"
    )
    description: str
    protocols: list[Protocol]
    parameter_schema: HarnessParameterSchema
    content_digest: str
    enabled: bool
    created_at: str
    deletion: DeletionState | None = None


class HarnessAdapter(Model):
    agent: str
    protocols: list[Protocol]
    parameters: list[str] | None = Field(
        None, description="Parameters a harness version may declare; null means any"
    )
    model_parameters: list[str] | None = Field(
        None, description="Model configuration parameters it passes on; null means any"
    )
    package: str | None = Field(
        None, description="npm package it installs at a harness version's upstream version"
    )
    tested_version: str | None = Field(
        None, description="Upstream version this Maestro was smoke-tested with"
    )


class UpstreamRelease(Model):
    version: str
    published_at: str | None


class UpstreamTag(UpstreamRelease):
    tag: str


class UpstreamVersions(Model):
    agent: str
    package: str
    tested_version: str | None
    registry: str | None = Field(description="Registry asked (the setup network's npm mirror)")
    checked_at: str | None
    tags: list[UpstreamTag] = Field(description="Release tags (stable, latest)")
    recent: list[UpstreamRelease] = Field(description="Newest releases first")
    error: str | None = Field(description="Why the registry could not be asked")


class EnabledChange(Model):
    enabled: bool


class AttachmentFile(Model):
    path: str
    size: int
    sha256: str
    executable: bool


class AttachmentFileEntry(Model):
    path: str = Field(description="Relative, made of letters, digits and ._+@-")
    sha256: str = Field(description="Lowercase hex")
    executable: bool


class AttachmentPublish(Model):
    files: list[AttachmentFileEntry] = Field(
        description="Every file of the snapshot; upload missing contents first"
    )
    label: str | None = Field(
        None, description="Unique per attachment name; never moves to other content"
    )
    parent_id: str | None = Field(None, description="The snapshot this one was made from")


class AttachmentContents(Model):
    sha256: list[str]


class AttachmentMissing(Model):
    missing: list[str] = Field(description="Contents to upload before publishing")


class Attachment(Model):
    id: str
    name: str
    label: str | None
    parent_id: str | None
    description: str
    owner_id: str
    owner_username: str | None
    content_digest: str = Field(
        description="Identifies the snapshot: every file's path, SHA-256 and mode"
    )
    effects: list[str]
    compatible_harnesses: list[str] = Field(description='Harness agents, or ["*"] for any')
    provides: AttachmentProvides
    has_bootstrap: bool = Field(description="Has bootstrap.sh, run in each trial's setup")
    file_count: int
    size_bytes: int
    enabled: bool
    created_at: str
    files: list[AttachmentFile] | None = None
    deletion: DeletionState | None = None


class AttachmentFileChange(Model):
    path: str
    before: AttachmentFile
    after: AttachmentFile


class AttachmentDiff(Model):
    attachment_id: str
    base_id: str | None = Field(description="Compared with; unset when there is no parent")
    added: list[AttachmentFile]
    removed: list[AttachmentFile]
    changed: list[AttachmentFileChange]


class PolicySetting(Model):
    name: str
    group: str
    description: str
    built_in: int
    min: int
    max: int


class TeamPolicy(Model):
    revision: int
    values: dict[str, int]
    settings: list[PolicySetting]


class TeamPolicyUpdate(Model):
    values: dict[str, int]
    expected_revision: int


class TeamPolicyRevision(Model):
    revision: int
    values: dict[str, int]
    created_by: str
    created_by_username: str
    created_at: str


class SetupNetworkFields(Model):
    proxy: str = Field(
        "",
        description="http(s)://host:port for setup work, reachable from containers; empty is off",
    )
    no_proxy: str = Field("", description="Comma-separated, no spaces")
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Variables for setup in the task container (package mirrors); no secrets",
    )
    harness_setup_timeout_sec: int | None = Field(
        None, description="The harness's setup time limit; none keeps the harness's own"
    )


class SetupNetwork(SetupNetworkFields):
    revision: int = Field(description="0 is nothing set")
    note: str
    created_by_username: str | None
    created_at: str | None
    warnings: list[str] = Field(description="Valid but probably not what was meant")


class SetupNetworkUpdate(SetupNetworkFields):
    note: str = ""
    expected_revision: int


class DeploymentSettings(Model):
    """What ``config.toml`` and the service environment set; read only on the web."""

    values: JsonObject = Field(description="config.toml settings in force, with defaults")
    retired: list[str] = Field(description="Keys in config.toml that are no longer used")
    service_proxy: bool = Field(description="The service process has a proxy in its environment")


class RunAccepted(Model):
    run_id: str
    operation_id: str
    state: str
    config_digest: str
    snapshot_digest: str
    planned_trials: int
    excluded_tasks: list[ExcludedTask]
    cache_observations: list[CacheObservation]
    links: dict[str, str]


class RunCounts(Model):
    planned: int
    terminal: int
    scored: int
    errors: int
    not_started: int
    cancelled: int
    interrupted: int
    active: int
    queued: int


RunStateName = Literal[
    "queued", "preparing", "running", "finalizing", "finished", "cancelled", "interrupted"
]


class Owner(Model):
    username: str
    display_name: str
    deleted: bool = Field(description="The account was deleted; history keeps its name")


class RunScoring(Model):
    """Run-list subset of the summary: a mean is never shown without its coverage."""

    metric_id: str
    planned_trials: int
    scored_trials: int
    excluded_error_trials: int
    unfinished_trials: int
    passed_trials: int | None
    mean: float | None = Field(description="Mean of valid scores only; null with no sample")
    stddev: float | None = Field(description="Sample standard deviation; null below 2 samples")
    pass_rate: float | None
    scored_coverage: float | None = Field(description="scored_trials / planned_trials")
    bench_score: float | None = Field(description="Macro mean of per-task means")


class Run(Model):
    id: str
    name: str
    owner_id: str
    owner: Owner
    pat_id: str | None
    relation: Literal["initial", "rerun", "modified"]
    source_run_id: str | None
    plan_id: str | None = None
    plan_revision: int | None = None
    batch_id: str | None = None
    dataset_version_id: str
    operation_id: str
    client_request_id: str | None
    state: RunStateName
    result_completeness: Literal["none", "partial", "complete"]
    artifact_state: Literal["pending", "saving", "complete", "partial", "failed", "expired"]
    cleanup_state: Literal["pending", "running", "complete", "failed", "not_required"]
    stop_requested: bool
    stop_reason: str | None
    queue_reason: str | None = Field(
        description="Why a queued run waits: CAPACITY_SLOTS, CAPACITY_CPU, CAPACITY_MEMORY "
        "or PROVIDER_LIMIT"
    )
    heartbeat_at: str | None = Field(description="Last sign of life from the run's Runner")
    counts: RunCounts
    scoring: RunScoring | None
    deletion: DeletionState | None = Field(description="Set when the run is in the recycle bin")
    dataset_version_deletion: DeletionState | None
    accepted_at: str
    started_at: str | None
    finished_at: str | None
    deadline_at: str
    allowed_actions: list[str]
    links: dict[str, str]


class AdmissionRecord(Model):
    """The admission and preparation a run's task was accepted with."""

    task_key: str
    admission_id: str | None
    admission_revision: int | None
    preparation_method_digest: str | None
    spec_digest: str
    expected_content_id: str | None


class ConfigDifference(Model):
    model_config = ConfigDict(json_schema_serialization_defaults_required=False)

    path: str = Field(description="JSON pointer into the effective configuration")
    before: JsonValue
    after: JsonValue
    hidden: bool | None = Field(
        None, description="true: the values are not shown to you (U-27); otherwise absent"
    )

    @model_serializer(mode="wrap")
    def _hidden_only_when_set(self, handler):
        return omit_unset(self, handler(self), "hidden")


class RunSource(Sparse):
    """Where a rerun or modified run came from (RUN-06)."""

    run_id: str
    relation: Literal["rerun", "modified"]
    snapshot_digest: str
    config_digest: str
    differences: list[ConfigDifference] | None = None
    task_causes: dict[str, list[str]] | None = Field(
        None, description="Rerun: why each chosen task of the source was not scored"
    )
    source_field_sources: dict[str, FieldSource] | None = None


class RunProvenance(Model):
    submitted_via: Literal["web_session", "pat"]
    resolution_id: str | None
    request: NormalizedRequest
    task_selection: TaskSelection
    excluded_tasks: list[ExcludedTask]
    field_sources: dict[str, FieldSource]
    baseline_differences: list[BaselineDifference]
    reference_revisions: ReferenceRevisions
    labels: dict[str, str]
    source: RunSource | None
    credential_ids: list[str] = Field(description="Bound credentials, by reference only")
    admissions: list[AdmissionRecord]


class RunPolicy(Model):
    aggregation_version: int
    policy_version: str
    team_policy_revision: int
    executor_id: str | None
    scoring: ScoringContract


class RunSnapshot(Model):
    schema_version: int
    effective: EffectiveConfig
    provenance: RunProvenance
    policy: RunPolicy
    config_digest: str
    snapshot_digest: str
    comparison_digest: str


class RunDetail(Run):
    snapshot: RunSnapshot


class TaskSummary(Model):
    task_key: str
    planned_trials: int
    scored_trials: int
    mean: float | None
    stddev: float | None
    passed_trials: int | None
    partial: bool = Field(description="Some planned repetitions have no valid score")
    excluded_trials: int
    failure_causes: list[str]


class UsageSummary(Model):
    known_trials: int
    unknown_trials: int = Field(description="Started trials that reported no usage")
    known_totals: dict[str, float]


class RunSummary(Model):
    """06 section 5. At a terminal run planned = scored + count_as_failure + excluded +
    unfinished; while running, active and queued make up the rest."""

    run_id: str
    contract: ScoringContract
    planned_trials: int
    scored_trials: int
    count_as_failure_trials: int
    excluded_error_trials: int
    unfinished_trials: int
    active_trials: int
    queued_trials: int
    passed_trials: int | None
    mean: float | None
    stddev: float | None
    pass_rate: float | None
    scored_coverage: float | None
    evaluation_coverage: float | None
    bench_task_coverage: float | None = Field(description="Run tasks / tasks in the version")
    bench_score: float | None
    bench_scored_tasks: int
    task_count: int
    result_completeness: Literal["none", "partial", "complete"]
    excluded_by_cause: dict[str, int]
    attempts: int
    retried_trials: int
    retry_count: int = Field(description="Extra attempts beyond the first, over all trials")
    usage: UsageSummary
    tasks: list[TaskSummary]


class TrialResult(Model):
    raw_score: float | None = Field(description="Verifier output; null when none")
    reward_valid: bool
    score: float | None = Field(description="Counted score; 0 is a real zero, null is unscored")
    failure_cause: str | None
    exception_type: str | None
    verifier_status: str | None
    policy_version: str
    attempt_id: str


class AttemptStep(Model):
    step: str = Field(description="starting, environment, agent, collecting or verifying")
    at: str


class AttemptError(Model):
    """Why an attempt did not produce a score, as the platform error policy classified it."""

    stage: str
    code: str
    failure_cause: str | None
    exception_type: str | None
    message: str
    reward_valid: bool | None
    retry_recommended: bool | None
    grade_requested: bool | None
    policy_version: str
    detail: JsonObject | None = None


class Attempt(Model):
    id: str
    attempt_index: int
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
    phase: str | None
    steps: list[AttemptStep] = Field(
        description="Step start times; a step ends when the next starts or the attempt ends"
    )
    executor_ref: JsonObject | None = Field(description="Executor-specific, e.g. Harbor's ids")
    setup_network_revision: int | None = Field(
        description="Setup network revision this attempt's setup was given; none for Oracle"
    )
    started_at: str
    ended_at: str | None
    error: AttemptError | None


class ArtifactCounts(Model):
    available: int
    failed: int = Field(description="Not saved, or found missing from storage")
    expired: int


class Trial(Model):
    id: str
    task_id: str
    task_key: str
    repetition_index: int
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
    phase: str | None
    reason_code: str | None
    artifact_state: Literal["pending", "saving", "complete", "partial", "failed", "expired"]
    artifact_counts: ArtifactCounts
    cleanup_state: Literal["pending", "running", "complete", "failed", "not_required"]
    result: TrialResult | None
    attempts: list[Attempt]


class Artifact(Model):
    id: str
    run_id: str
    trial_id: str | None
    attempt_id: str | None
    kind: str
    relative_path: str
    digest: str | None
    size_bytes: int | None
    status: Literal[
        "pending", "available", "failed", "deleting", "expired", "unavailable", "deleted"
    ]
    status_reason: str | None = Field(
        description="e.g. STORAGE_WRITE_FAILED, STORAGE_CONTENT_MISSING, RETENTION_EXPIRED"
    )
    created_at: str
    expires_at: str | None = Field(
        description="When the file is reclaimed: trial (or run) end + TTL; null until it ends"
    )


class ExportCreate(Model):
    run_ids: list[str]
    include_artifacts: bool = True
    artifact_kinds: list[str] | None = None
    task_keys: list[str] | None = None


class ExportRequest(Model):
    run_ids: list[str]
    include_artifacts: bool
    artifact_kinds: list[str] | None
    task_keys: list[str] | None


class ExportSummary(Model):
    runs: int
    files: int
    missing: int = Field(description="Files listed in the manifest's missing section")
    warnings: int
    artifact_bytes: int
    size_bytes: int = Field(description="Size of the package")
    digest: str


class Export(Model):
    id: str
    owner_id: str
    operation_id: str
    state: Literal["queued", "building", "ready", "failed", "expired"]
    request: ExportRequest
    size_bytes: int | None
    digest: str | None
    summary: ExportSummary | None
    error: Problem | None
    created_at: str
    finished_at: str | None
    expires_at: str | None
    links: dict[str, str]


# --- comparisons, collections and analysis snapshots (06 section 8) -------------------

Dimension = Literal[
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
VariableDimension = Literal[
    "model", "harness", "attachments", "resources", "repetitions", "parameters"
]
FailureGroup = Literal[
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
Scenario = Literal["models", "harnesses", "model_harness", "attachments", "repeats", "custom"]
Evidence = Literal["available", "none", "deleted", "purged"]


class LayoutSort(Model):
    key: str
    descending: bool = False


class Layout(Model):
    mode: Literal["leaderboard", "matrix"] = Field(
        "leaderboard",
        description="leaderboard: a row per run; matrix: two varying dimensions as rows "
        "and columns",
    )
    rows: Dimension | None = None
    columns: Dimension | None = None
    sort: LayoutSort | None = None


class ComparisonSettingsInput(Model):
    """Unset fields keep the saved (or default) value."""

    exclude: list[FailureGroup] | None = Field(
        None, description="Failure groups left out; failures of other groups count as failed"
    )
    scope: Literal["common", "all"] | None = Field(
        None, description="common: tasks every run has; all: every task, no differences"
    )
    baseline_run_id: str | None = None
    layout: Layout | None = None


class ComparisonSettings(Model):
    exclude: list[FailureGroup]
    scope: Literal["common", "all"]
    baseline_run_id: str | None
    layout: Layout


class ComparisonRequest(ComparisonSettingsInput):
    run_ids: list[str] = Field(description="Runs in display order")


class ComparisonDimension(Model):
    key: Dimension
    varies: bool
    paths: list[str] = Field(description="JSON pointers where some run differs from the first")
    labels: dict[str, str] = Field(description="A short name of each run's value, by run id")


class ComparisonNotice(Sparse):
    code: str
    dimension: Dimension | None = None
    run_ids: list[str] | None = None
    common: int | None = None
    all: int | None = None


class FailureGroupCount(Model):
    key: FailureGroup
    side: Literal["external", "subject"]
    excluded: bool
    trials: int


class ComparisonCell(Model):
    planned_trials: int
    scored_trials: int
    failed_trials: int = Field(description="Failures counted as failed (0 when higher is better)")
    excluded_trials: int
    pending_trials: int
    valid_trials: int
    mean: Number | None
    passed_trials: int | None
    excluded_by_group: dict[str, int]
    failed_by_group: dict[str, int]
    trial_ids: list[str]
    scores: list[Number | None]


class ComparedTask(Model):
    task_key: str
    status: Literal["compared", "no_valid_trials", "not_common"]
    cells: dict[str, ComparisonCell | None]


class ComparisonDifference(Model):
    mean: Number | None
    pass_rate: Number | None


class ComparisonUsage(Model):
    known_trials: int
    known_totals: dict[str, Number]


class ComparedRunRow(Model):
    run_id: str
    name: str
    label: str | None
    title: str = Field(description="The run's values of the varying dimensions")
    owner_username: str | None
    state: str
    terminal: bool
    relation: str
    source_run_id: str | None
    batch_id: str | None
    accepted_at: str
    finished_at: str | None
    evidence: Evidence
    task_count: int
    compared_trials: int = Field(description="Planned trials of the compared tasks")
    valid_trials: int = Field(description="Scored plus counted-as-failed, in compared tasks")
    mean: Number | None = Field(description="Macro mean of the compared tasks' means")
    stddev: Number | None = Field(description="Sample standard deviation of the task means")
    passed_trials: int | None
    pass_rate: Number | None
    planned_trials: int
    scored_trials: int
    failed_trials: int
    excluded_trials: int
    pending_trials: int
    excluded_by_group: dict[str, int]
    failed_by_group: dict[str, int]
    usage: ComparisonUsage
    actual_models: list[str]
    difference: ComparisonDifference | None = Field(
        description="To the baseline; only for the same dataset content and scoring over "
        "common tasks"
    )


class SetAsideRun(Model):
    run_id: str
    name: str
    reason: Literal["deleted"]


class Comparison(Model):
    comparison_version: int
    grouping_version: int
    settings: ComparisonSettings
    comparable: bool = Field(description="Same dataset content and scoring contract")
    blockers: list[ComparisonNotice]
    warnings: list[ComparisonNotice]
    dimensions: list[ComparisonDimension]
    varying: list[Dimension]
    groups: list[FailureGroupCount]
    runs: list[ComparedRunRow]
    tasks: list[ComparedTask]
    common_tasks: int
    all_tasks: int
    dropped_tasks: int = Field(description="Common tasks left out: a run had no valid trial")
    set_aside: list[SetAsideRun]


class CollectionBase(Model):
    """Where the fixed values come from: a run, or a full configuration request."""

    run_id: str | None = None
    request: ConfigRequest | None = None


class CollectionCreate(Model):
    name: str
    description: str = ""
    scenario: Scenario
    variable_dimensions: list[VariableDimension] | None = Field(
        None, description="Only for the custom scenario"
    )
    base: CollectionBase
    run_ids: list[str] = []
    settings: ComparisonSettingsInput | None = None


class CollectionUpdate(Model):
    name: str | None = None
    description: str | None = None
    settings: ComparisonSettingsInput | None = None


class CollectionCopy(Model):
    """A new collection with a changed specification; matching runs are carried over."""

    name: str | None = None
    description: str | None = None
    scenario: Scenario | None = None
    variable_dimensions: list[VariableDimension] | None = None
    base: CollectionBase | None = Field(
        None, description="Required when a dimension that varied becomes fixed"
    )
    settings: ComparisonSettingsInput | None = None


class CollectionRunRef(Model):
    run_id: str
    label: str | None = None


class CollectionRunsAdd(Model):
    runs: list[CollectionRunRef]


class CollectionRunLabel(Model):
    label: str | None = None


class CollectionMember(Model):
    run_id: str
    run_name: str
    run_state: str
    label: str | None
    added_by_username: str | None
    added_at: str
    deletion: DeletionState | None


class Mismatch(Model):
    dimension: Dimension
    path: str


class NotCarried(Model):
    run_id: str
    name: str
    mismatches: list[Mismatch]


class CollectionCandidate(Model):
    run_id: str
    name: str
    state: RunStateName
    relation: str
    owner_username: str
    accepted_at: str
    model: str
    harness: str
    mismatches: list[Mismatch] = Field(description="Empty when the run can be added")


class Collection(Model):
    id: str
    name: str
    description: str
    owner_id: str
    owner_username: str | None
    scenario: Scenario
    variable_dimensions: list[VariableDimension]
    fixed: dict[str, JsonObject] = Field(description="Fixed dimensions and their values")
    fixed_labels: dict[str, str]
    template: PlanTemplate = Field(description="Fills in runs submitted from the collection")
    base_run_id: str | None
    settings: ComparisonSettings
    copied_from: str | None
    run_count: int
    created_at: str
    updated_at: str
    allowed_actions: list[str]
    members: list[CollectionMember] | None = None
    deletion: DeletionState | None = None
    not_carried: list[NotCarried] | None = Field(
        None, description="After a copy: runs left behind and what differs"
    )


class CollectionEvent(Model):
    id: str
    action: str
    run_id: str | None
    actor_username: str | None
    pat_id: str | None
    at: str
    detail: JsonObject


class SnapshotCreate(Model):
    name: str
    collection_id: str | None = None
    run_ids: list[str] | None = Field(None, description="Without a collection")
    settings: ComparisonSettingsInput | None = None
    note: str | None = None


class SnapshotSpec(Model):
    scenario: Scenario
    variable_dimensions: list[VariableDimension]
    fixed: dict[str, JsonObject]


class SnapshotNote(Model):
    id: str
    author_id: str
    author_username: str | None
    pat_id: str | None
    body: str
    created_at: str
    updated_at: str
    editable: bool


class NoteBody(Model):
    body: str


class SnapshotSummary(Model):
    id: str
    name: str
    collection_id: str | None
    owner_id: str
    owner_username: str | None
    pat_id: str | None
    run_count: int
    created_at: str
    allowed_actions: list[str]


class Snapshot(SnapshotSummary):
    settings: ComparisonSettings
    spec: SnapshotSpec | None
    result: Comparison
    evidence: dict[str, Evidence] = Field(description="Whether each run's files still open")
    notes: list[SnapshotNote]
    deletion: DeletionState | None


# --- deletion and recycle bin ---------------------------------------------------------

ObjectType = Literal[
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


class RecycleEntry(Model):
    id: str
    object_type: ObjectType
    object_id: str
    label: str = Field(description="The object's name when it was deleted")
    owner_id: str | None
    owner_username: str | None
    state: Literal["recycled", "purged", "restored"]
    deleted_by: str
    deleted_by_username: str | None
    deleted_at: str
    purged_by: str | None
    purged_at: str | None
    detail: JsonObject = Field(description="What a purge removed; depends on the object type")


class PurgeRequest(Model):
    confirm_object_id: str = Field(description="Repeat the object id: purging is irreversible")


class OperationResource(Model):
    type: str
    id: str


class OperationBase(Model):
    id: str
    resource: OperationResource | None
    owner_id: str | None
    status: Literal["accepted", "running", "succeeded", "failed", "cancelled", "interrupted"]
    phase: str | None
    error: Problem | None
    allowed_actions: list[str]
    links: dict[str, str]
    revision: int
    created_at: str
    updated_at: str
    finished_at: str | None


class RunProgress(Model):
    planned: int
    terminal: int
    scored: int
    errors: int
    active: int


class RunOperationResult(Model):
    run_id: str
    state: RunStateName
    counts: RunCounts


class RunOperation(OperationBase):
    kind: Literal["run"]
    progress: RunProgress
    result: RunOperationResult | None


class BatchProgress(Model):
    runs: int
    terminal_runs: int
    finished: int
    cancelled: int
    interrupted: int
    deleted_runs: int = Field(description="Members in the recycle bin or purged")
    planned_trials: int
    scored_trials: int
    error_trials: int


class BatchOperationResult(BatchProgress):
    batch_id: str


class BatchOperation(OperationBase):
    kind: Literal["batch"]
    progress: BatchProgress
    result: BatchOperationResult | None


class ExportProgress(Model):
    runs: int
    files: int


class ExportOperationResult(Model):
    export_id: str
    runs: int | None = None
    files: int | None = None
    missing: int | None = None
    warnings: int | None = None
    artifact_bytes: int | None = None
    size_bytes: int | None = None
    digest: str | None = None


class ExportOperation(OperationBase):
    kind: Literal["export"]
    progress: ExportProgress
    result: ExportOperationResult | None


class ImportRequest(Model):
    source_kind: str
    namespace: str
    name: str
    ref: str
    credential_id: str | None
    accept_changed_content: bool
    prepare: bool


class ImportProgress(Model):
    request: ImportRequest
    resolved_digest: str | None = None
    tasks_total: int | None = None
    tasks_done: int | None = None


class ImportResult(Sparse):
    dataset_version_id: str
    existing: bool = Field(description="The same version was imported before")
    preparation_operation_id: str | None = None


class DatasetImportOperation(OperationBase):
    kind: Literal["dataset_import"]
    progress: ImportProgress
    result: ImportResult | None


class FirstPreparationProgress(Model):
    task_ids: list[str]
    tasks_total: int
    tasks_done: int
    admitted: int | None = None
    failed: int | None = None


class FirstPreparationTask(Sparse):
    state: Literal["pending", "admitted", "first_failed", "revoked"]
    skipped: bool | None = Field(None, description="Already decided by an earlier attempt")
    content_id: str | None = None
    error: Problem | None = None


class FirstPreparationResult(Model):
    tasks: dict[str, FirstPreparationTask] = Field(description="Keyed by task id")


class FirstPreparationOperation(OperationBase):
    kind: Literal["preparation_first"]
    progress: FirstPreparationProgress
    result: FirstPreparationResult | None


class RestoreProgress(Model):
    spec_digest: str
    task_key: str
    waiters: int | None = None


class PreparedImageView(Model):
    role: str
    digest: str
    ref: str
    key: str = Field(description="The definition: identical ones share the image")
    detail: JsonObject = Field(description="How it was prepared; depends on the environment")


class PreparedOutputView(Model):
    content_id: str
    images: list[PreparedImageView]
    detail: JsonObject


class RestoreResult(Model):
    output: PreparedOutputView
    waiters: int


class RestoreOperation(OperationBase):
    kind: Literal["preparation_restore"]
    progress: RestoreProgress
    result: RestoreResult | None


class NoProgress(Model):
    pass


class EvictionOperation(OperationBase):
    kind: Literal["preparation_eviction"]
    progress: NoProgress
    result: None


class Operation(
    RootModel[
        Annotated[
            RunOperation
            | BatchOperation
            | ExportOperation
            | DatasetImportOperation
            | FirstPreparationOperation
            | RestoreOperation
            | EvictionOperation,
            Field(discriminator="kind"),
        ]
    ]
):
    """A long operation; ``kind`` decides the shape of ``progress`` and ``result``."""


class AuditEvent(Model):
    id: str
    actor_user_id: str | None
    pat_id: str | None
    auth_channel: str | None
    action: str
    object_type: str | None
    object_id: str | None
    outcome: str
    detail: JsonObject | None = Field(description="Depends on the action")
    request_id: str | None
    created_at: str


class HostCapacityView(Model):
    slots: int
    cpus: float
    memory_mb: int


class CapacityUse(Model):
    slots: int
    cpus: float
    memory_mb: int


class ProviderCapacity(Model):
    provider: str
    max_concurrent_trials: int | None = Field(description="null means no platform limit")
    in_use: int


class CapacityOverview(Model):
    host: HostCapacityView
    in_use: CapacityUse
    providers: list[ProviderCapacity]


class ProviderLimit(Model):
    max_concurrent_trials: int | None = Field(description="null removes the limit")


class ActionRule(Model):
    """Who may do an action: a web session (of an admin when ``role`` says so) and PATs
    of the listed scopes."""

    web_session: bool
    pat_scopes: list[Scope]
    role: Role | None = None


class AuthInfo(Model):
    pat_header: str
    session_cookie: bool
    csrf_header: str


class ExecutionInfo(Model):
    executor: str | None
    modes: list[Literal["agent", "oracle"]]
    harness_adapters: list[HarnessAdapter]
    policy_version: str
    capacity: HostCapacityView


class Capabilities(Model):
    api_version: str
    service_version: str
    schema_revision: str
    contract_digest: str = Field(
        description="sha256 of the OpenAPI document; clients generated from another one warn"
    )
    features: dict[str, bool]
    limits: dict[str, int]
    team_policy_revision: int
    auth: AuthInfo
    actions: dict[str, ActionRule]
    execution: ExecutionInfo


class Health(Model):
    status: Literal["alive", "ready", "maintenance"]
    accepting_runs: bool | None = None


class StorageThreshold(Model):
    gib: Number
    percent: Number


class StorageStatus(Model):
    level: Literal["ok", "accept_paused", "dispatch_paused"]
    free_bytes: int
    total_bytes: int
    accept_min_free: StorageThreshold
    dispatch_min_free: StorageThreshold


class SchemaState(Model):
    current: str | None
    head: str
    compatible: bool


class SystemDetail(Model):
    storage: StorageStatus
    database: SchemaState = Field(alias="schema")
    master_key_available: bool
    executor: str | None
    worker_running: bool
    needs_setup: bool | None


class SystemStatus(Health):
    detail: SystemDetail


# --- batches (04 section 4) ------------------------------------------------------


class BatchCreate(Model):
    """Runs accepted together, all or none; each costs one unit of PAT quota."""

    name: str
    runs: list[RunCreate]
    client_request_id: str | None = None


class BatchAccepted(Model):
    batch_id: str
    operation_id: str
    planned_trials: int
    runs: list[RunAccepted]
    links: dict[str, str]


class BatchPreviewRun(Sparse):
    index: int
    config_digest: str | None = None
    planned_trials: int
    labels: dict[str, str] | None = None
    blockers: list[ConfigBlocker] | None = None
    warnings: list[ConfigWarning] | None = None


class PatQuota(Model):
    needed: int
    left: int


class BatchBlocker(Sparse):
    code: str
    path: str
    limit: int | None = None
    needed: int | None = None
    left: int | None = None


class BatchPreview(Model):
    runs: list[BatchPreviewRun]
    planned_trials: int
    pat_quota: PatQuota | None
    duplicate_configs: list[str] = Field(description="Configuration digests given twice")
    blockers: list[BatchBlocker]
    can_submit: bool


class Batch(Model):
    id: str
    name: str
    owner_id: str
    owner_username: str | None
    pat_id: str | None
    client_request_id: str | None
    stop_requested: bool
    created_at: str
    status: str
    operation_id: str
    progress: BatchProgress
    runs: list[Run]
    allowed_actions: list[str]
