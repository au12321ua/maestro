"""Task material cache, task inspection and the preparation spec (02 sections 3 and 6).

Material is stored by its upstream content digest under ``<cache_dir>/tasks/<hex>``
and checked against a second digest of the local files (``tree_digest``): the
upstream identity and the unpacked material are separate facts and never swapped.

Inspection reads ``task.toml`` and the author's Compose files without Harbor: what the
platform needs before execution (resources of every environment, timeouts, author
parameters) and the normalized preparation spec whose digest is the cache key. The spec
lists every image a trial starts: the main environment, a separate verifier's own image
and each Compose service besides ``main`` (N-01).
"""

import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
import tomllib
from pathlib import Path

from maestro.boundaries.dataset_source import (
    MISMATCH,
    NOT_FOUND,
    UNSUPPORTED,
    FetchedTask,
    SourceError,
    SourceTask,
    SourceVersion,
)
from maestro.core.digests import digest_json
from maestro.execution import compose

DEFAULT_CPUS = 1
DEFAULT_MEMORY_MB = 2048
INSTRUCTION_CHARS = 4000
# Default upper bound for a task's own build_timeout_sec (``build_timeout_max_sec`` in
# config.toml). The design names no build limit (14 section 5), and Harbor's default is
# 600 s; an hour covers large images while one build cannot hold a restore slot for longer.
MAX_BUILD_TIMEOUT = 3600.0

# How the platform prepares an environment. A change here is a new preparation
# method and needs a new admission (02 section 5).
PREPARATION_METHOD = {
    "adapter": "maestro-docker-prep/2",
    "environment_abi_version": "2",
    # Reproducible build settings (M1 TECH-03): fixed timestamps, no provenance.
    "build": {"provenance": False, "source_date_epoch": 0, "rewrite_timestamp": True},
}
PREPARATION_METHOD_DIGEST = digest_json(PREPARATION_METHOD)


def image_key(entry: dict) -> str:
    """The definition of one image, without where it sits in the task: two tasks with
    identical files, options and method share the image (N-01)."""
    fields = ("source", "reference", "definition_digest", "dockerfile", "args", "target")
    return digest_json({"method": PREPARATION_METHOD_DIGEST, **{k: entry.get(k) for k in fields}})


_MATERIAL_NAME = re.compile(r"[0-9a-f]{64}")
_TEMPLATE = re.compile(r"\$\{([^}:]+)(?::-(.*))?\}")
_SENSITIVE = re.compile(r"(KEY|SECRET|TOKEN(?!S)|PASSWORD|CREDENTIAL|AUTH)", re.IGNORECASE)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Names the platform or the executor process relies on. Parameter values become the
# Runner's environment (Harbor resolves templates from it), so a task declaring one of
# these, even with only a default, is refused rather than given it.
RESERVED_PARAMETER = re.compile(
    r"(PATH|HOME|USER|SHELL|PWD|TMPDIR|TZ|LANG|LC_\w+|LD_\w+|PYTHON\w*|DOCKER_\w+|COMPOSE_\w+"
    r"|HARBOR_\w+|MAESTRO_\w+|LITELLM_\w+|SSL_CERT_\w+|(?i:https?_proxy|no_proxy|all_proxy))"
)
_COMPOSE_FILES = {"docker-compose.yaml", "docker-compose.yml", "compose.yaml", "compose.yml"}
# Variables Harbor's Docker environment fills in itself.
_INFRA = {
    "MAIN_IMAGE_NAME",
    "PREBUILT_IMAGE_NAME",
    "CONTEXT_DIR",
    "CPUS",
    "MEMORY",
    "GPUS",
    "SHM_SIZE",
    "TRIAL_ID",
    "SESSION_ID",
    "HOST_VERIFIER_LOGS_PATH",
    "HOST_AGENT_LOGS_PATH",
    "HOST_ARTIFACTS_PATH",
    "ENV_VERIFIER_LOGS_PATH",
    "ENV_AGENT_LOGS_PATH",
    "ENV_ARTIFACTS_PATH",
}


