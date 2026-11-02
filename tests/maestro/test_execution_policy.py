"""Platform error policy and the Harbor 0.24.0 mapping."""

import pytest

from maestro.execution.policy import (
    HARBOR_EXCEPTIONS,
    MAPPING_ID,
    RULES,
    harbor_outcome,
    harbor_retry_config,
)
from maestro.services.worker import trial_outcome


@pytest.mark.parametrize(
    ("exception", "rewards", "verified", "state", "cause", "valid", "retry"),
    [
        (None, {"reward": 0.0}, True, "scored", None, True, False),
        (None, {"reward": 1.0}, True, "scored", None, True, False),
        (None, None, True, "execution_error", "verifier_failed", False, False),
        (
            None,
            {"accuracy": 0.5, "f1": 0.4},
            True,
            "execution_error",
            "verifier_failed",
            False,
            False,
        ),
        (
            "AgentTimeoutError",
            {"reward": 0.0},
            True,
            "scored",
            "agent_exhausted_budget",
            True,
            False,
        ),
        (
            "AgentTimeoutError",
            None,
            False,
            "execution_error",
            "agent_exhausted_budget",
            False,
            False,
        ),
        (
            "AgentSafetyRefusalError",
            {"reward": 1.0},
            True,
            "scored",
            "agent_refused_task",
            True,
            False,
        ),
        (
            "ApiRateLimitError",
            {"reward": 1.0},
            True,
            "execution_error",
            "model_provider_failed",
            False,
            True,
        ),
        (
            "AgentAuthenticationError",
            None,
            False,
            "execution_error",
            "model_provider_failed",
            False,
            False,
        ),
        ("VerifierTimeoutError", None, True, "execution_error", "verifier_failed", False, False),
        (
            "EnvironmentStartTimeoutError",
            None,
            False,
            "execution_error",
            "sandbox_failed",
            False,
            True,
        ),
        (
            "SomethingNewError",
            {"reward": 1.0},
            True,
            "execution_error",
            "unclassified",
            False,
            True,
        ),
    ],
)
def test_harbor_outcomes(exception, rewards, verified, state, cause, valid, retry):
    outcome = harbor_outcome(exception, "message", rewards, verified)
    assert trial_outcome(outcome)[0] == state
    assert outcome.failure_cause == cause
    assert outcome.reward_valid is valid
    assert outcome.retry_recommended is retry


def test_invalid_reward_is_kept_as_evidence_only():
    outcome = harbor_outcome("ApiRateLimitError", None, {"reward": 0.75}, True)
    assert outcome.raw_score == 0.75 and not outcome.reward_valid


def test_non_finite_reward_is_not_a_score():
    outcome = harbor_outcome(None, None, {"reward": float("nan")}, True)
    assert outcome.raw_score is None and not outcome.reward_valid


def test_retry_config_is_explicit_and_follows_policy():
    config = harbor_retry_config()
    excluded = set(config["exclude_exceptions"])
    assert config["max_retries"] == 2
    for name, rule in HARBOR_EXCEPTIONS.items():
        assert (name in excluded) is (not RULES[rule].retry)
    # Unknown exceptions stay retryable, like the unclassified rule.
    assert "SomethingNewError" not in excluded


def test_mapping_id_names_harbor_version_and_digest():
    assert MAPPING_ID.startswith("harbor-0.24.0/") and len(MAPPING_ID) == len("harbor-0.24.0/") + 12
