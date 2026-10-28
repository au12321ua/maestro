"""Model harness adapters: platform agent input -> Harbor ``AgentConfig`` (03 section 2).

Credentials never travel through Harbor's configuration: Harbor writes the job and trial
configuration (including agent kwargs) into the trial directory, which becomes evidence.
They are not put in the Runner's environment either, where an author's compose file
could reference them. The service sends them on stdin; they stay in ``SECRETS`` and the
adapter's agent class hands them to the model client in memory.

Terminus-2 is Harbor's own agent: the model is called from this process and only shell
commands run in the task container, so the container needs no route to the model.

OpenCode, Codex and Claude Code run inside the task container and call the model from
there, so the agent phase may reach the model's host only (the executor's network plan).
Their key reaches the container as a root-owned file just before the agent runs and is
removed right after, before verification: never a command-line argument (``docker exec
-e`` would show it to every host user) or an environment variable of the Runner. The
agent process can read it while it works; that is what calling the model from the
container means. OpenCode reads the file through its config, Claude Code through an
``apiKeyHelper`` that prints it, and Codex gets its ``auth.json`` copied in the same way.
Each is installed in setup with npm, with the deployment's setup network (proxy, package
mirrors) when it has one, at the exact upstream version of the harness version.

Attachments (03 section 7) are installed by the same agent class during agent setup,
before the agent's own setup: each package is copied into this trial's container with
Harbor's file transfer (no host directory is mounted), compared file by file with the
published content, and its ``bootstrap.sh`` runs under a time limit enforced inside the
container (Harbor's exec timeout only stops the local client, M1). Setup runs as the
task's agent user with the environment's setup network; the agent and verification
phases switch to the platform's restrictions afterwards, so nothing can be fetched late.
A failure ends this trial only.
"""

import functools
import hashlib
import json
import math
import os
import re
import shlex
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

SECRETS: dict[str, str] = {}
# Set by the job: packages in load order ({name, version, content_digest, path, bootstrap,
# files}), the install time limit of one trial, and the callback the job reports through.
ATTACHMENTS: list[dict] = []
INSTALL: dict[str, Any] = {"timeout_sec": None, "report": None, "env": {}}
TARGET = "/opt/maestro/attachments"
LOG_DIR = "/logs/agent/attachments"
# Time to copy and check one package, on top of the install time limit.
TRANSFER_SEC = 120

# Model configuration parameters Terminus-2 takes as its own options; the rest go to
# every model call (litellm drops what a provider does not support).
_OPTION_PARAMS = {"temperature", "reasoning_effort", "max_thinking_tokens"}
# Harness parameters an administrator may expose for Terminus-2.
TERMINUS_PARAMETERS = {
    "max_turns",
    "parser_name",
    "enable_summarize",
    "proactive_summarization_threshold",
    "record_terminal_session",
    "tmux_pane_width",
    "tmux_pane_height",
    "interleaved_thinking",
}
# Harnesses installed in the task container: the npm package of each, and the version
# this Runner was last smoke-tested with against a real model (N-09; the service shows
# it next to upstream versions). A run always names an exact version, never "latest":
# it must be reproducible from its snapshot.
PACKAGES = {
    "opencode": "opencode-ai",
    "codex": "@openai/codex",
    "claude-code": "@anthropic-ai/claude-code",
}
TESTED = {"opencode": "1.18.35", "codex": "0.165.0", "claude-code": "2.1.308"}
EXACT_VERSION = re.compile(r"\d+(\.\d+){1,3}([-+][0-9A-Za-z.-]+)?")
MODEL_KEY = "/run/maestro/model-key"
ADAPTERS = {
    "terminus-2": {
        "protocols": ("anthropic", "openai", "openai-compatible"),
        "parameters": TERMINUS_PARAMETERS,
        "model_parameters": None,
    },
    "opencode": {
        "protocols": ("anthropic", "openai", "openai-compatible"),
        "parameters": set(),
        "model_parameters": None,
    },
    # Codex speaks the Responses API only; Claude Code the Messages API only.
    "codex": {
        "protocols": ("openai",),
        "parameters": set(),
        "model_parameters": {"reasoning_effort", "reasoning_summary"},
    },
    "claude-code": {
        "protocols": ("anthropic",),
        "parameters": {"max_turns"},
        "model_parameters": {"reasoning_effort", "max_thinking_tokens"},
    },
}


