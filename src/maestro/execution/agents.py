"""Model protocols and harness adapters shared by configuration and executors (03 section 2).

A model configuration says how a model is called: the protocol (wire format), the
endpoint and the model id. A harness version is the program that drives the model
through a task; executors implement harnesses as adapters and declare which adapters,
and which protocols per adapter, they can run.
"""

import math
import re
from urllib.parse import urlparse

PROTOCOLS = {
    "openai-compatible": "OpenAI Chat Completions compatible (vLLM, SGLang, gateways)",
    "openai": "OpenAI Responses",
    "anthropic": "Anthropic Messages",
}

PARAM_TYPES = {"string", "integer", "number", "boolean"}
# The program a harness version installs is pinned exactly: a run must install the same
# one again (N-09). Not "latest", a tag or a range.
UPSTREAM_VERSION = re.compile(r"\d+(\.\d+){1,3}([-+][0-9A-Za-z.-]+)?")

# Generation-length settings look like secrets by name but are not (dev ``reject_secrets``).
_LENGTH_KEYS = {
    "max_tokens",
    "max_output_tokens",
    "max_completion_tokens",
    "maxTokens",
    "maxOutputTokens",
    "maxCompletionTokens",
}
_SECRET_KEY = re.compile(r"(password|secret|token|api.?key|authorization|cookie)", re.IGNORECASE)
_SECRET_VALUE = re.compile(
    r"(sk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}|\bBearer\s+\S{8,}|\bAKIA[0-9A-Z]{16}\b)"
)
PARAMS_MAX_BYTES = 16 * 1024


def secret_problems(value, path: str = "") -> list[str]:
    """JSON pointers of values that look like credentials. Configuration is shown to the
    whole team and frozen into snapshots, so credentials only go in by reference."""
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            here = f"{path}/{key}"
            if key in _LENGTH_KEYS and type(child) is int:
                continue
            if _SECRET_KEY.search(str(key)):
                found.append(here)
                continue
            found.extend(secret_problems(child, here))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(secret_problems(child, f"{path}/{index}"))
    elif isinstance(value, str):
        if _SECRET_VALUE.search(value):
            found.append(path or "/")
        elif "://" in value:
            url = urlparse(value)
            if url.username or url.password:
                found.append(path or "/")
    return found


def endpoint_problem(endpoint) -> str | None:
    if not isinstance(endpoint, str) or not 1 <= len(endpoint) <= 512:
        return "LENGTH"
    try:
        url = urlparse(endpoint)
        url.port  # noqa: B018 - raises for an invalid port
    except ValueError:
        return "INVALID"
    if url.scheme not in {"http", "https"} or not url.hostname:
        return "SCHEME"
    # Credentials or parameters in the URL would be shown to everyone (03 section 2).
    if url.username or url.password or url.query or url.fragment or url.params:
        return "CREDENTIALS_IN_URL"
    if _SECRET_VALUE.search(url.path):
        return "CREDENTIALS_IN_URL"
    return None


def endpoint_host(endpoint: str) -> str:
    return urlparse(endpoint).hostname or ""


def param_problem(spec: dict, value) -> str | None:
    """Check one harness parameter value against its schema entry."""
    kind = spec.get("type")
    if kind == "string" and not isinstance(value, str):
        return "TYPE"
    if kind == "boolean" and not isinstance(value, bool):
        return "TYPE"
    if kind == "integer" and type(value) is not int:
        return "TYPE"
    if kind == "number" and (
        isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value)
    ):
        return "TYPE"
    if "enum" in spec and value not in spec["enum"]:
        return "ENUM"
    if kind in {"integer", "number"}:
        if "minimum" in spec and value < spec["minimum"]:
            return "RANGE"
        if "maximum" in spec and value > spec["maximum"]:
            return "RANGE"
    if kind == "string" and len(value) > 4096:
        return "LENGTH"
    return None
