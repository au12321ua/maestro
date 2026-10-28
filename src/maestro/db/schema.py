"""Logical data dictionary of 01 section 4 as SQLAlchemy Core tables.

Migrations are the source of truth for the deployed schema; this metadata must
match the latest migration (checked by tests). Times are RFC 3339 text in UTC,
CPU in millicores, memory and disk in bytes, limits in seconds.
"""

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


def _id():
    return Column("id", String(40), primary_key=True)


def _ref(name, table, nullable=False, **kw):
    return Column(name, String(40), ForeignKey(f"{table}.id"), nullable=nullable, **kw)


def _time(name, nullable=False):
    return Column(name, String(32), nullable=nullable)


def _json(name, nullable=False):
    return Column(name, JSON, nullable=nullable)


# --- Identity (09) -------------------------------------------------------------

users = Table(
    "users",
    metadata,
    _id(),
    Column("username", String(64), nullable=False, unique=True),
    Column("display_name", String(128), nullable=False),
    Column("role", String(16), nullable=False),
    Column("status", String(16), nullable=False),
    Column("password_hash", Text, nullable=False),
    Column("revision", Integer, nullable=False),
    _ref("created_by", "users", nullable=True),
    _time("created_at"),
    _time("updated_at"),
    _time("disabled_at", nullable=True),
    CheckConstraint("role IN ('admin','member')", name="role"),
    CheckConstraint("status IN ('active','disabled')", name="status"),
)

invitations = Table(
    "invitations",
    metadata,
    _id(),
    Column("token_hash", String(80), nullable=False, unique=True),
    Column("role", String(16), nullable=False),
    Column("note", String(256), nullable=False),
    # Set for a password-reset link issued to an existing account.
    _ref("target_user_id", "users", nullable=True),
    _ref("created_by", "users"),
    _time("created_at"),
    _time("expires_at"),
    _time("used_at", nullable=True),
    _ref("used_by", "users", nullable=True),
    _time("revoked_at", nullable=True),
    CheckConstraint("role IN ('admin','member')", name="role"),
)

# Web sessions are not in 01's dictionary; 09 section 4 requires server-side
# revocation, so sessions are stored by token hash like PATs.
sessions = Table(
    "sessions",
    metadata,
    _id(),
    Column("token_hash", String(80), nullable=False, unique=True),
    _ref("user_id", "users"),
    _time("created_at"),
    _time("expires_at"),
    _time("last_seen_at"),
    _time("revoked_at", nullable=True),
    Index("ix_sessions_user", "user_id"),
)

pats = Table(
    "pats",
    metadata,
    _id(),
    _ref("owner_id", "users"),
    Column("name", String(128), nullable=False),
    Column("token_hash", String(80), nullable=False, unique=True),
    Column("token_prefix", String(32), nullable=False),
    Column("scope", String(16), nullable=False),
    _time("expires_at"),
    Column("run_limit", Integer, nullable=False),
    Column("accepted_runs", Integer, nullable=False),
    Column("revision", Integer, nullable=False),
    _time("created_at"),
    _time("last_used_at", nullable=True),
    _time("revoked_at", nullable=True),
    CheckConstraint("scope IN ('read','experiment')", name="scope"),
    CheckConstraint("run_limit >= 0 AND accepted_runs >= 0", name="counts"),
    CheckConstraint("accepted_runs <= run_limit", name="limit"),
    Index("ix_pats_owner", "owner_id", "created_at"),
)

# --- Credentials and master key (09 section 4) ---------------------------------

master_key_state = Table(
    "master_key_state",
    metadata,
    Column("singleton", Integer, primary_key=True),
    Column("generation", Integer, nullable=False),
    Column("basename", String(160), nullable=False),
    Column("kcv", String(64), nullable=False),
    _time("rotated_at", nullable=True),
    CheckConstraint("singleton = 1", name="singleton"),
    CheckConstraint("generation >= 1", name="generation"),
)