def tree_digest(root: Path) -> str:
    """Names, contents and executable bits; links and special files are rejected.

    Every entry is length-framed (name length, name, kind, executable bits, content
    length, content), so no two different trees feed the hash the same bytes. Files
    are read in chunks: task content can be large."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError(f"symlinks are unsupported in task content: {relative}")
        mode = path.stat().st_mode
        if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError(f"special files are unsupported in task content: {relative}")
        name = os.fsencode(relative)
        kind = b"f" if stat.S_ISREG(mode) else b"d"
        digest.update(len(name).to_bytes(8, "big") + name + kind + bytes([mode & 0o111]))
        if kind == b"f":
            with path.open("rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                digest.update(size.to_bytes(8, "big"))
                remaining = size
                while remaining:
                    chunk = handle.read(min(remaining, 1 << 20))
                    if not chunk:
                        raise ValueError(f"file changed while it was read: {relative}")
                    digest.update(chunk)
                    remaining -= len(chunk)
    return "sha256:" + digest.hexdigest()


def build_timeout(value, cap: float = MAX_BUILD_TIMEOUT) -> float | None:
    """The author's build time limit when it is usable, else None (the platform default
    applies). A build holds one of the few restore slots for as long as it runs, so a
    task may not ask for ``inf`` or for days: the limit is capped at ``cap``."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return min(float(value), cap)


def _hex(content_digest: str) -> str | None:
    algorithm, _, value = content_digest.partition(":")
    if algorithm != "sha256" or len(value) != 64 or not value.isalnum():
        return None
    return value.lower()


class TaskMaterials:
    def __init__(self, cache_dir: Path):
        self.root = cache_dir / "tasks"

    def staging(self) -> Path:
        """A fresh directory on the cache filesystem for one fetch."""
        self.root.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=".fetch-", dir=self.root))

    def path_for(self, content_digest: str) -> Path | None:
        value = _hex(content_digest)
        if value is None:
            return None
        path = self.root / value
        return path if (path / "task.toml").is_file() else None

    def remove(self, content_digest: str) -> None:
        """Retire a task's material: renamed away first so no reader sees half a tree."""
        path = self.path_for(content_digest)
        if path is None:
            return
        retired = Path(tempfile.mkdtemp(prefix=".remove-", dir=self.root))
        os.rename(path, retired / "task")
        shutil.rmtree(retired, ignore_errors=True)

    def verified(self, content_digest: str, material_digest: str) -> Path | None:
        """The local copy, only if its files still match the recorded digest."""
        path = self.path_for(content_digest)
        if path is None:
            return None
        try:
            return path if tree_digest(path) == material_digest else None
        except (OSError, ValueError):
            return None

    def install(self, source: Path, content_digest: str, expected_material: str | None = None):
        """Copy a fetched task in under its upstream digest and return the material
        digest. A differing copy already in place is replaced by one rename."""
        value = _hex(content_digest)
        if value is None:
            raise SourceError(UNSUPPORTED, f"unsupported content digest {content_digest!r}")
        try:
            material = tree_digest(source)
        except ValueError as error:
            raise SourceError(UNSUPPORTED, str(error)) from None
        if expected_material and material != expected_material:
            raise SourceError(
                MISMATCH,
                "Task files differ from the recorded material digest",
                {"expected_digest": expected_material, "actual_digest": material},
            )
        target = self.root / value
        if self.verified(content_digest, material):
            return material
        self.root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".install-", dir=self.root))
        try:
            copy = staging / "task"
            shutil.copytree(source, copy, symlinks=True)
            if tree_digest(copy) != material:
                raise SourceError(MISMATCH, "task content changed while it was being copied")
            try:
                if target.exists():
                    os.rename(target, staging / "old")
                os.rename(copy, target)
            except OSError:
                # A concurrent install of the same digest (a double-clicked import)
                # got there first. If what it left is this very content, we are done.
                if self.verified(content_digest, material):
                    return material
                raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return material

    def sweep(self, referenced: set[str]) -> list[str]:
        """Start-up reconciliation, before any install can run: remove what a crash left
        (``.fetch-*``, ``.install-*``, ``.remove-*``) and material no task row refers
        to (a failed import copied it in). Material a task refers to stays."""
        if not self.root.is_dir():
            return []
        keep = {_hex(digest) for digest in referenced}
        removed = []
        for path in sorted(self.root.iterdir()):
            leftover = path.name.startswith((".fetch-", ".install-", ".remove-"))
            orphan = _MATERIAL_NAME.fullmatch(path.name) and path.name not in keep
            if (leftover or orphan) and path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
                removed.append(path.name)
        return removed


