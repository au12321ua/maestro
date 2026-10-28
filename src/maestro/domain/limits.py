"""Built-in values of the team policy (D-011 initial values) and structural constants.

The team policy (U-21: PAT, run size, retention, login limits ...) lives in the database
as numbered revisions edited by administrators (``maestro.services.policy``). The values
below are revision 0, in force until an administrator saves the first revision; services
read the policy in force, not these names.

Limits that depend on the machine and its disk (artifact caps, export size, build, pull
and save time, restore slots, Hub concurrency, Runner liveness) are not here: they are
set in ``config.toml`` (``maestro.config``). Capabilities report both kinds as in force.
"""

# --- team policy, revision 0 -----------------------------------------------------------
PAT_DEFAULT_DAYS = 30
PAT_MAX_DAYS = 365
PAT_DEFAULT_RUN_LIMIT = 1000
PAT_MAX_RUN_LIMIT = 1000
INVITATION_HOURS = 72
RUN_DEADLINE_HOURS = 72
RUN_DEADLINE_HOURS_MAX = 72
RUN_MAX_TASKS = 1000
REPETITIONS_DEFAULT = 1
REPETITIONS_MAX = 20
RUN_MAX_TRIALS = 10000
RUN_DEFAULT_CONCURRENCY = 4
RUN_MAX_CONCURRENCY = 10
TRIAL_CPUS_MAX = 16
TRIAL_MEMORY_MB_MAX = 64 * 1024
AGENT_TIMEOUT_SEC_MAX = 24 * 3600
BOOTSTRAP_TIMEOUT_SEC = 600
BOOTSTRAP_TIMEOUT_SEC_MAX = 3600
RUN_MAX_ATTACHMENTS = 8
BATCH_MAX_RUNS = 100
BATCH_MAX_TRIALS = 10000
ARTIFACT_TTL_DAYS = 7
EXPORT_MAX_RUNS = 100
EXPORT_TTL_HOURS = 24
EXPORT_MAX_ACTIVE_PER_USER = 2
LOGIN_ATTEMPTS_PER_MINUTE = 10

# --- structural: fixed --------------------------------------------------------------------
IDEMPOTENCY_DAYS = 7
PAGE_DEFAULT = 50
PAGE_MAX = 200
EXPORT_PIN_MINUTES = 60
RESOLUTION_MINUTES = 10
# Attachment packages (03 section 6): the uploaded archive, and what it unpacks to.
ATTACHMENT_UPLOAD_MAX_BYTES = 256 * 1024 * 1024
ATTACHMENT_UNPACKED_MAX_BYTES = 1024 * 1024 * 1024
ATTACHMENT_MAX_FILES = 10000
# Runs compared at once, and runs one collection holds (06 section 8).
COMPARISON_MAX_RUNS = 50
# Recent runs a collection offers to add, each checked against its fixed values.
COLLECTION_CANDIDATES = 200

STRUCTURAL = (
    "IDEMPOTENCY_DAYS",
    "PAGE_DEFAULT",
    "PAGE_MAX",
    "EXPORT_PIN_MINUTES",
    "RESOLUTION_MINUTES",
    "ATTACHMENT_UPLOAD_MAX_BYTES",
    "ATTACHMENT_UNPACKED_MAX_BYTES",
    "ATTACHMENT_MAX_FILES",
    "COMPARISON_MAX_RUNS",
)


def structural() -> dict:
    return {name.lower(): globals()[name] for name in STRUCTURAL}