class InputError(ValueError):
    pass


def _litellm_model(protocol: str, model_id: str) -> str:
    if protocol == "anthropic":
        return f"anthropic/{model_id}"
    if protocol in {"openai", "openai-compatible"}:
        return f"openai/{model_id}"
    raise InputError(f"unsupported protocol {protocol!r}")


def agent_config(agent: dict) -> dict[str, Any]:
    """Keyword arguments for Harbor's ``AgentConfig`` from the platform's agent input:
    ``{"adapter", "model": {protocol, endpoint, model_id, params}, "params"}``."""
    adapter = agent.get("adapter")
    if adapter not in ADAPTERS:
        raise InputError(f"unknown harness adapter {adapter!r}")
    model = agent.get("model") or {}
    protocol = model.get("protocol")
    if protocol not in ADAPTERS[adapter]["protocols"]:
        raise InputError(f"{adapter} cannot call a {protocol!r} model")
    params = agent.get("params") or {}
    unknown = set(params) - ADAPTERS[adapter]["parameters"]
    if unknown:
        raise InputError(f"unknown {adapter} parameters: {sorted(unknown)}")
    allowed = ADAPTERS[adapter]["model_parameters"]
    if allowed is not None and set(model.get("params") or {}) - allowed:
        unknown = sorted(set(model["params"]) - allowed)
        raise InputError(f"{adapter} does not take these model parameters: {unknown}")
    if adapter in PACKAGES:
        version = agent.get("version")
        if not isinstance(version, str) or not EXACT_VERSION.fullmatch(version):
            raise InputError(f"{adapter} needs an exact upstream version, not {version!r}")
        return {
            "opencode": _opencode_config,
            "codex": _codex_config,
            "claude-code": _claude_code_config,
        }[adapter](protocol, model, params, version)
    model_params = dict(model.get("params") or {})
    kwargs: dict[str, Any] = {
        **params,
        "api_base": model["endpoint"],
        "use_responses_api": protocol == "openai",
        **{k: model_params.pop(k) for k in list(model_params) if k in _OPTION_PARAMS},
    }
    if model_params:
        kwargs["llm_call_kwargs"] = model_params
    return {
        "import_path": "maestro_runner.agents:MaestroTerminus2",
        "model_name": _litellm_model(protocol, model["model_id"]),
        "kwargs": kwargs,
    }


class AttachmentInstallFailed(RuntimeError):
    pass


class AttachmentInstallTimeout(RuntimeError):
    pass


class AttachmentTransferFailed(RuntimeError):
    pass


def describe(root: Path) -> list[list]:
    """[[path, sha256, executable]] in path order, as the service digests a package."""
    found = []
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError(f"not a regular file: {path.relative_to(root)}")
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                while chunk := handle.read(1 << 20):
                    digest.update(chunk)
            found.append(
                [
                    path.relative_to(root).as_posix(),
                    digest.hexdigest(),
                    bool(path.stat().st_mode & 0o100),
                ]
            )
    return found