# --- inspection -------------------------------------------------------------------


def _env_maps(value, where=""):
    """Every ``env`` table of task.toml with its location, metadata excluded."""
    if isinstance(value, dict):
        for name, child in value.items():
            if name in {"metadata", "task"}:
                continue
            path = f"{where}.{name}" if where else name
            if name == "env" and isinstance(child, dict):
                yield path, child
            else:
                yield from _env_maps(child, path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _env_maps(child, f"{where}[{index}]")


def _compose_variables(file: Path) -> list[tuple[str, bool, str]]:
    """``docker compose config --variables`` with an empty env file and environment.
    Only the client parses the file; nothing is started."""
    result = subprocess.run(
        ["docker", "compose", "--env-file", "/dev/null", "-f", str(file), "config", "--variables"],
        cwd=file.parent,
        env={"PATH": os.defpath + ":/usr/local/bin", "HOME": "/nonexistent"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    lines = result.stdout.splitlines()
    if result.returncode or not lines or not lines[0].startswith("NAME"):
        raise ValueError("compose variable discovery failed")
    header = lines[0]
    required_at = header.index("REQUIRED")
    default_at = header.index("DEFAULT VALUE")
    alternate_at = header.index("ALTERNATE VALUE")
    output = []
    for line in lines[1:]:
        name = line[:required_at].strip()
        required = line[required_at:default_at].strip()
        if not _NAME.fullmatch(name) or required not in {"true", "false"}:
            raise ValueError("unsupported compose variable line")
        output.append((name, required == "true", line[default_at:alternate_at].strip()))
    return output


def _parameters(
    config: dict, task_dir: Path, frozen: set[str] = frozenset()
) -> tuple[list[dict], list[dict]]:
    """Author parameters (03): environment-variable templates in task.toml and the
    variables of author compose files. Variables frozen into a prepared image (``frozen``:
    used only in ``image:`` or ``build:``) are not parameters: no value could change it."""
    found: dict[str, dict] = {}
    issues: list[dict] = []

    def add(name: str, default: str | None, required: bool, used_in: str) -> None:
        if RESERVED_PARAMETER.fullmatch(name):
            issues.append({"code": "RESERVED_NAME", "name": name, "used_in": used_in})
            return
        entry = found.setdefault(
            name,
            {
                "name": name,
                "default": default,
                "required": required,
                "sensitive": bool(_SENSITIVE.search(name)),
                "description": "",
                "hard_constraints": {},
                "used_in": [],
            },
        )
        entry["required"] = entry["required"] or required
        if entry["default"] is None:
            entry["default"] = default
        elif default is not None and default != entry["default"]:
            issues.append({"code": "CONFLICTING_DEFAULTS", "name": name})
        entry["used_in"].append(used_in)

    for where, mapping in _env_maps(config):
        for value in mapping.values():
            match = _TEMPLATE.fullmatch(value) if isinstance(value, str) else None
            if match:
                add(match.group(1), match.group(2), match.group(2) is None, where)
    for file in sorted(task_dir.rglob("*")):
        if file.name not in _COMPOSE_FILES or not file.is_file():
            continue
        relative = file.relative_to(task_dir).as_posix()
        try:
            variables = _compose_variables(file)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            issues.append({"code": "COMPOSE_DISCOVERY_FAILED", "file": relative})
            continue
        for name, required, default in variables:
            if name in _INFRA or name.startswith("EGRESS_CONTROL_") or name in frozen:
                continue
            add(name, default or None, required, relative)
    return sorted(found.values(), key=lambda p: p["name"]), issues


def _build_entry(
    role: str, task_dir: Path, directory: Path, build: dict, where: str, frozen: dict
) -> dict:
    """A ``build:`` the platform runs itself. The context stays inside the environment
    directory and only content-deciding options are kept, frozen at their defaults."""
    if isinstance(build, str):
        build = {"context": build}
    other = sorted(set(build) - compose.FROZEN_BUILD - compose.IGNORED_BUILD)
    if other:
        raise compose.ComposeUnsupported(f"{where}: build option {other[0]} is unsupported")
    context = Path(compose.freeze(build.get("context", "."), where, frozen))
    context = (context if context.is_absolute() else directory / context).resolve()
    if not context.is_relative_to(directory.resolve()) or not context.is_dir():
        raise compose.ComposeUnsupported(f"{where}: the build context must be inside {role}")
    dockerfile = compose.freeze(build.get("dockerfile") or "Dockerfile", where, frozen)
    path = (context / dockerfile).resolve()
    if not path.is_relative_to(context) or not path.is_file():
        raise compose.ComposeUnsupported(f"{where}: the Dockerfile must be inside its context")
    raw = build.get("args") or {}
    if isinstance(raw, list):
        raw = dict(item.split("=", 1) if "=" in item else (item, None) for item in raw)
    args = {}
    for name, value in sorted(raw.items()):
        if value is None:
            raise compose.ComposeUnsupported(f"{where}: build argument {name} needs a value")
        args[str(name)] = compose.freeze(value, f"{where} args.{name}", frozen)
    target = build.get("target")
    try:
        digest = tree_digest(context)
    except ValueError as error:
        raise compose.ComposeUnsupported(f"{where}: {error}") from None
    return {
        "role": role,
        "source": "build",
        "context": context.relative_to(task_dir.resolve()).as_posix(),
        "dockerfile": path.relative_to(context).as_posix(),
        "args": args,
        "target": None if target is None else compose.freeze(target, where, frozen),
        "definition_digest": digest,
    }


def _pull_entry(role: str, reference, where: str, frozen: dict) -> dict:
    reference = compose.freeze(reference, where, frozen).strip()
    if not reference or any(c.isspace() for c in reference):
        raise compose.ComposeUnsupported(f"{where}: the image reference is not usable")
    return {"role": role, "source": "pull", "reference": reference}


def _resources(section: dict) -> dict:
    return {
        "cpus": section.get("cpus") or DEFAULT_CPUS,
        "memory_mb": section.get("memory_mb") or DEFAULT_MEMORY_MB,
    }


def _images(config: dict, task_dir: Path) -> tuple[list[dict], list[dict], set[str], dict]:
    """Every image a trial of this task starts (N-01): (images, the containers besides
    the main one with their resources, Compose variables frozen into images, reasons the
    task cannot be prepared as {code: message})."""
    images: list[dict] = []
    environments: list[dict] = []
    frozen: dict[str, str] = {}
    elsewhere: set[str] = set()
    reasons: dict[str, str] = {}
    environment = config.get("environment") or {}

    def services(directory: Path) -> None:
        prefix = directory.name
        try:
            document = compose.read(directory)
        except (compose.ComposeUnsupported, OSError, subprocess.TimeoutExpired) as error:
            reasons.setdefault("compose", str(error) or f"{prefix}: compose file unreadable")
            return
        if document is None:
            return
        for kind in ("networks", "volumes"):
            for name, settings in (document.get(kind) or {}).items():
                if isinstance(settings, dict) and (
                    settings.get("external") or settings.get("name")
                ):
                    reasons.setdefault(
                        "compose_external_resources",
                        f"{prefix}/{compose.COMPOSE_FILE} {kind}.{name}: external or named "
                        "resources are unsupported",
                    )
        for name, service in sorted((document.get("services") or {}).items()):
            service = service if isinstance(service, dict) else {}
            where = f"{prefix}/{compose.COMPOSE_FILE} services.{name}"
            content = {"image", "build", "cpus", "mem_limit", "deploy"}
            elsewhere.update(
                compose.variables({k: v for k, v in service.items() if k not in content})
            )
            if name == compose.MAIN:
                if "image" in service or "build" in service:
                    reasons.setdefault(
                        "compose_main_image", f"{where}: main's image comes from the task"
                    )
                continue
            role = f"{prefix}/{name}"
            try:
                if "build" in service:
                    images.append(
                        _build_entry(role, task_dir, directory, service["build"], where, frozen)
                    )
                elif "image" in service:
                    images.append(_pull_entry(role, service["image"], where, frozen))
                else:
                    raise compose.ComposeUnsupported(f"{where}: neither image nor build")
                limits = {
                    key: compose.freeze(value, where, frozen) if isinstance(value, str) else value
                    for key, value in service.items()
                    if key in {"cpus", "mem_limit"}
                }
                deploy = service.get("deploy")
                if deploy is not None:
                    limits["deploy"] = json.loads(compose.freeze(json.dumps(deploy), where, frozen))
                environments.append({"role": role, **compose.limits(limits, where)})
            except compose.ComposeUnsupported as error:
                reasons.setdefault("compose_service", str(error))

    env_dir = task_dir / "environment"
    if environment.get("docker_image"):
        try:
            images.append(
                _pull_entry("environment/main", environment["docker_image"], "task.toml", frozen)
            )
        except compose.ComposeUnsupported as error:
            reasons.setdefault("environment_image", str(error))
    elif (env_dir / "Dockerfile").is_file():
        images.append(
            _build_entry(
                "environment/main", task_dir, env_dir, {"context": "."}, "environment", frozen
            )
        )
    else:
        reasons.setdefault(
            "no_environment_definition",
            "the task has neither environment/Dockerfile nor docker_image",
        )
    if env_dir.is_dir():
        services(env_dir)

    verifier = config.get("verifier") or {}
    separate = verifier.get("environment_mode") == "separate" or (
        verifier.get("environment_mode") is None and verifier.get("environment") is not None
    )
    steps = [s for s in config.get("steps") or [] if isinstance(s, dict)]
    step_separate = any(
        (s.get("verifier") or {}).get("environment_mode") == "separate"
        or (s.get("verifier") or {}).get("environment") is not None
        for s in steps
    )
    if step_separate or (separate and steps):
        reasons.setdefault(
            "separate_verifier_steps", "separate verifiers of multi-step tasks are unsupported"
        )
    elif separate:
        # Harbor's order: the verifier's own image, then a definition under tests/, else a
        # fresh container of the main environment.
        tests = task_dir / "tests"
        own = verifier.get("environment") or {}
        if own.get("docker_image"):
            try:
                images.append(
                    _pull_entry("tests/main", own["docker_image"], "[verifier.environment]", frozen)
                )
            except compose.ComposeUnsupported as error:
                reasons.setdefault("verifier_image", str(error))
            services(tests)
        elif any((tests / name).is_file() for name in ("Dockerfile", compose.COMPOSE_FILE)):
            if (tests / "Dockerfile").is_file():
                images.append(
                    _build_entry("tests/main", task_dir, tests, {"context": "."}, "tests", frozen)
                )
            else:
                reasons.setdefault(
                    "verifier_definition", "tests/docker-compose.yaml without tests/Dockerfile"
                )
            services(tests)
        environments.append({"role": "verifier", **_resources(own or environment)})
    return images, environments, set(frozen) - elsewhere, reasons


def inspect_task(
    task_dir: Path,
    content_digest: str,
    material_digest: str,
    build_timeout_max: float = MAX_BUILD_TIMEOUT,
) -> dict:
    """The task definition the platform stores (02 section 3 step 3). The build limit it
    records is the task's own, capped at ``build_timeout_max``."""
    try:
        config = tomllib.loads((task_dir / "task.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise SourceError(UNSUPPORTED, f"task.toml cannot be read: {error}") from None
    environment = config.get("environment") or {}
    unsupported = []
    if environment.get("gpus"):
        unsupported.append("gpus")  # O-09: no GPU in the first release
    steps = config.get("steps") or []
    if any(isinstance(step, dict) and step.get("environment") for step in steps):
        unsupported.append("step_environments")
    images, environments, frozen, reasons = _images(config, task_dir)
    unsupported.extend(code for code in reasons if code not in unsupported)
    parameters, issues = _parameters(config, task_dir, frozen)
    build_limit = build_timeout(environment.get("build_timeout_sec"), build_timeout_max)
    instruction = task_dir / "instruction.md"
    definition = {
        "instruction": (
            instruction.read_text(errors="replace")[:INSTRUCTION_CHARS]
            if instruction.is_file()
            else ""
        ),
        "resources": {
            "cpus": environment.get("cpus") or DEFAULT_CPUS,
            "memory_mb": environment.get("memory_mb") or DEFAULT_MEMORY_MB,
            "storage_mb": environment.get("storage_mb"),
            # Author resource values are suggestions unless stated otherwise (02 section 3).
            "constraint_strength": "unknown",
            # Every container a trial starts besides the main one, with the author's limits
            # (None: the team default applies). A trial reserves their sum (U-54).
            "environments": environments,
        },
        "timeouts": {
            "agent_sec": (config.get("agent") or {}).get("timeout_sec"),
            "verifier_sec": (config.get("verifier") or {}).get("timeout_sec"),
            "build_sec": build_limit,
        },
        "network": {
            "allow_internet": environment.get("allow_internet"),
            "network_mode": environment.get("network_mode"),
        },
        "has_solution": (task_dir / "solution" / "solve.sh").is_file(),
        "parameters": parameters,
        "parameter_issues": issues,
        "unsupported": unsupported,
        "unsupported_reason": "; ".join(reasons.values()) or None,
    }
    spec = {
        "schema_version": 2,
        "task_content_digest": content_digest,
        "materialization_digest": material_digest,
        "images": images,
        "build_timeout_sec": build_limit,
        "preparation_parameters": {},
        "preparation_method_digest": PREPARATION_METHOD_DIGEST,
        "environment_abi_version": PREPARATION_METHOD["environment_abi_version"],
    }
    definition["preparation"] = {"spec": spec, "spec_digest": digest_json(spec)}
    return definition


def required_parameters(definition: dict) -> list[str]:
    """Author parameters a run configuration must give a value for: required, or
    sensitive, and without a default (03). The resolver blocks a task until it has one."""
    return [
        p["name"]
        for p in definition.get("parameters") or []
        if p["default"] is None and (p["required"] or p["sensitive"])
    ]


def configuration_blockers(definition: dict) -> list[dict]:
    """What blocks a task whatever the run configuration says."""
    if definition.get("parameter_issues"):
        return [{"code": "AUTHOR_PARAMETERS_UNRESOLVED", "issues": definition["parameter_issues"]}]
    return []


class LocalTaskSource:
    """Every subdirectory with a ``task.toml`` is one task; the version digest is the
    digest of the task list. Development and tests only."""

    kind = "local"

    def __init__(self, root: Path):
        self.root = root

    def _tasks(self) -> list[SourceTask]:
        return [
            SourceTask(task_dir.name, tree_digest(task_dir), {"path": str(task_dir)})
            for task_dir in sorted(p for p in self.root.iterdir() if (p / "task.toml").is_file())
        ]

    async def search(self, query: str, credential: str | None) -> list[dict]:
        return []

    async def list_versions(self, namespace: str, name: str, credential: str | None) -> list:
        return []

    async def describe(self, namespace: str, name: str, ref: str, credential: str | None):
        tasks = self._tasks() if self.root.is_dir() else []
        if not tasks:
            raise SourceError(NOT_FOUND, f"no task directories under {self.root}")
        resolved = digest_json([[t.task_key, t.content_digest] for t in tasks])
        if ref.startswith("sha256:") and ref != resolved:
            raise SourceError(NOT_FOUND, "local tasks no longer match this digest")
        return SourceVersion(
            namespace=namespace,
            name=name,
            upstream_ref=ref,
            ref_kind="digest" if ref.startswith("sha256:") else "tag",
            resolved_digest=resolved,
            tasks=tuple(tasks),
            description=f"Local tasks from {self.root.name}",
        )

    async def fetch(self, task: SourceTask, destination: Path, credential: str | None):
        source = Path(task.locator["path"])
        if not source.is_dir() or tree_digest(source) != task.content_digest:
            raise SourceError(NOT_FOUND, f"local task {task.task_key} changed or is gone")
        shutil.copytree(source, destination, symlinks=True)
        return FetchedTask(destination, task.content_digest)
