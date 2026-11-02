"""Platform error policy and the Harbor 0.24.0 adapter mapping.

The policy is the platform's and carries its own version; the adapter only names
which Harbor exception types fall under which policy rule. Exception names are
plain strings: the service never imports Harbor.
"""

import math
from dataclasses import dataclass

from maestro.boundaries.executor import DEFAULT_METRIC_ID, AttemptOutcome
from maestro.core.digests import digest_json

POLICY_VERSION = "platform-1"
HARBOR_VERSION = "0.24.0"


@dataclass(frozen=True)
class Rule:
    cause: str
    reward_valid: bool
    retry: bool
    grade: bool


RULES = {
    "agent_exhausted_budget": Rule("agent_exhausted_budget", True, False, True),
    "agent_refused_task": Rule("agent_refused_task", True, False, True),
    "sandbox_failed": Rule("sandbox_failed", False, True, False),
    "provider_transient": Rule("model_provider_failed", False, True, True),
    "provider_permanent": Rule("model_provider_failed", False, False, True),
    "provider_auth": Rule("model_provider_failed", False, False, False),
    "harbor_failed": Rule("harbor_failed", False, False, False),
    # An attachment's install script failed or ran out of time: neither a
    # model result nor a zero, and running it again in a clean container would not help.
    "attachment_failed": Rule("attachment_failed", False, False, False),
    "verifier_failed": Rule("verifier_failed", False, False, True),
    "unclassified": Rule("unclassified", False, True, True),
    "cancelled": Rule("cancelled", False, False, False),
}

# Exception class names raised by the locked Harbor version, read from its source.
HARBOR_EXCEPTIONS = {
    "AgentTimeoutError": "agent_exhausted_budget",
    "ContextLengthExceededError": "agent_exhausted_budget",
    "OutputLengthExceededError": "agent_exhausted_budget",
    "OutputTokenExceededError": "agent_exhausted_budget",
    "ContextWindowExceededError": "agent_exhausted_budget",
    "AgentSafetyRefusalError": "agent_refused_task",
    "EnvironmentStartTimeoutError": "sandbox_failed",
    "AgentSetupTimeoutError": "sandbox_failed",
    "SandboxBuildFailedError": "sandbox_failed",
    "HealthcheckError": "sandbox_failed",
    "MemoryLimitExceededError": "sandbox_failed",
    "SandboxLikelyOutOfMemoryError": "sandbox_failed",
    "ApiRateLimitError": "provider_transient",
    "ApiInternalServerError": "provider_transient",
    "ApiOverloadedError": "provider_transient",
    "ApiConnectionClosedError": "provider_transient",
    "ApiResponseStalledError": "provider_transient",
    "NetworkConnectionError": "provider_transient",
    "ApiError": "provider_permanent",
    "UnknownApiError": "provider_permanent",
    "ApiUsageLimitError": "provider_permanent",
    "AgentAuthenticationError": "provider_auth",
    "ModelNotFoundError": "provider_auth",
    "ApiProviderResourceNotFoundError": "provider_auth",
    "ServiceOperationsUnsupportedError": "harbor_failed",
    "MissingExtraError": "harbor_failed",
    "VerifierTimeoutError": "verifier_failed",
    "RewardFileNotFoundError": "verifier_failed",
    "RewardFileEmptyError": "verifier_failed",
    "VerifierOutputParseError": "verifier_failed",
    "AddTestsDirError": "verifier_failed",
    "DownloadVerifierDirError": "verifier_failed",
    "CancelledError": "cancelled",
    # Raised by the Runner's attachment installer (maestro_runner.agents); a copy that
    # does not match is a transfer fault and gets a clean container.
    "AttachmentInstallFailed": "attachment_failed",
    "AttachmentInstallTimeout": "attachment_failed",
    "AttachmentContentMissing": "attachment_failed",
    "AttachmentTransferFailed": "sandbox_failed",
    # Model client errors Terminus-2 lets through after its own three tries per call
    # (litellm exception class names; Terminus-2 calls the model from the Runner).
    "AuthenticationError": "provider_auth",
    "PermissionDeniedError": "provider_auth",
    "NotFoundError": "provider_auth",
    "RateLimitError": "provider_transient",
    "APIConnectionError": "provider_transient",
    "InternalServerError": "provider_transient",
    "ServiceUnavailableError": "provider_transient",
    "BadGatewayError": "provider_transient",
    "MidStreamFallbackError": "provider_transient",
    "Timeout": "provider_transient",
    "APIError": "provider_transient",
    "BadRequestError": "provider_permanent",
    "UnprocessableEntityError": "provider_permanent",
    "UnsupportedParamsError": "provider_permanent",
    "PaymentRequiredError": "provider_permanent",
    "BudgetExceededError": "provider_permanent",
    "ContentPolicyViolationError": "agent_refused_task",
}