def content_digest(files: list[list]) -> str:
    """The service's ``digest_json`` of the file list (canonical JSON of plain values)."""
    text = json.dumps(files, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def setup_timeout(harness_sec: float) -> float:
    """Harbor's limit for the whole agent setup: the harness's own install time, plus the
    attachments' install and copy time when there are any."""
    if not ATTACHMENTS:
        return harness_sec
    return harness_sec + (INSTALL["timeout_sec"] or 0) + TRANSFER_SEC * len(ATTACHMENTS)


def _in_container(output: str) -> list[list]:
    """Parse ``sha256sum`` lines, a separator, then the executable paths."""
    sums, _, executables = output.partition("--maestro--\n")
    marked = {line.removeprefix("./") for line in executables.splitlines() if line}
    found = []
    for line in sums.splitlines():
        digest, _, name = line.partition("  ")
        name = name.removeprefix("./")
        found.append([name, digest, name in marked])
    return sorted(found)


async def install(agent, environment) -> None:
    """Copy, check and install every attachment in this trial's container."""
    trial_name = Path(agent.logs_dir).parent.name
    report: Callable = INSTALL["report"] or (lambda *args, **kwargs: None)
    # The limit covers the install scripts together; copying has its own allowance
    # (``TRANSFER_SEC`` in Harbor's setup limit), so a slow copy does not eat it.
    limit = float(INSTALL["timeout_sec"] or 0)
    spent = 0.0
    user = environment.default_user
    records: list[dict] = []
    report(trial_name, None)
    try:
        await environment.exec(
            f"mkdir -p {TARGET} {LOG_DIR}"
            + (f" && chown {shlex.quote(str(user))} {LOG_DIR}" if user is not None else ""),
            user="root",
        )
        for index, attachment in enumerate(ATTACHMENTS):
            target = f"{TARGET}/{index:02d}-{attachment['name']}"
            record = {
                "name": attachment["name"],
                "version": attachment["version"],
                "content_digest": attachment["content_digest"],
                "target": target,
                "user": None if user is None else str(user),
                # Whether the deployment's setup proxy was offered; the revision of the
                # setup network is on the attempt (N-08).
                "proxy": "HTTP_PROXY" in INSTALL["env"],
                "status": "copying",
            }
            records.append(record)
            started = time.monotonic()
            await environment.exec(f"mkdir -p {target}", user="root")
            await environment.upload_dir(attachment["path"], target)
            check = await environment.exec(
                f"cd {target} && find . -type f -print0 | xargs -0 -r sha256sum"
                " && echo --maestro-- && find . -type f -perm -u=x",
                user="root",
            )
            if check.return_code != 0 or _in_container(check.stdout or "") != sorted(
                attachment["files"]
            ):
                record["status"] = "transfer_mismatch"
                raise AttachmentTransferFailed(
                    f"the copy of {attachment['name']} in the container differs from the package"
                )
            if user is not None:
                await environment.exec(f"chown -R {shlex.quote(str(user))} {target}", user="root")
            record["copy_seconds"] = round(time.monotonic() - started, 2)
            if not attachment["bootstrap"]:
                record["status"] = "ok"
                continue
            remaining = math.ceil(limit - spent)
            log = f"{LOG_DIR}/{index:02d}-{attachment['name']}.log"
            record.update(status="installing", log=log)
            if remaining < 1:
                record["status"] = "timeout"
                raise AttachmentInstallTimeout(f"no install time left for {attachment['name']}")
            started = time.monotonic()
            result = await environment.exec(
                f"timeout -k 5 {remaining} bash bootstrap.sh > {log} 2>&1",
                cwd=target,
                env={**INSTALL["env"], "MAESTRO_ATTACHMENT_DIR": target},
                timeout_sec=remaining + 30,
            )
            spent += time.monotonic() - started
            record["install_seconds"] = round(time.monotonic() - started, 2)
            record["exit_code"] = result.return_code
            # timeout(1) exits 124 on the limit, 137 when it had to kill.
            record["status"] = {0: "ok", 124: "timeout", 137: "timeout"}.get(
                result.return_code, "failed"
            )
            if record["status"] == "timeout":
                raise AttachmentInstallTimeout(
                    f"{attachment['name']} did not finish installing in {limit:g}s"
                )
            if record["status"] == "failed":
                raise AttachmentInstallFailed(
                    f"bootstrap.sh of {attachment['name']} exited with {result.return_code}"
                )
    except (AttachmentInstallFailed, AttachmentInstallTimeout, AttachmentTransferFailed):
        raise
    except Exception as error:
        if records and records[-1]["status"] == "installing":
            records[-1]["status"] = "failed"
            raise AttachmentInstallFailed(f"install could not run: {error}") from error
        if records:
            records[-1]["status"] = "transfer_failed"
        raise AttachmentTransferFailed(f"attachment transfer failed: {error}") from error
    finally:
        report(trial_name, records)


def _opencode_config(protocol: str, model: dict, params: dict, version: str) -> dict[str, Any]:
    """OpenCode reads its provider from its own config: the endpoint and a key file, and
    the model's parameters as provider options. Web tools stay off (no network for them)."""
    provider = {
        "anthropic": "anthropic",
        "openai": "openai",
        "openai-compatible": "maestro",
    }[protocol]
    settings: dict[str, Any] = {
        "options": {"baseURL": model["endpoint"], "apiKey": f"{{file:{MODEL_KEY}}}"},
        "models": {model["model_id"]: {"options": dict(model.get("params") or {})}},
    }
    if provider == "maestro":
        settings |= {"npm": "@ai-sdk/openai-compatible", "name": "maestro"}
    return {
        "import_path": "maestro_runner.agents:MaestroOpenCode",
        "model_name": f"{provider}/{model['model_id']}",
        "kwargs": {
            "version": version,
            "disable_web_search": True,
            "opencode_config": {"provider": {provider: settings}},
        },
    }


def _codex_config(protocol: str, model: dict, params: dict, version: str) -> dict[str, Any]:
    """Codex takes the endpoint from ``openai_base_url`` in its config.toml, which Harbor
    writes from ``OPENAI_BASE_URL``; the key comes as ``auth.json``. Codex passes only the
    last path segment of the model name as ``--model``, so ids with a slash are refused
    rather than silently shortened."""
    if "/" in model["model_id"]:
        raise InputError("codex cannot call a model id containing '/'")
    return {
        "import_path": "maestro_runner.agents:MaestroCodex",
        "model_name": model["model_id"],
        "env": {"OPENAI_BASE_URL": model["endpoint"]},
        "kwargs": {
            "version": version,
            "disable_web_search": True,
            **dict(model.get("params") or {}),
        },
    }


def _claude_code_config(protocol: str, model: dict, params: dict, version: str) -> dict[str, Any]:
    """Claude Code takes the endpoint from ``ANTHROPIC_BASE_URL`` and the key from an
    ``apiKeyHelper`` in its settings, which prints the key file; web tools stay off.
    Model configurations give the endpoint with its ``/v1`` (as OpenCode's Anthropic
    provider takes it); Claude Code adds ``/v1/messages`` itself."""
    base = model["endpoint"].rstrip("/").removesuffix("/v1")
    return {
        "import_path": "maestro_runner.agents:MaestroClaudeCode",
        "model_name": model["model_id"],
        "env": {"ANTHROPIC_BASE_URL": base},
        "kwargs": {
            **params,
            **dict(model.get("params") or {}),
            "version": version,
            "disable_web_search": True,
            "config": {"apiKeyHelper": f"cat {MODEL_KEY}"},
        },
    }


async def _setup_with_attachments(agent, environment, setup) -> None:
    """Attachments first, then the agent's own setup; both with the setup network."""
    with environment.scoped_exec_env(INSTALL["env"]):
        if ATTACHMENTS:
            await install(agent, environment)
        await setup(environment)


async def _upload_key(environment, content: str, target: str) -> None:
    """``content`` as a file only the agent user can read, copied in (never an argument)."""
    fd, local = tempfile.mkstemp(prefix=".key-")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        await environment.exec(f"mkdir -p {shlex.quote(os.path.dirname(target))}", user="root")
        await environment.upload_file(local, target)
    finally:
        os.unlink(local)
    user = environment.default_user
    owner = f"chown {shlex.quote(str(user))} {target} && " if user is not None else ""
    await environment.exec(f"{owner}chmod 400 {target}", user="root")


def _container_harness(base):
    """``base`` (a Harbor installed agent) with the platform's rules for harnesses that run
    in the task container: nothing from the Runner's own environment, the install skipped
    when the image already has the exact version, attachments before the harness's own
    setup, and the model key a file for the length of the agent run only."""

    class InContainer(base):
        def _env_sources(self):
            return (self._resolved_env_vars, self._extra_env)

        async def install(self, environment) -> None:
            found = await environment.exec(command=self.get_version_command() or "false")
            if found.return_code == 0 and self.parse_version(found.stdout or "") == self._version:
                return
            await self._install(environment)

        async def _install(self, environment) -> None:
            await base.install(self, environment)

        async def setup(self, environment) -> None:
            await _setup_with_attachments(self, environment, super().setup)

        async def run(self, instruction, environment, context) -> None:
            await _upload_key(environment, SECRETS.get("model_api_key", ""), MODEL_KEY)
            try:
                await super().run(instruction, environment, context)
            finally:
                # Gone before verification: the verifier runs in this container.
                await environment.exec(f"rm -f {MODEL_KEY}", user="root")

    return InContainer


@functools.cache
def _opencode():
    from harbor.agents.installed.opencode import OpenCode

    class MaestroOpenCode(_container_harness(OpenCode)):
        """OpenCode reading its key file through its provider config."""

    return MaestroOpenCode


@functools.cache
def _codex():
    from harbor.agents.installed.codex import Codex

    class MaestroCodex(_container_harness(Codex)):
        """Codex with ``auth.json`` copied in from memory instead of Harbor writing it from
        an ``OPENAI_API_KEY`` exec variable. Harbor links it into ``CODEX_HOME`` and
        removes its directory when the agent ends; the Runner removes it as well."""

        _auth_json: Path | None = None

        def _resolve_auth_json_path(self) -> Path | None:
            return self._auth_json

        async def run(self, instruction, environment, context) -> None:
            fd, local = tempfile.mkstemp(prefix=".auth-", suffix=".json")
            remote = (self._REMOTE_CODEX_SECRETS_DIR / "auth.json").as_posix()
            try:
                with os.fdopen(fd, "w") as handle:
                    json.dump({"OPENAI_API_KEY": SECRETS.get("model_api_key", "")}, handle)
                self._auth_json = Path(local)
                await Codex.run(self, instruction, environment, context)
            finally:
                self._auth_json = None
                os.unlink(local)
                await environment.exec(f"rm -f {shlex.quote(remote)}", user="root")

    return MaestroCodex


@functools.cache
def _claude_code():
    from harbor.agents.installed.claude_code import ClaudeCode
    from harbor.agents.installed.node_install import nvm_node_install_snippet

    class MaestroClaudeCode(_container_harness(ClaudeCode)):
        """Claude Code installed from npm everywhere (U-41), so the setup network's
        package mirror applies; Harbor uses the official download site on glibc, which
        only a proxy reaches. The key is read by ``apiKeyHelper``."""

        async def _install(self, environment) -> None:
            await self.ensure_system_dependencies(
                environment, ("curl", "bash", "nodejs", "npm", "procps")
            )
            await self.exec_as_agent(
                environment,
                command=(
                    "set -euo pipefail; "
                    # nvm's Node does not run on musl: use the packaged one there.
                    "if ldd --version 2>&1 | grep -qi musl || [ -f /etc/alpine-release ]; then "
                    "node --version && npm --version; "
                    f"else {nvm_node_install_snippet()}; fi && "
                    f"npm i -g {PACKAGES['claude-code']}@{self._version} && "
                    # Harbor runs ``claude`` with only ~/.local/bin added to PATH; the
                    # package's postinstall puts the native binary in place.
                    'mkdir -p "$HOME/.local/bin" && '
                    'ln -sf "$(command -v claude)" "$HOME/.local/bin/claude" && '
                    'export PATH="$HOME/.local/bin:$PATH" && claude --version'
                ),
            )

    return MaestroClaudeCode


@functools.cache
def _terminus():
    from harbor.agents.terminus_2.terminus_2 import Terminus2

    class MaestroTerminus2(Terminus2):
        """Terminus-2 whose model client gets the API key from memory, and which installs
        the run's attachments before its own setup."""

        async def setup(self, environment) -> None:
            await _setup_with_attachments(self, environment, super().setup)

        def _init_llm(self, *args, **kwargs):
            key = SECRETS.get("model_api_key")
            if key:
                if args:  # positional form: llm_kwargs is the fifth argument
                    args = list(args)
                    args[4] = {**(args[4] or {}), "api_key": key}
                else:
                    kwargs["llm_kwargs"] = {**(kwargs.get("llm_kwargs") or {}), "api_key": key}
            return super()._init_llm(*args, **kwargs)

    return MaestroTerminus2


_CLASSES = {
    "MaestroTerminus2": _terminus,
    "MaestroOpenCode": _opencode,
    "MaestroCodex": _codex,
    "MaestroClaudeCode": _claude_code,
}


def agent_class(adapter: str):
    return {
        "terminus-2": _terminus,
        "opencode": _opencode,
        "codex": _codex,
        "claude-code": _claude_code,
    }[adapter]()


def __getattr__(name: str):
    # Harbor loads the class by import path; build it only then, after Harbor imports.
    if name in _CLASSES:
        return _CLASSES[name]()
    raise AttributeError(name)
