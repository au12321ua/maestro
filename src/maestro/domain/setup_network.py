"""The setup network (N-08): how setup work reaches the network on this deployment.

An administrator keeps it on the admin page as numbered revisions; revision 0 (nothing
set) is in force before the first save. Setup work reads the revision in force when it
starts, not one frozen at acceptance, so a mirror that went away can be replaced for
queued runs too. It is the deployment's network condition, never part of a run's
configuration or ``config_digest``.

- ``proxy`` and ``no_proxy`` go to the platform's image builds (Docker's predefined proxy
  build arguments) and to setup in the task container: the harness's own install and
  attachment scripts. Never to the agent or the verifier.
- ``env`` (package mirrors and the like) goes to setup in the task container only: a
  Dockerfile sees just the build arguments it declares.
- ``harness_setup_timeout_sec`` replaces the harness's own setup time limit.

Nothing here may carry a credential: Harbor passes exec variables as ``docker exec -e``,
visible to every local user, and every member can read every revision.
"""

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from maestro.core.errors import FieldError, MaestroError

HARNESS_SETUP_TIMEOUT_RANGE = (30, 86_400)
NOTE_MAX = 500
VALUE_MAX = 2048
ENV_MAX = 32
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PROXY_NAMES = {"http_proxy", "https_proxy", "no_proxy", "all_proxy"}
_SECRET_WORDS = ("token", "secret", "password", "passwd", "auth", "credential", "key")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True)
class SetupNetwork:
    revision: int = 0
    proxy: str = ""
    no_proxy: str = ""
    env: dict[str, str] = field(default_factory=dict)
    harness_setup_timeout_sec: int | None = None

    def proxy_env(self) -> dict[str, str]:
        """Proxy variables in both spellings tools read; empty when off."""
        if not self.proxy:
            return {}
        env = {"HTTP_PROXY": self.proxy, "HTTPS_PROXY": self.proxy}
        if self.no_proxy:
            env["NO_PROXY"] = self.no_proxy
        return env | {k.lower(): v for k, v in env.items()}

    def container_env(self) -> dict[str, str]:
        """Everything setup in a task container is given: the proxy and ``env``."""
        return self.proxy_env() | self.env

    def warnings(self) -> list[str]:
        """Valid but probably not what was meant."""
        host = urlsplit(self.proxy).hostname if self.proxy else None
        if host in _LOOPBACK:
            note = (
                f"The proxy points at {host}: inside a container that is the container "
                "itself; use an address containers reach, such as the Docker bridge's"
            )
            return [note]
        return []


BUILT_IN = SetupNetwork()


def check(value: dict, revision: int = 0) -> SetupNetwork:
    """Validate an administrator's input (all fields, as the page sends them)."""
    errors: list[FieldError] = []
    proxy = value.get("proxy") or ""
    no_proxy = value.get("no_proxy") or ""
    env = value.get("env") or {}
    timeout = value.get("harness_setup_timeout_sec")
    if not isinstance(proxy, str):
        errors.append(FieldError("/proxy", "TYPE", "a string"))
    elif proxy:
        parts = urlsplit(proxy)
        try:
            port = parts.port
        except ValueError:
            port = None
        if parts.scheme not in {"http", "https"} or not parts.hostname or not port:
            errors.append(FieldError("/proxy", "FORMAT", "http://host:port (https also accepted)"))
        elif parts.username or parts.password:
            errors.append(
                FieldError(
                    "/proxy",
                    "CREDENTIAL",
                    "no user name or password; use a proxy that admits the host by address",
                )
            )
    if not isinstance(no_proxy, str) or any(c.isspace() for c in no_proxy):
        errors.append(FieldError("/no_proxy", "FORMAT", "a comma-separated list without spaces"))
    if not isinstance(env, dict):
        errors.append(FieldError("/env", "TYPE", "an object of strings"))
        env = {}
    elif len(env) > ENV_MAX:
        errors.append(FieldError("/env", "TOO_MANY", f"at most {ENV_MAX}", limit=ENV_MAX))
    for name, item in env.items():
        problem = _env_problem(name, item)
        if problem:
            errors.append(FieldError(f"/env/{name}", problem[0], problem[1]))
    low, high = HARNESS_SETUP_TIMEOUT_RANGE
    if timeout is not None and (type(timeout) is not int or not low <= timeout <= high):
        errors.append(
            FieldError(
                "/harness_setup_timeout_sec",
                "RANGE",
                f"an integer from {low} to {high}",
                limit=high,
            )
        )
    if errors:
        raise MaestroError(
            "SETUP_NETWORK_INVALID", "Invalid setup network", 422, field_errors=errors
        )
    return SetupNetwork(revision, proxy, no_proxy, dict(env), timeout)


def _env_problem(name: str, value) -> tuple[str, str] | None:
    lower = name.lower()
    if not _ENV_NAME.fullmatch(name):
        return "NAME", "a plain shell variable name"
    if not isinstance(value, str) or any(c in value for c in "\n\r\0"):
        return "TYPE", "a one-line string"
    if len(value) > VALUE_MAX:
        return "TOO_LONG", f"at most {VALUE_MAX} characters"
    if lower in _PROXY_NAMES:
        return "PROXY", "set the proxy in its own field"
    if lower.startswith("maestro_"):
        return "RESERVED", "MAESTRO_* is reserved for the platform"
    if any(word in lower for word in _SECRET_WORDS) or re.search(r"://[^/\s]*@", value):
        return "CREDENTIAL", "looks like a credential; setup variables are visible to local users"
    return None