MAPPING_ID = (
    f"harbor-{HARBOR_VERSION}/"
    + digest_json({"policy": POLICY_VERSION, "map": HARBOR_EXCEPTIONS})[7:19]
)

# Retry defaults: at most 3 attempts, 5 s then 20 s back-off.
MAX_RETRIES = 2
RETRY_MIN_WAIT_SEC = 5.0
RETRY_WAIT_MULTIPLIER = 4.0
RETRY_MAX_WAIT_SEC = 20.0


def harbor_retry_config() -> dict:
    """Job-internal retry settings handed to Harbor: everything is retried
    except what the platform policy says not to, which keeps unknown exceptions
    retryable like the ``unclassified`` rule. Harbor's own default list is not used."""
    return {
        "max_retries": MAX_RETRIES,
        "exclude_exceptions": sorted(
            name for name, rule in HARBOR_EXCEPTIONS.items() if not RULES[rule].retry
        ),
        "min_wait_sec": RETRY_MIN_WAIT_SEC,
        "wait_multiplier": RETRY_WAIT_MULTIPLIER,
        "max_wait_sec": RETRY_MAX_WAIT_SEC,
    }


def legal_score(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def primary_score(rewards, metric_id: str = DEFAULT_METRIC_ID) -> float | None:
    """The reward the run's scoring contract names. Harbor writes a lone
    unnamed verifier value as ``reward``, so only the default metric also accepts the
    only value; any other metric must be present by name."""
    if not isinstance(rewards, dict) or not rewards:
        return None
    if metric_id in rewards:
        return rewards[metric_id]
    if metric_id == DEFAULT_METRIC_ID and len(rewards) == 1:
        return next(iter(rewards.values()))
    return None


def harbor_outcome(
    exception_type: str | None,
    message: str | None,
    rewards,
    verifier_ran: bool,
    metric_id: str = DEFAULT_METRIC_ID,
) -> AttemptOutcome:
    raw = primary_score(rewards, metric_id)
    # Every reward the verifier wrote stays as evidence, not only the scored metric.
    metrics = rewards if isinstance(rewards, dict) else {}
    usable = verifier_ran and legal_score(raw)
    raw = raw if legal_score(raw) else None
    if exception_type is None:
        if usable:
            return AttemptOutcome(
                raw_score=raw, reward_valid=True, verifier_status="ok", metrics=metrics
            )
        rule = RULES["verifier_failed"]
        return AttemptOutcome(
            raw_score=raw,
            reward_valid=False,
            failure_cause=rule.cause,
            exception_type="RewardMissing",
            message="Verifier produced no usable reward",
            verifier_status="no_reward" if verifier_ran else "not_run",
            metrics=metrics,
        )
    rule = RULES[HARBOR_EXCEPTIONS.get(exception_type, "unclassified")]
    return AttemptOutcome(
        raw_score=raw,
        reward_valid=rule.reward_valid and usable,
        failure_cause=rule.cause,
        exception_type=exception_type,
        message=message,
        retry_recommended=rule.retry,
        grade_requested=rule.grade,
        verifier_status=("ok" if usable else "no_reward") if verifier_ran else "not_run",
        metrics=metrics,
    )