credentials = Table(
    "credentials",
    metadata,
    _id(),
    Column("name", String(128), nullable=False, unique=True),
    Column("kind", String(32), nullable=False),
    Column("provider", String(64), nullable=False),
    Column("description", String(512), nullable=False),
    Column("nonce", LargeBinary, nullable=False),
    Column("encrypted_payload", LargeBinary, nullable=False),
    Column("key_version", Integer, nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("revision", Integer, nullable=False),
    _ref("created_by", "users"),
    _time("created_at"),
    _time("updated_at"),
    CheckConstraint("kind IN ('model_api_key','hub_token','generic_secret')", name="kind"),
)

# --- Catalog and preparation (02) ----------------------------------------------

datasets = Table(
    "datasets",
    metadata,
    _id(),
    Column("source_kind", String(32), nullable=False),
    Column("namespace", String(128), nullable=False),
    Column("name", String(128), nullable=False),
    Column("description", Text, nullable=False),
    Column("enabled", Boolean, nullable=False),
    # Team source credential used to read this dataset; only the reference is stored.
    _ref("credential_id", "credentials", nullable=True),
    _time("created_at"),
    # Not unique: a purged dataset keeps its row as a receipt and may be added again.
    Index("ix_datasets_identity", "source_kind", "namespace", "name"),
)

dataset_versions = Table(
    "dataset_versions",
    metadata,
    _id(),
    _ref("dataset_id", "datasets"),
    Column("upstream_ref", String(256), nullable=False),
    Column("ref_kind", String(16), nullable=False),
    Column("resolved_digest", String(80), nullable=False),
    Column("materialization_digest", String(80), nullable=True),
    _json("manifest_json"),
    Column("status", String(16), nullable=False),
    _ref("created_by", "users", nullable=True),
    _ref("operation_id", "operations", nullable=True),
    _time("created_at"),
    # Not unique for the same reason; imports check it in their write transaction.
    Index("ix_dataset_versions_digest", "dataset_id", "resolved_digest"),
)

tasks = Table(
    "tasks",
    metadata,
    _id(),
    _ref("dataset_version_id", "dataset_versions"),
    Column("task_key", String(256), nullable=False),
    _json("definition_json"),
    # Upstream content identity (Hub task digest) and the digest of the local files.
    Column("content_digest", String(80), nullable=False),
    Column("material_digest", String(80), nullable=False),
    # Preparation spec of this task (02 section 6); the cache key with location_id.
    Column("spec_digest", String(80), nullable=False),
    UniqueConstraint("dataset_version_id", "task_key"),
    Index("ix_tasks_spec", "spec_digest"),
)

task_admissions = Table(
    "task_admissions",
    metadata,
    _id(),
    _ref("task_id", "tasks"),
    Column("preparation_method_digest", String(80), nullable=False),
    Column("state", String(16), nullable=False),
    Column("revision", Integer, nullable=False),
    _json("evidence_json"),
    _ref("approved_by", "users", nullable=True),
    _time("approved_at", nullable=True),
    UniqueConstraint("task_id", "preparation_method_digest"),
    Index("ix_task_admissions_task_state", "task_id", "state"),
    CheckConstraint("state IN ('pending','admitted','first_failed','revoked')", name="state"),
)

content_availability = Table(
    "content_availability",
    metadata,
    _id(),
    Column("content_digest", String(80), nullable=False),
    Column("resource_kind", String(32), nullable=False),
    Column("availability", String(16), nullable=False),
    _json("evidence_json"),
    _time("checked_at", nullable=True),
    Column("revision", Integer, nullable=False),
    UniqueConstraint("content_digest", "resource_kind"),
    CheckConstraint("availability IN ('unknown','available','unavailable')", name="availability"),
)

operations = Table(
    "operations",
    metadata,
    _id(),
    Column("kind", String(32), nullable=False),
    _ref("owner_id", "users", nullable=True),
    _ref("pat_id", "pats", nullable=True),
    Column("resource_type", String(32), nullable=True),
    Column("resource_id", String(40), nullable=True),
    Column("status", String(16), nullable=False),
    Column("phase", String(32), nullable=True),
    _json("progress_json"),
    _json("error_json", nullable=True),
    _json("result_json", nullable=True),
    Column("revision", Integer, nullable=False),
    Column("service_epoch", String(40), nullable=True),
    Column("lease_token", String(80), nullable=True),
    _time("deadline_at", nullable=True),
    _json("process_identity_json", nullable=True),
    _time("created_at"),
    _time("updated_at"),
    _time("finished_at", nullable=True),
    Index("ix_operations_owner_created", "owner_id", "created_at", "id"),
    Index("ix_operations_created", "created_at", "id"),
    Index("ix_operations_resource", "resource_type", "resource_id"),
    CheckConstraint(
        "status IN ('accepted','running','succeeded','failed','cancelled','interrupted')",
        name="status",
    ),
)

preparations = Table(
    "preparations",
    metadata,
    _id(),
    _ref("task_id", "tasks"),
    Column("spec_digest", String(80), nullable=False),
    Column("location_id", String(64), nullable=False),
    Column("cache_state", String(16), nullable=False),
    Column("generation", Integer, nullable=False),
    _ref("active_operation_id", "operations", nullable=True),
    _ref("eviction_operation_id", "operations", nullable=True),
    # What a successful preparation produced (image id, local reference, archive).
    _json("output_json", nullable=True),
    _time("prepared_at", nullable=True),
    _time("last_used_at", nullable=True),
    _json("last_restore_error_json", nullable=True),
    UniqueConstraint("spec_digest", "location_id"),
    Index("ix_preparations_state_used", "cache_state", "last_used_at"),
    CheckConstraint("cache_state IN ('absent','restoring','ready','stale')", name="cache_state"),
)

preparation_pins = Table(
    "preparation_pins",
    metadata,
    _id(),
    _ref("preparation_id", "preparations"),
    Column("holder_kind", String(16), nullable=False),
    Column("holder_id", String(40), nullable=False),
    _time("expires_at"),
    UniqueConstraint("preparation_id", "holder_kind", "holder_id"),
)

# --- Configuration (03) ---------------------------------------------------------

# Team policy (03 section 2, U-21): defaults and hard limits as numbered revisions. The
# newest revision is in force; runs record the revision their configuration used.
team_policies = Table(
    "team_policies",
    metadata,
    _id(),
    Column("revision", Integer, nullable=False, unique=True),
    _json("values_json"),
    _ref("created_by", "users"),
    _time("created_at"),
)

# N-08: the setup network an administrator keeps, one row per revision.
setup_networks = Table(
    "setup_networks",
    metadata,
    _id(),
    Column("revision", Integer, nullable=False, unique=True),
    _json("value_json"),
    Column("note", String(500), nullable=False),
    _ref("created_by", "users"),
    _time("created_at"),
)

model_configs = Table(
    "model_configs",
    metadata,
    _id(),
    # Not unique: a purged configuration frees its name (its row stays as the receipt).
    Column("name", String(128), nullable=False),
    Column("description", String(512), nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("current_revision", Integer, nullable=False),
    _ref("created_by", "users"),
    _time("created_at"),
    _time("updated_at"),
    Index("ix_model_configs_name", "name"),
)

model_revisions = Table(
    "model_revisions",
    metadata,
    _id(),
    _ref("model_config_id", "model_configs"),
    Column("revision", Integer, nullable=False),
    # Provider name for concurrency limits; protocol and endpoint say how it is called.
    Column("provider", String(64), nullable=False),
    Column("protocol", String(32), nullable=False),
    Column("endpoint", String(512), nullable=False),
    Column("model_id", String(256), nullable=False),
    _json("params_json"),
    _ref("credential_id", "credentials", nullable=True),
    _ref("created_by", "users"),
    _time("created_at"),
    UniqueConstraint("model_config_id", "revision"),
    CheckConstraint("protocol IN ('openai-compatible','openai','anthropic')", name="protocol"),
)

harness_versions = Table(
    "harness_versions",
    metadata,
    _id(),
    Column("name", String(128), nullable=False),
    Column("version", String(64), nullable=False),
    # Executor adapter that installs and drives this harness (e.g. Harbor's opencode).
    Column("agent", String(64), nullable=False),
    # Exact version of the program the adapter installs in the task container (N-09);
    # null for an adapter that installs nothing (Terminus-2 comes with the executor).
    Column("upstream_version", String(64), nullable=True),
    Column("description", String(512), nullable=False),
    # Model protocols it can speak, and the schema of its parameters.
    _json("protocols_json"),
    _json("schema_json"),
    Column("content_digest", String(80), nullable=False),
    Column("enabled", Boolean, nullable=False),
    _ref("created_by", "users"),
    _time("created_at"),
    # Not unique for the same reason as model configurations.
    Index("ix_harness_versions_name_version", "name", "version"),
)

# A published attachment package (03 section 6); its files are stored by content digest
# and never change. Name and version are unique among versions not purged (checked in
# the publishing transaction, so a purged version's receipt keeps its name).
attachment_versions = Table(
    "attachment_versions",
    metadata,
    _id(),
    _ref("owner_id", "users"),
    Column("name", String(128), nullable=False),
    Column("version", String(64), nullable=False),
    Column("description", String(512), nullable=False),
    Column("content_digest", String(80), nullable=False),
    _json("manifest_json"),
    # [{path, size, sha256, executable}] in path order.
    _json("files_json"),
    Column("file_count", Integer, nullable=False),
    Column("size_bytes", Integer, nullable=False),
    Column("has_bootstrap", Boolean, nullable=False),
    Column("enabled", Boolean, nullable=False),
    _time("created_at"),
    Index("ix_attachment_versions_name_version", "name", "version"),
)

# Which attachment versions a run loads, in load order; finds the runs using one.
run_attachments = Table(
    "run_attachments",
    metadata,
    _ref("run_id", "runs"),
    Column("position", Integer, nullable=False),
    _ref("attachment_version_id", "attachment_versions"),
    PrimaryKeyConstraint("run_id", "position"),
    Index("ix_run_attachments_version", "attachment_version_id"),
)

# A resolved configuration offered for confirmation (03 section 4). It reserves nothing;
# acceptance resolves the stored request again and refuses when the result changed.
config_resolutions = Table(
    "config_resolutions",
    metadata,
    _id(),
    _ref("owner_id", "users"),
    _ref("pat_id", "pats", nullable=True),
    _json("request_json"),
    _json("result_json"),
    Column("config_digest", String(80), nullable=True),
    _time("created_at"),
    _time("expires_at"),
    Index("ix_config_resolutions_expires", "expires_at"),
)

# --- Plans, submission and execution (04, 05) --------------------------------

# A saved, partial configuration request (04 section 1). The creator edits it, the team
# reads and copies it; a run records the revision it used and keeps its own values.
experiment_plans = Table(
    "experiment_plans",
    metadata,
    _id(),
    _ref("owner_id", "users"),
    Column("name", String(128), nullable=False),
    Column("description", String(1024), nullable=False),
    Column("revision", Integer, nullable=False),
    _json("template_json"),
    _ref("copied_from", "experiment_plans", nullable=True),
    _time("created_at"),
    _time("updated_at"),
    _time("archived_at", nullable=True),
    Index("ix_experiment_plans_name", "name"),
)

idempotency_records = Table(
    "idempotency_records",
    metadata,
    _id(),
    Column("principal_id", String(40), nullable=False),
    Column("action", String(64), nullable=False),
    Column("key", String(128), nullable=False),
    Column("request_hash", String(80), nullable=False),
    Column("status_code", Integer, nullable=False),
    _json("response_json"),
    _time("created_at"),
    _time("expires_at"),
    UniqueConstraint("principal_id", "action", "key"),
    Index("ix_idempotency_expires", "expires_at"),
)

# Runs accepted together, all or none (04 section 4); each member runs on its own.
submission_batches = Table(
    "submission_batches",
    metadata,
    _id(),
    Column("name", String(256), nullable=False),
    _ref("owner_id", "users"),
    _ref("pat_id", "pats", nullable=True),
    _ref("operation_id", "operations"),
    Column("client_request_id", String(128), nullable=True),
    Column("stop_requested", Boolean, nullable=False),
    _time("created_at"),
    Index("ix_submission_batches_owner_created", "owner_id", "created_at", "id"),
)

runs = Table(
    "runs",
    metadata,
    _id(),
    Column("name", String(256), nullable=False),
    _ref("owner_id", "users"),
    _ref("pat_id", "pats", nullable=True),
    _ref("batch_id", "submission_batches", nullable=True),
    _ref("plan_id", "experiment_plans", nullable=True),
    Column("plan_revision", Integer, nullable=True),
    _ref("source_run_id", "runs", nullable=True),
    Column("relation", String(16), nullable=False),
    _ref("dataset_version_id", "dataset_versions"),
    # Model configuration and harness of an agent run, for filters (null for Oracle).
    _ref("model_config_id", "model_configs", nullable=True),
    _ref("harness_version_id", "harness_versions", nullable=True),
    _ref("operation_id", "operations"),
    Column("state", String(16), nullable=False),
    Column("stop_requested", Boolean, nullable=False),
    Column("stop_reason", String(64), nullable=True),
    Column("client_request_id", String(128), nullable=True),
    Column("concurrency", Integer, nullable=False),
    Column("artifact_state", String(16), nullable=False),
    Column("cleanup_state", String(16), nullable=False),
    Column("service_epoch", String(40), nullable=True),
    Column("lease_token", String(80), nullable=True),
    Column("queue_reason", String(64), nullable=True),
    _json("reservation_json", nullable=True),
    _json("runner_json", nullable=True),
    _time("heartbeat_at", nullable=True),
    _time("accepted_at"),
    _time("started_at", nullable=True),
    _time("finished_at", nullable=True),
    _time("deadline_at"),
    Index("ix_runs_owner_accepted", "owner_id", "accepted_at", "id"),
    Index("ix_runs_dataset_accepted", "dataset_version_id", "accepted_at", "id"),
    Index("ix_runs_model_accepted", "model_config_id", "accepted_at", "id"),
    Index("ix_runs_accepted", "accepted_at", "id"),
    Index("ix_runs_owner_client", "owner_id", "client_request_id"),
    Index("ix_runs_state", "state"),
    CheckConstraint("relation IN ('initial','rerun','modified')", name="relation"),
    CheckConstraint(
        "state IN ('queued','preparing','running','finalizing','finished','cancelled',"
        "'interrupted')",
        name="state",
    ),
)

run_snapshots = Table(
    "run_snapshots",
    metadata,
    _ref("run_id", "runs", primary_key=True),
    Column("schema_version", Integer, nullable=False),
    _json("effective_json"),
    _json("provenance_json"),
    Column("snapshot_digest", String(80), nullable=False),
    Column("config_digest", String(80), nullable=False),
    Column("comparison_digest", String(80), nullable=False),
    _json("policy_json"),
)

trials = Table(
    "trials",
    metadata,
    _id(),
    _ref("run_id", "runs"),
    _ref("task_id", "tasks"),
    Column("repetition_index", Integer, nullable=False),
    Column("state", String(16), nullable=False),
    Column("phase", String(32), nullable=True),
    Column("reason_code", String(64), nullable=True),
    _time("execution_terminal_at", nullable=True),
    Column("result_revision", Integer, nullable=False),
    Column("artifact_state", String(16), nullable=False),
    Column("cleanup_state", String(16), nullable=False),
    UniqueConstraint("run_id", "task_id", "repetition_index"),
    Index("ix_trials_run_state", "run_id", "state"),
    CheckConstraint(
        "state IN ('queued','preparing','executing','verifying','scored','execution_error',"
        "'not_started','cancelled','interrupted')",
        name="state",
    ),
)

execution_attempts = Table(
    "execution_attempts",
    metadata,
    _id(),
    _ref("trial_id", "trials"),
    Column("attempt_index", Integer, nullable=False),
    Column("worker_id", String(64), nullable=False),
    Column("service_epoch", String(40), nullable=False),
    Column("lease_token", String(80), nullable=False),
    Column("runner_id", String(128), nullable=True),
    Column("protocol_version", Integer, nullable=True),
    _json("process_identity_json", nullable=True),
    Column("state", String(16), nullable=False),
    Column("phase", String(32), nullable=True),
    _json("steps_json", nullable=True),
    _json("executor_ref_json", nullable=True),
    # The setup network revision a model run's setup was given (N-08); None for Oracle.
    Column("setup_network_revision", Integer, nullable=True),
    _time("started_at"),
    _time("ended_at", nullable=True),
    _json("error_json", nullable=True),
    UniqueConstraint("trial_id", "attempt_index"),
)

work_items = Table(
    "work_items",
    metadata,
    _id(),
    _ref("trial_id", "trials", unique=True),
    Column("state", String(16), nullable=False),
    _time("eligible_at"),
    Column("enqueue_seq", Integer, nullable=False),
    Column("lease_token", String(80), nullable=True),
    Index("ix_work_items_state", "state", "eligible_at", "enqueue_seq"),
)

preparation_waiters = Table(
    "preparation_waiters",
    metadata,
    _id(),
    _ref("operation_id", "operations"),
    # D-020: runs wait before their attempts exist; attempt_id is for trial-level work.
    _ref("attempt_id", "execution_attempts", nullable=True),
    # A waiter is a run, or the administrator's first-preparation Operation.
    _ref("run_id", "runs", nullable=True),
    _ref("holder_operation_id", "operations", nullable=True),
    _time("deadline_at"),
    Column("state", String(16), nullable=False),
    _time("created_at"),
    _time("ended_at", nullable=True),
    UniqueConstraint("operation_id", "run_id"),
    UniqueConstraint("operation_id", "holder_operation_id"),
    Index("ix_preparation_waiters_op_state", "operation_id", "state"),
    CheckConstraint("state IN ('waiting','released','cancelled','failed')", name="state"),
)

trial_results = Table(
    "trial_results",
    metadata,
    _ref("trial_id", "trials", primary_key=True),
    _ref("attempt_id", "execution_attempts"),
    Column("result_digest", String(80), nullable=False),
    Column("raw_score", Float, nullable=True),
    _json("metrics_json"),
    Column("verifier_status", String(32), nullable=True),
    Column("failure_cause", String(64), nullable=True),
    Column("exception_type", String(128), nullable=True),
    Column("reward_valid", Boolean, nullable=False),
    Column("grade_requested", Boolean, nullable=False),
    Column("retry_recommended", Boolean, nullable=False),
    _json("actual_model_json", nullable=True),
    _json("usage_json", nullable=True),
    Column("policy_version", String(32), nullable=False),
    _time("recorded_at"),
)

artifacts = Table(
    "artifacts",
    metadata,
    _id(),
    _ref("run_id", "runs"),
    _ref("trial_id", "trials", nullable=True),
    _ref("attempt_id", "execution_attempts", nullable=True),
    Column("kind", String(32), nullable=False),
    Column("relative_path", String(512), nullable=False),
    Column("storage_key", String(256), nullable=False, unique=True),
    Column("digest", String(80), nullable=True),
    Column("size_bytes", Integer, nullable=True),
    Column("status", String(16), nullable=False),
    # Why content is not downloadable, e.g. STORAGE_CONTENT_MISSING or DELETED.
    Column("status_reason", String(64), nullable=True),
    _time("created_at"),
    # Trial (or run) execution end + TTL; null until that end is known (07 section 2).
    _time("expires_at", nullable=True),
    Index("ix_artifacts_status_expires", "status", "expires_at"),
    Index("ix_artifacts_run", "run_id", "trial_id"),
    CheckConstraint(
        "status IN ('pending','available','failed','deleting','expired','unavailable','deleted')",
        name="status",
    ),
)

# Bounded read protection: retention skips pinned artifacts until the pin expires.
artifact_pins = Table(
    "artifact_pins",
    metadata,
    _id(),
    _ref("artifact_id", "artifacts"),
    Column("holder_kind", String(16), nullable=False),
    Column("holder_id", String(40), nullable=False),
    _time("expires_at"),
    UniqueConstraint("artifact_id", "holder_kind", "holder_id"),
    Index("ix_artifact_pins_holder", "holder_kind", "holder_id"),
)

exports = Table(
    "exports",
    metadata,
    _id(),
    _ref("owner_id", "users"),
    _ref("pat_id", "pats", nullable=True),
    _ref("operation_id", "operations"),
    _json("request_json"),
    Column("state", String(16), nullable=False),
    Column("size_bytes", Integer, nullable=True),
    Column("digest", String(80), nullable=True),
    _json("summary_json", nullable=True),
    _json("error_json", nullable=True),
    _time("created_at"),
    _time("finished_at", nullable=True),
    _time("expires_at", nullable=True),
    Index("ix_exports_owner_created", "owner_id", "created_at", "id"),
    Index("ix_exports_state", "state"),
    CheckConstraint("state IN ('queued','building','ready','failed','expired')", name="state"),
)

# --- Collections and analysis snapshots (06 section 8) ------------------------------
# A collection is a list of run references its members keep up; its specification (the
# scenario, which dimensions may vary, the fixed values) never changes after creation.
collections = Table(
    "collections",
    metadata,
    _id(),
    Column("name", String(128), nullable=False),
    Column("description", String(1024), nullable=False),
    _ref("owner_id", "users"),
    _ref("pat_id", "pats", nullable=True),
    Column("scenario", String(32), nullable=False),
    _json("variable_json"),
    _json("fixed_json"),
    # The request the fixed values came from, to fill in runs submitted from here.
    _json("template_json"),
    _ref("base_run_id", "runs", nullable=True),
    # Layout, failure groups excluded, task scope and baseline; the team edits these.
    _json("settings_json"),
    _ref("copied_from", "collections", nullable=True),
    _time("created_at"),
    _time("updated_at"),
    Index("ix_collections_name", "name"),
)

collection_members = Table(
    "collection_members",
    metadata,
    _ref("collection_id", "collections"),
    _ref("run_id", "runs"),
    Column("label", String(128), nullable=True),
    _ref("added_by", "users"),
    _ref("pat_id", "pats", nullable=True),
    _time("added_at"),
    PrimaryKeyConstraint("collection_id", "run_id"),
    Index("ix_collection_members_run", "run_id"),
)

# Who added or removed which run, renamed the collection or changed its settings.
collection_events = Table(
    "collection_events",
    metadata,
    _id(),
    _ref("collection_id", "collections"),
    Column("action", String(32), nullable=False),
    Column("run_id", String(40), nullable=True),
    _ref("actor_id", "users"),
    _ref("pat_id", "pats", nullable=True),
    _time("at"),
    _json("detail_json"),
    Index("ix_collection_events_collection_at", "collection_id", "at", "id"),
)

# A frozen comparison: the run list with copies of what identified each run, the
# settings, the specification, and the computed table. It never reads the runs again
# for its numbers, so purged runs and expired artifacts leave it intact.
analysis_snapshots = Table(
    "analysis_snapshots",
    metadata,
    _id(),
    Column("name", String(256), nullable=False),
    _ref("collection_id", "collections", nullable=True),
    _ref("owner_id", "users"),
    _ref("pat_id", "pats", nullable=True),
    _json("runs_json"),
    _json("settings_json"),
    _json("spec_json", nullable=True),
    _json("result_json"),
    _time("created_at"),
    Index("ix_analysis_snapshots_created", "created_at", "id"),
    Index("ix_analysis_snapshots_collection", "collection_id"),
)

# A conclusion written by a person or an agent, kept apart from the computed table.
snapshot_notes = Table(
    "snapshot_notes",
    metadata,
    _id(),
    _ref("snapshot_id", "analysis_snapshots"),
    _ref("author_id", "users"),
    _ref("pat_id", "pats", nullable=True),
    Column("body", Text, nullable=False),
    _time("created_at"),
    _time("updated_at"),
    Index("ix_snapshot_notes_snapshot", "snapshot_id", "created_at"),
)

# --- Deletion (07 section 8, U-08 / U-19) ------------------------------------------
# One row per deleted object: 'recycled' hides it and blocks new references; 'purged'
# means content and files are gone and the row is the deletion receipt. Restoring
# removes the row; the audit log keeps the history.
deletions = Table(
    "deletions",
    metadata,
    _id(),
    Column("object_type", String(32), nullable=False),
    Column("object_id", String(40), nullable=False),
    Column("label", String(256), nullable=False),
    _ref("owner_id", "users", nullable=True),
    Column("state", String(16), nullable=False),
    _ref("deleted_by", "users"),
    _time("deleted_at"),
    _ref("purged_by", "users", nullable=True),
    _time("purged_at", nullable=True),
    _json("detail_json"),
    UniqueConstraint("object_type", "object_id"),
    Index("ix_deletions_state_deleted", "state", "deleted_at", "id"),
    CheckConstraint("state IN ('recycled','purged')", name="state"),
)

audit_events = Table(
    "audit_events",
    metadata,
    _id(),
    _ref("actor_user_id", "users", nullable=True),
    _ref("pat_id", "pats", nullable=True),
    Column("auth_channel", String(16), nullable=True),
    Column("action", String(64), nullable=False),
    Column("object_type", String(32), nullable=True),
    Column("object_id", String(40), nullable=True),
    Column("outcome", String(16), nullable=False),
    _json("detail_json", nullable=True),
    Column("request_id", String(40), nullable=True),
    _time("created_at"),
    Index("ix_audit_object", "object_type", "object_id", "created_at"),
    Index("ix_audit_created", "created_at", "id"),
)

provider_limits = Table(
    "provider_limits",
    metadata,
    Column("provider", String(64), primary_key=True),
    Column("max_concurrent_trials", Integer, nullable=False),
    _ref("updated_by", "users"),
    _time("updated_at"),
    CheckConstraint("max_concurrent_trials >= 1", name="max_concurrent_trials"),
)

instance = Table(
    "instance",
    metadata,
    Column("singleton", Integer, primary_key=True),
    Column("instance_id", String(40), nullable=False),
    _time("created_at"),
    CheckConstraint("singleton = 1", name="singleton"),
)

service_epochs = Table(
    "service_epochs",
    metadata,
    _id(),
    _time("started_at"),
    _json("process_identity_json"),
)
