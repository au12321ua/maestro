#!/usr/bin/env python3
"""maestroctl: install and look after one Maestro instance on a Linux host (12).

It runs with the host's ``python3`` and the standard library only, because it has to
work before any Maestro environment exists. A release package carries its own copy
next to the wheels it installs; the installed copy is linked as
``/usr/local/sbin/maestroctl`` and always belongs to the current version.

Layout (12 section 1, U-13 / U-29): the program under ``/opt/maestro`` (root owned,
read-only to the service), configuration and the master key under ``/etc/maestro``, data
under ``/var/lib/maestro``, cache under ``/var/cache/maestro``, backups under
``/var/backups/maestro``. The service runs as the ``maestro`` system account, which is
put in the group of the system Docker socket (U-29: that group is root-equivalent).

``--test-root`` (hidden) places the whole layout under a scratch directory, runs as the
calling user and registers a systemd *user* unit instead, so the installer can be
exercised without root. The unit then has no ``User=`` and no sandboxing options.
"""

import argparse
import contextlib
import grp
import hashlib
import json
import os
import pwd
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SERVICE_USER = "maestro"
PYTHON_REQUEST = ">=3.12,<3.14"
DEFAULT_PORT = 8420
READY_TIMEOUT = 180
KEEP_VERSIONS = 2
LABEL_INSTANCE = "io.maestro.instance_id"
# The local image registry prepared images are kept in (N-01, U-55): the official image,
# pinned, run by Docker. Deleting manifests is allowed so purged versions free space.
REGISTRY_IMAGE = (
    "registry:3.0.0@sha256:6c5666b861f3505b116bb9aa9b25175e71210414bd010d92035ff64018f9457e"
)
REGISTRY_CONFIG = "/etc/distribution/config.yml"
DEFAULT_REGISTRY = "127.0.0.1:8425"
# Compose overrides use !reset to drop an author's build (02 section 6).
COMPOSE_MIN = (2, 24)
RELEASE_FILE = "RELEASE.json"
COMPLETE = ".complete"


class CtlError(Exception):
    """A failure with a message meant for the administrator; exit status 1."""


def say(message: str = "") -> None:
    print(message, flush=True)


def step(message: str) -> None:
    print(f"==> {message}", flush=True)


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


# --- layout -------------------------------------------------------------------------


@dataclass(frozen=True)
class Layout:
    root: Path
    test: bool = False

    def _at(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    @property
    def opt(self) -> Path:
        return self._at("/opt/maestro")

    @property
    def versions(self) -> Path:
        return self.opt / "versions"

    @property
    def current(self) -> Path:
        return self.opt / "current"

    @property
    def harbor(self) -> Path:
        return self.opt / "harbor"

    @property
    def python_dir(self) -> Path:
        return self.opt / "python"

    @property
    def uv(self) -> Path:
        return self.opt / "bin" / "uv"

    @property
    def incoming(self) -> Path:
        return self.opt / "incoming"

    @property
    def etc(self) -> Path:
        return self._at("/etc/maestro")

    @property
    def config(self) -> Path:
        return self.etc / "config.toml"

    @property
    def environment(self) -> Path:
        return self.etc / "environment"

    @property
    def secrets(self) -> Path:
        return self.etc / "secrets"

    @property
    def record(self) -> Path:
        return self.etc / "install.json"

    @property
    def data(self) -> Path:
        return self._at("/var/lib/maestro")

    @property
    def home(self) -> Path:
        return self.data / "home"

    @property
    def cache(self) -> Path:
        return self._at("/var/cache/maestro")

    @property
    def backups(self) -> Path:
        return self._at("/var/backups/maestro")

    @property
    def registry_dir(self) -> Path:
        return self.data / "registry"

    @property
    def registry_name(self) -> str:
        if not self.test:
            return "maestro-registry"
        tag = hashlib.sha256(str(self.root).encode()).hexdigest()[:10]
        return f"maestro-registry-test-{tag}"

    @property
    def ctl_link(self) -> Path:
        return self._at("/usr/local/sbin/maestroctl")

    @property
    def unit_name(self) -> str:
        if not self.test:
            return "maestro.service"
        tag = hashlib.sha256(str(self.root).encode()).hexdigest()[:10]
        return f"maestro-test-{tag}.service"

    @property
    def unit_path(self) -> Path:
        if not self.test:
            return Path("/etc/systemd/system") / self.unit_name
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        return base / "systemd" / "user" / self.unit_name

    def removable(self) -> set[Path]:
        """The only directories this tool ever deletes as a whole."""
        return {self.opt, self.etc, self.data, self.cache, self.backups, self.incoming}


def safe_rmtree(layout: Layout, path: Path) -> None:
    """Delete one of the layout's own directories; refuse anything else, a symlink, or a
    path that resolves outside the layout root."""
    path = Path(os.path.abspath(path))
    allowed = (
        layout.removable()
        | {p for p in layout.versions.glob("*")}
        | {p for p in layout.harbor.glob("*/*")}
    )
    if path not in allowed:
        raise CtlError(f"refusing to delete {path}: not a Maestro directory")
    if path.is_symlink():
        raise CtlError(f"refusing to delete {path}: it is a symbolic link")
    if len(path.parts) < 3 or not path.resolve().is_relative_to(layout.root.resolve()):
        raise CtlError(f"refusing to delete {path}: outside the installation")
    if path.exists():
        shutil.rmtree(path)


# --- host operations ----------------------------------------------------------------


def run(cmd, *, env=None, check=True, capture=False, timeout=None, input=None, cwd="/"):
    # From "/" so that no project or virtual environment around the caller's directory
    # is picked up (uv looks for both).
    try:
        result = subprocess.run(
            [str(c) for c in cmd],
            env=env,
            cwd=cwd,
            check=False,
            text=True,
            capture_output=capture,
            timeout=timeout,
            input=input,
        )
    except FileNotFoundError:
        raise CtlError(f"command not found: {cmd[0]}") from None
    except subprocess.TimeoutExpired:
        raise CtlError(f"timed out after {timeout}s: {' '.join(map(str, cmd))}") from None
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "").strip()[-2000:] if capture else ""
        raise CtlError(
            f"{' '.join(map(str, cmd[:4]))} ... failed ({result.returncode})"
            + (f":\n{detail}" if detail else "")
        )
    return result


class Host:
    """Everything that differs between a real install and a ``--test-root`` one."""

    def __init__(self, layout: Layout):
        self.layout = layout

    # systemd

    def systemctl(self, *args, check=True, capture=False):
        base = ["systemctl", "--user"] if self.layout.test else ["systemctl"]
        return run([*base, *args], check=check, capture=capture)

    def start(self, verb: str = "start") -> None:
        """An administrator's start is deliberate: clear the crash-loop limit first, which
        counts every start, so that an update after a few restarts is not refused."""
        self.systemctl("reset-failed", self.layout.unit_name, check=False, capture=True)
        self.systemctl(verb, self.layout.unit_name)

    def unit_state(self) -> str:
        result = self.systemctl("is-active", self.layout.unit_name, check=False, capture=True)
        return result.stdout.strip() or "unknown"

    def journal(self, extra: list[str]) -> int:
        unit = ["--user-unit" if self.layout.test else "--unit", self.layout.unit_name]
        return subprocess.call(["journalctl", *unit, "--no-pager", *extra])

    # accounts and ownership

    def service_ids(self) -> tuple[int, int]:
        if self.layout.test:
            return os.getuid(), os.getgid()
        entry = pwd.getpwnam(SERVICE_USER)
        return entry.pw_uid, entry.pw_gid

    def own(self, path: Path, mode: int) -> None:
        """Owned by the service account with ``mode``."""
        uid, gid = self.service_ids()
        if not self.layout.test:
            os.chown(path, uid, gid)
        os.chmod(path, mode)

    def root_owned(self, path: Path, mode: int, group_service: bool = False) -> None:
        if not self.layout.test:
            os.chown(path, 0, self.service_ids()[1] if group_service else 0)
        os.chmod(path, mode)

    def service_env(self) -> dict:
        env = {
            "HOME": str(self.layout.home),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "MAESTRO_CONFIG": str(self.layout.config),
        }
        if self.layout.test:
            env["PATH"] = os.environ.get("PATH", env["PATH"])
        return env

    def as_service(self, args, *, check=True, capture=True, timeout=None):
        """Run a command as the service account with a clean environment."""
        env = self.service_env()
        cmd = ["env", "-i", *(f"{k}={v}" for k, v in env.items()), *args]
        if not self.layout.test:
            cmd = ["runuser", "-u", SERVICE_USER, "--", *cmd]
        return run(cmd, check=check, capture=capture, timeout=timeout)

    def maestro(self, version_dir: Path, *args, check=True, timeout=None):
        return self.as_service(
            [version_dir / "venv" / "bin" / "maestro", "--config", self.layout.config, *args],
            check=check,
            timeout=timeout,
        )


# --- releases -----------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Release:
    directory: Path
    info: dict

    @property
    def build(self) -> str:
        return self.info["build"]

    @classmethod
    def load(cls, directory: Path) -> "Release":
        try:
            info = json.loads((directory / RELEASE_FILE).read_text())
        except (OSError, ValueError):
            raise CtlError(f"{directory} is not a Maestro release (no {RELEASE_FILE})") from None
        if info.get("name") != "maestro" or not re.fullmatch(r"[A-Za-z0-9._+-]+", info["build"]):
            raise CtlError(f"{directory}/{RELEASE_FILE} is not a Maestro release description")
        return cls(directory, info)

    def verify(self) -> None:
        for name, expected in sorted(self.info["files"].items()):
            path = self.directory / name
            if not path.is_file() or sha256_file(path) != expected:
                raise CtlError(f"release file {name} is missing or does not match its digest")

    def file(self, prefix: str, suffix: str) -> Path:
        found = [n for n in self.info["files"] if n.startswith(prefix) and n.endswith(suffix)]
        if len(found) != 1:
            raise CtlError(f"release has no single {prefix}*{suffix}")
        return self.directory / found[0]


def own_release() -> Release:
    return Release.load(Path(__file__).resolve().parent)


def unpack(package: Path, layout: Layout) -> Path:
    """A release directory from a package path: an unpacked directory or a ``.tar.gz``."""
    package = package.absolute()
    if package.is_dir():
        return package
    if not tarfile.is_tarfile(package):
        raise CtlError(f"{package} is neither a release directory nor a release package")
    layout.incoming.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = Path(tempfile.mkdtemp(prefix="release-", dir=layout.incoming))
    with tarfile.open(package) as archive:
        archive.extractall(target, filter="data")
    tops = [p for p in target.iterdir() if p.is_dir()]
    if len(tops) != 1:
        raise CtlError(f"{package} must contain exactly one top-level directory")
    return tops[0]


# --- uv environments ----------------------------------------------------------------


def find_uv(layout: Layout, given: str | None) -> Path:
    candidates = [given, os.environ.get("MAESTRO_UV"), shutil.which("uv"), str(layout.uv)]
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        with contextlib.suppress(KeyError):
            candidates.append(str(Path(pwd.getpwnam(sudo_user).pw_dir) / ".local/bin/uv"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return Path(candidate).absolute()
    raise CtlError(
        "uv was not found: install it (https://docs.astral.sh/uv/) and pass --uv /path/to/uv"
    )


def keep_uv(layout: Layout, uv: Path) -> Path:
    """Later updates run as root and must not execute a binary a user can replace."""
    if uv == layout.uv:
        return uv
    layout.uv.parent.mkdir(parents=True, exist_ok=True)
    staged = layout.uv.with_name("uv.new")
    shutil.copy2(uv, staged)
    os.chmod(staged, 0o755)
    os.replace(staged, layout.uv)
    return layout.uv


def uv_env(layout: Layout, proxy: dict) -> dict:
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": os.environ.get("HOME", "/root"),
        "LANG": "C.UTF-8",
        **proxy,
    }
    if layout.test:
        # Reuse the developer's caches and interpreters: the scratch install is theirs.
        env["PATH"] = os.environ.get("PATH", env["PATH"])
        for name in ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
            if name in os.environ:
                env[name] = os.environ[name]
    else:
        # Interpreters must be readable by the service account, so never root's home.
        env["UV_PYTHON_INSTALL_DIR"] = str(layout.python_dir)
        env["UV_CACHE_DIR"] = str(layout.opt / ".uv-cache")
    return env


def build_environment(uv: Path, env: dict, target: Path, requirements: Path, wheel: Path) -> None:
    run([uv, "venv", "--quiet", "--python", PYTHON_REQUEST, target], env=env, capture=True)
    python = target / "bin" / "python"
    common = [uv, "pip", "install", "--quiet", "--python", python, "--compile-bytecode"]
    run([*common, "--require-hashes", "-r", requirements], env=env, capture=True)
    run([*common, "--no-deps", wheel], env=env, capture=True)


def install_version(layout: Layout, release: Release, uv: Path, proxy: dict) -> Path:
    """Program and Runner environments of one release; reused when already complete.
    Building happens next to the running version, which keeps serving meanwhile."""
    target = layout.versions / release.build
    if (target / COMPLETE).exists():
        say(f"    version {release.build} already installed")
        return target
    release.verify()
    if target.exists():
        safe_rmtree(layout, target)
    layout.versions.mkdir(parents=True, exist_ok=True)
    target.mkdir()
    env = uv_env(layout, proxy)
    for name in release.info["files"]:
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(release.directory / name, destination)
        os.chmod(destination, 0o755 if name == "maestroctl" else 0o644)
    shutil.copyfile(release.directory / RELEASE_FILE, target / RELEASE_FILE)
    os.chmod(target / RELEASE_FILE, 0o644)
    step(f"Installing the service environment for {release.build}")
    build_environment(
        uv,
        env,
        target / "venv",
        target / "server" / "requirements.txt",
        release.file("server/", ".whl"),
    )
    runner = layout.harbor / release.info["harbor_version"] / release.info["runner_revision"]
    if not (runner / COMPLETE).exists():
        step(f"Installing Harbor {release.info['harbor_version']} and the Runner")
        if runner.exists():
            safe_rmtree(layout, runner)
        runner.mkdir(parents=True)
        build_environment(
            uv,
            env,
            runner / "venv",
            target / "runner" / "requirements.txt",
            release.file("runner/", ".whl"),
        )
        (runner / COMPLETE).write_text(release.build + "\n")
    (target / "runner-env").symlink_to(os.path.relpath(runner, target))
    (target / COMPLETE).write_text(utc_stamp() + "\n")
    return target


def switch_current(layout: Layout, target: Path) -> None:
    staged = layout.opt / "current.new"
    with contextlib.suppress(FileNotFoundError):
        staged.unlink()
    staged.symlink_to(os.path.relpath(target, layout.opt))
    os.replace(staged, layout.current)


def current_version(layout: Layout) -> Path | None:
    if not layout.current.is_symlink():
        return None
    return layout.current.resolve()


def prune_versions(layout: Layout, keep: list[Path]) -> list[str]:
    removed = []
    keep = {p.resolve() for p in keep}
    for version in sorted(layout.versions.glob("*")):
        if version.resolve() not in keep and not version.is_symlink():
            safe_rmtree(layout, version)
            removed.append(version.name)
    used = {(v / "runner-env").resolve() for v in keep if (v / "runner-env").exists()}
    for runner in layout.harbor.glob("*/*"):
        if runner.resolve() not in used:
            safe_rmtree(layout, runner)
            removed.append(f"runner {runner.parent.name}/{runner.name}")
    return removed


# --- configuration and unit ---------------------------------------------------------


def toml_string(value: str) -> str:
    return json.dumps(value)


def render_config(layout: Layout, args) -> str:
    lines = [
        f"# Written by maestroctl install on {utc_stamp()}. Keys and limits: INSTALL.md of",
        "# the release. Apply changes with: maestroctl restart",
        f"data_dir = {toml_string(str(layout.data))}",
        f"cache_dir = {toml_string(str(layout.cache))}",
        f"master_key_dir = {toml_string(str(layout.secrets))}",
        f"runner_python = {toml_string(str(layout.current / 'runner-env' / 'venv' / 'bin' / 'python'))}",
        f"listen_host = {toml_string(args.listen_host)}",
        f"listen_port = {args.port}",
        f"image_registry = {toml_string(f'127.0.0.1:{args.registry_port}')}",
        # Without HTTPS in front, browsers drop a Secure cookie and logins never stick.
        f"cookie_secure = {'false' if args.insecure_cookie else 'true'}",
    ]
    return "\n".join(lines) + "\n"


def render_environment(args) -> str:
    """Variables for the service process: the Docker client (BuildKit fetches registry
    tokens itself), Hub downloads in the Runner, and the Docker endpoint if not default."""
    lines = ["# Written by maestroctl. Applied on: maestroctl restart"]
    if args.proxy:
        no_proxy = args.no_proxy or "127.0.0.1,localhost,::1"
        for name, value in (
            ("HTTP_PROXY", args.proxy),
            ("HTTPS_PROXY", args.proxy),
            ("NO_PROXY", no_proxy),
        ):
            lines += [f"{name}={value}", f"{name.lower()}={value}"]
    if args.docker_host:
        lines.append(f"DOCKER_HOST={args.docker_host}")
    return "\n".join(lines) + "\n"


def read_config(layout: Layout) -> dict:
    try:
        return tomllib.loads(layout.config.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise CtlError(f"cannot read {layout.config}: {error}") from None


def render_unit(layout: Layout) -> str:
    venv = layout.current / "venv" / "bin" / "maestro"
    path = "/usr/local/bin:/usr/bin:/bin"
    if layout.test:
        docker = shutil.which("docker")
        if docker and str(Path(docker).parent) not in path.split(":"):
            path = f"{Path(docker).parent}:{path}"
    lines = [
        "# Written by maestroctl and rewritten on update; local changes belong in an",
        f"# override: systemctl edit {layout.unit_name}",
        "[Unit]",
        "Description=Maestro experiment service",
        "After=network-online.target docker.service",
        "Wants=network-online.target",
        "StartLimitIntervalSec=600",
        "StartLimitBurst=5",
        "",
        "[Service]",
        "Type=exec",
    ]
    if not layout.test:
        lines += [f"User={SERVICE_USER}", f"Group={SERVICE_USER}"]
    lines += [
        f"WorkingDirectory={layout.data}",
        f"Environment=MAESTRO_CONFIG={layout.config}",
        f"Environment=HOME={layout.home}",
        f"Environment=PATH={path}",
        "Environment=PYTHONUNBUFFERED=1",
        f"EnvironmentFile=-{layout.environment}",
        f"ExecStart={venv} serve",
        "Restart=on-failure",
        "RestartSec=5",
        # Runners get SIGTERM with the service and stop their Harbor jobs; the next start
        # reconciles anything left by label (05 sections 7-8).
        "TimeoutStopSec=120",
        "UMask=0077",
    ]
    if not layout.test:
        lines += [
            "NoNewPrivileges=yes",
            "ProtectSystem=full",
            f"ReadWritePaths={layout.secrets}",
            "ProtectHome=yes",
        ]
    lines += [
        "",
        "[Install]",
        f"WantedBy={'default.target' if layout.test else 'multi-user.target'}",
    ]
    return "\n".join(lines) + "\n"


def write_unit(host: Host) -> None:
    path = host.layout.unit_path
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(path.name + ".new")
    staged.write_text(render_unit(host.layout))
    os.chmod(staged, 0o644)
    os.replace(staged, path)
    host.systemctl("daemon-reload")


def health_url(layout: Layout) -> str:
    config = read_config(layout)
    host = config.get("listen_host", "127.0.0.1")
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{config.get('listen_port', DEFAULT_PORT)}"


def probe(layout: Layout) -> dict | None:
    try:
        with urllib.request.urlopen(health_url(layout) + "/health/ready", timeout=3) as response:
            return json.loads(response.read())
    except (OSError, ValueError, urllib.error.URLError):
        return None


def wait_ready(host: Host, timeout: float) -> dict:
    """Ready means reconciled and serving (12 section 4); a unit that failed ends the wait."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = probe(host.layout)
        if state and state.get("status") == "ready":
            return state
        if host.unit_state() in ("failed", "inactive"):
            break
        time.sleep(1)
    raise CtlError(
        f"the service did not become ready within {timeout:.0f}s; see: maestroctl logs -n 100"
    )


# --- checks -------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    status: str  # ok, warn, fail
    detail: str
    fix: str = ""


def _docker(cmd: list[str], env: dict | None = None) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["docker", *cmd], capture_output=True, text=True, timeout=30, env=env, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def free_bytes(path: Path) -> int:
    while not path.exists():
        path = path.parent
    stat = os.statvfs(path)
    return stat.f_bavail * stat.f_frsize


def port_free(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe_socket:
        probe_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe_socket.bind((host, port))
        except OSError:
            return False
    return True


def run_checks(layout: Layout, args, *, installing: bool) -> list[Check]:
    checks = []
    add = checks.append
    if sys.platform != "linux":
        add(Check("platform", "fail", sys.platform, "Maestro runs on Linux only"))
        return checks
    machine = os.uname().machine
    add(Check("platform", "ok" if machine in ("x86_64", "aarch64") else "warn", f"Linux {machine}"))
    if not layout.test and os.geteuid() != 0:
        add(Check("privileges", "fail", "not root", "run with sudo"))
    if Path("/run/systemd/system").is_dir():
        add(Check("systemd", "ok", "running"))
    else:
        add(Check("systemd", "fail", "systemd is not the init system", "use a systemd host"))
    try:
        uv = find_uv(layout, getattr(args, "uv", None))
        found = run(
            [uv, "python", "find", PYTHON_REQUEST],
            env=uv_env(layout, {}),
            check=False,
            capture=True,
        )
        add(Check("uv", "ok", str(uv)))
        if found.returncode == 0:
            add(Check("python", "ok", found.stdout.strip()))
        else:
            add(
                Check(
                    "python",
                    "warn",
                    f"no Python {PYTHON_REQUEST} found",
                    "uv will download one into /opt/maestro/python (needs network)",
                )
            )
    except CtlError as error:
        add(Check("uv", "fail", str(error), "pass --uv /path/to/uv"))
    env = dict(os.environ)
    if getattr(args, "docker_host", None):
        env["DOCKER_HOST"] = args.docker_host
    version = _docker(["version", "--format", "{{.Server.Version}}"], env)
    if version is None:
        add(Check("docker", "fail", "docker CLI not found", "install Docker Engine"))
    elif version.returncode:
        add(
            Check(
                "docker",
                "fail",
                (version.stderr or "").strip()[-200:],
                "start the Docker daemon (systemctl start docker)",
            )
        )
    else:
        add(Check("docker", "ok", f"daemon {version.stdout.strip()}"))
        for plugin in ("compose", "buildx"):
            found = _docker([plugin, "version"], env)
            ok = found is not None and found.returncode == 0
            text = found.stdout.strip().splitlines()[0] if ok and found.stdout else "missing"
            advice = "" if ok else f"install the docker-{plugin}-plugin package"
            if ok and plugin == "compose" and compose_version(text) < COMPOSE_MIN:
                ok = False
                advice = "Docker Compose 2.24 or later is needed; update docker-compose-plugin"
            add(Check(f"docker {plugin}", "ok" if ok else "fail", text, advice))
        info = _docker(["info", "--format", "{{json .}}"], env)
        if info is not None and info.returncode == 0:
            with contextlib.suppress(ValueError):
                for check in daemon_checks(
                    json.loads(info.stdout), bool(getattr(args, "proxy", None))
                ):
                    add(check)
    if not layout.test and getattr(args, "docker_host", None) is None:
        sock = Path("/var/run/docker.sock")
        if sock.exists():
            add(Check("docker socket", "ok", f"group {grp.getgrgid(sock.stat().st_gid).gr_name}"))
    nft_ok = Path("/sys/module/nf_tables").exists()
    add(
        Check(
            "nftables",
            "ok" if nft_ok else "fail",
            "kernel nf_tables available" if nft_ok else "kernel module nf_tables not loaded",
            "" if nft_ok else "modprobe nf_tables (Harbor needs it to restrict task networks)",
        )
    )
    free = free_bytes(layout.data)
    gib = free / 1024**3
    add(
        Check(
            "disk",
            "ok" if gib >= 20 else "warn",
            f"{gib:.0f} GiB free for {layout.data}",
            ""
            if gib >= 20
            else "new runs are refused below 20 GiB free (storage_accept_min_free_gb)",
        )
    )
    if not installing and layout.config.exists():
        address = registry_address(layout)
        ready = registry_ready(address)
        add(
            Check(
                "image registry",
                "ok" if ready else "fail",
                f"{layout.registry_name} at {address}",
                "" if ready else "prepared images cannot be restored; run: maestroctl restart",
            )
        )
    if installing:
        port = getattr(args, "registry_port", None) or int(DEFAULT_REGISTRY.rsplit(":", 1)[1])
        free = port_free("127.0.0.1", port)
        add(
            Check(
                "registry port",
                "ok" if free else "fail",
                f"127.0.0.1:{port}",
                "" if free else "choose another --registry-port",
            )
        )
        host = getattr(args, "listen_host", "127.0.0.1")
        port = getattr(args, "port", DEFAULT_PORT)
        add(
            Check(
                "port",
                "ok" if port_free(host, port) else "fail",
                f"{host}:{port}",
                "" if port_free(host, port) else "choose another --port",
            )
        )
    return checks


def print_checks(checks: list[Check]) -> bool:
    marks = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL"}
    for check in checks:
        say(f"  [{marks[check.status]}] {check.name}: {check.detail}")
        if check.fix and check.status != "ok":
            say(f"         -> {check.fix}")
    return not any(c.status == "fail" for c in checks)


# --- commands -----------------------------------------------------------------------


def require_root(layout: Layout) -> None:
    if not layout.test and os.geteuid() != 0:
        raise CtlError("this command needs root: run it with sudo")


def require_installed(layout: Layout) -> Path:
    version = current_version(layout)
    if version is None or not layout.config.exists():
        raise CtlError(
            "Maestro is not installed here (no /opt/maestro/current); run: maestroctl install"
        )
    return version


def read_record(layout: Layout) -> dict:
    with contextlib.suppress(OSError, ValueError):
        return json.loads(layout.record.read_text())
    return {}


def write_record(layout: Layout, **values) -> None:
    record = read_record(layout) | values
    layout.record.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    os.chmod(layout.record, 0o644)


def ensure_account(layout: Layout, docker_host: str | None) -> None:
    if layout.test:
        return
    try:
        pwd.getpwnam(SERVICE_USER)
        say(f"    account {SERVICE_USER} exists")
    except KeyError:
        run(
            [
                "useradd",
                "--system",
                "--user-group",
                "--home-dir",
                layout.home,
                "--no-create-home",
                "--shell",
                "/usr/sbin/nologin",
                "--comment",
                "Maestro service",
                SERVICE_USER,
            ]
        )
        say(f"    created system account {SERVICE_USER}")
    if docker_host:
        return  # another daemon: access is arranged by whoever runs it
    sock = Path("/var/run/docker.sock")
    if not sock.exists():
        raise CtlError("/var/run/docker.sock does not exist: is the Docker daemon running?")
    group = grp.getgrgid(sock.stat().st_gid).gr_name
    if group == "root":
        raise CtlError("the Docker socket belongs to group root; refusing to add the service to it")
    run(["usermod", "--append", "--groups", group, SERVICE_USER])
    say(f"    {SERVICE_USER} may use the system Docker (group {group}; equivalent to root)")


def make_directories(host: Host) -> None:
    layout = host.layout
    os.umask(0o022)
    for path in (layout.opt, layout.versions, layout.harbor, layout.ctl_link.parent):
        path.mkdir(parents=True, exist_ok=True)
    layout.etc.mkdir(parents=True, exist_ok=True)
    host.root_owned(layout.etc, 0o750, group_service=True)
    for path, mode in (
        (layout.secrets, 0o700),
        (layout.data, 0o700),
        (layout.home, 0o700),
        (layout.cache, 0o700),
        (layout.backups, 0o700),
    ):
        path.mkdir(parents=True, exist_ok=True)
        host.own(path, mode)


SERVICE_PROXY = "service"


def service_proxy(layout: Layout) -> str | None:
    """The proxy the installed service uses (HTTPS_PROXY in the environment file)."""
    with contextlib.suppress(OSError):
        for line in layout.environment.read_text().splitlines():
            name, _, value = line.partition("=")
            if name.strip() in ("HTTPS_PROXY", "https_proxy") and value.strip():
                return value.strip()
    return None


def download_env(args, service: str | None) -> dict:
    """Downloads go direct unless ``--download-proxy`` asks otherwise: alone it means the
    service's proxy, with a value that proxy. The service's proxy is not the default
    because a mirror or index may well be faster without it."""
    chosen = args.download_proxy
    if chosen == SERVICE_PROXY:
        chosen = service
        if not chosen:
            raise CtlError("--download-proxy without a value needs a service proxy (--proxy)")
    if not chosen:
        return {}
    if "://" not in chosen:
        raise CtlError(
            f"--download-proxy {chosen!r} is not a proxy URL; give one, or put the package "
            "path before --download-proxy"
        )
    say(f"    downloading through {chosen}")
    return {name: chosen for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")}


def daemon_checks(info: dict, proxy_wanted: bool) -> list[Check]:
    """What ``docker info`` says about pulls: the daemon's proxy and registry mirrors.
    Reported only; maestroctl never changes the host's Docker configuration."""
    checks = []
    if proxy_wanted and not (info.get("HttpsProxy") or info.get("HttpProxy")):
        checks.append(
            Check(
                "docker proxy",
                "warn",
                "the Docker daemon has no proxy",
                "pulls go direct; to proxy them add HTTP(S)_PROXY to a drop-in "
                "of docker.service (systemctl edit docker) and restart Docker",
            )
        )
    mirrors = (info.get("RegistryConfig") or {}).get("Mirrors") or []
    checks.append(
        Check(
            "docker mirrors",
            "ok",
            ", ".join(mirrors) if mirrors else "none: Docker Hub pulls go to the registry itself",
        )
    )
    return checks


def compose_version(text: str) -> tuple[int, int]:
    """``Docker Compose version v2.29.1`` -> (2, 29); (0, 0) when it cannot be read."""
    found = re.search(r"v?(\d+)\.(\d+)", text)
    return (int(found[1]), int(found[2])) if found else (0, 0)


def docker_env(layout: Layout) -> dict:
    """The service's Docker endpoint (DOCKER_HOST in its environment file), else ours."""
    env = dict(os.environ)
    with contextlib.suppress(OSError):
        for line in layout.environment.read_text().splitlines():
            if line.startswith("DOCKER_HOST="):
                env["DOCKER_HOST"] = line.split("=", 1)[1]
    return env


def registry_address(layout: Layout) -> str:
    return read_config(layout).get("image_registry", DEFAULT_REGISTRY)


def registry_ready(address: str, timeout: float = 2) -> bool:
    # Local: a proxy in root's environment must not be asked for it.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://{address}/v2/", timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def start_registry(host: Host, *, read_only: bool = False, timeout: float = 60) -> None:
    """(Re)create the registry container: data under data_dir, the port on the address the
    service is configured with, restarted by Docker with the daemon. Read-only for garbage
    collection: pushes are refused, pulls go on."""
    layout = host.layout
    env = docker_env(layout)
    address = registry_address(layout)
    bind, port = address.rsplit(":", 1)
    layout.registry_dir.mkdir(parents=True, exist_ok=True)
    host.own(layout.registry_dir, 0o700)
    run(["docker", "rm", "-f", layout.registry_name], env=env, check=False, capture=True)
    cmd = ["docker", "run", "-d", "--name", layout.registry_name, "--restart", "unless-stopped"]
    if not layout.test:
        # Files belong to the service account, so backups and the service can read them.
        uid, gid = host.service_ids()
        cmd += ["--user", f"{uid}:{gid}"]
    cmd += [
        "-p",
        f"{bind}:{port}:5000",
        "-v",
        f"{layout.registry_dir}:/var/lib/registry",
        "-e",
        "REGISTRY_STORAGE_DELETE_ENABLED=true",
    ]
    if read_only:
        cmd += ["-e", 'REGISTRY_STORAGE_MAINTENANCE_READONLY={"enabled": true}']
    run([*cmd, REGISTRY_IMAGE], env=env, capture=True, timeout=900)
    deadline = time.monotonic() + timeout
    while not registry_ready(address):
        if time.monotonic() > deadline:
            raise CtlError(f"the image registry did not answer at {address}")
        time.sleep(0.5)


def remove_registry(layout: Layout) -> None:
    run(
        ["docker", "rm", "-f", layout.registry_name],
        env=docker_env(layout),
        check=False,
        capture=True,
    )


def cmd_registry_gc(layout: Layout, args) -> int:
    """Free the layers no prepared image uses any more (after purging dataset versions)."""
    require_root(layout)
    version = require_installed(layout)
    host = Host(layout)
    activity = json.loads(host.maestro(version, "activity", timeout=60).stdout)
    pushing = {"dataset_import", "preparation_first", "preparation_restore"}
    busy = sorted(pushing & set(activity["active_operations"]))
    if busy and not args.force:
        raise CtlError(
            f"preparations are in progress ({', '.join(busy)}); they would fail to push. "
            "Wait for them, or pass --force"
        )
    before = directory_size(layout.registry_dir)
    if not (layout.registry_dir / "docker" / "registry" / "v2" / "repositories").is_dir():
        # The collector itself fails on a registry that never stored anything.
        say("the registry holds no images yet: nothing to collect")
        return 0
    step("Registry read-only: pulls go on, pushes are refused")
    start_registry(host, read_only=True)
    try:
        step("Collecting garbage")
        run(
            [
                "docker",
                "exec",
                layout.registry_name,
                "registry",
                "garbage-collect",
                REGISTRY_CONFIG,
            ],
            env=docker_env(layout),
            capture=True,
            timeout=3600,
        )
    finally:
        step("Registry writable again")
        start_registry(host)
    after = directory_size(layout.registry_dir)
    say(f"registry {before / 1024**2:,.0f} MiB -> {after / 1024**2:,.0f} MiB")
    return 0


def cmd_check(layout: Layout, args) -> int:
    installed = current_version(layout) is not None
    say("Maestro host check" + (" (already installed)" if installed else ""))
    return 0 if print_checks(run_checks(layout, args, installing=not installed)) else 1


def cmd_install(layout: Layout, args) -> int:
    require_root(layout)
    release = own_release()
    if current_version(layout) is not None:
        raise CtlError(
            f"already installed ({current_version(layout).name}); use: maestroctl update"
        )
    step(f"Checking the host for Maestro {release.build}")
    if not print_checks(run_checks(layout, args, installing=True)):
        raise CtlError("fix the failed checks above and run install again")
    host = Host(layout)
    step("Account and directories")
    ensure_account(layout, args.docker_host)
    make_directories(host)
    uv = keep_uv(layout, find_uv(layout, args.uv))
    version = install_version(
        layout, release, uv, download_env(args, args.proxy or service_proxy(layout))
    )
    switch_current(layout, version)
    step("Configuration")
    if layout.config.exists():
        say(f"    keeping {layout.config}")
    else:
        layout.config.write_text(render_config(layout, args))
        say(f"    wrote {layout.config}")
    host.root_owned(layout.config, 0o640, group_service=True)
    if not layout.environment.exists() or args.proxy or args.docker_host:
        layout.environment.write_text(render_environment(args))
    host.root_owned(layout.environment, 0o640, group_service=True)
    step("Image registry")
    start_registry(host)
    say(f"    {layout.registry_name} at {registry_address(layout)}")
    step("Initializing the instance")
    init = host.maestro(version, "init")
    token = None
    lines = init.stdout.strip().splitlines()
    for line in lines:
        if not line.startswith(("One-time setup token", "mst_set_")):
            say(f"    {line}")
    if lines and lines[-1].startswith("mst_set_"):
        token = lines[-1]
    step("Starting the service")
    write_unit(host)
    host.systemctl("enable", "--now", layout.unit_name, capture=True)
    wait_ready(host, args.ready_timeout)
    link_ctl(layout)
    write_record(layout, installed_at=utc_stamp(), build=release.build, unit=layout.unit_name)
    say()
    say(f"Maestro {release.build} is running: {health_url(layout)}")
    if token:
        say("One-time setup token for the first administrator (shown once):")
        say(f"    {token}")
    else:
        say("An administrator already exists (data kept from an earlier install).")
    if token:
        say("Next: open the address (HTTPS through your reverse proxy), create the administrator,")
        say("then add credentials, models and datasets in the web pages.")
    say("Daily: maestroctl status | logs | backup; help: maestroctl --help")
    return 0


def link_ctl(layout: Layout) -> None:
    staged = layout.ctl_link.with_name("maestroctl.new")
    with contextlib.suppress(FileNotFoundError):
        staged.unlink()
    staged.symlink_to(layout.current / "maestroctl")
    os.replace(staged, layout.ctl_link)


def cmd_status(layout: Layout, args) -> int:
    version = require_installed(layout)
    host = Host(layout)
    record = read_record(layout)
    state = host.unit_state()
    health = probe(layout) if state == "active" else None
    say(f"version:  {version.name}")
    say(f"service:  {state} ({layout.unit_name})")
    if health:
        say(
            f"health:   {health.get('status')}, accepting runs: {'yes' if health.get('accepting_runs') else 'no'}"
        )
    say(f"address:  {health_url(layout)}")
    with contextlib.suppress(CtlError):
        activity = json.loads(host.maestro(version, "activity", timeout=60).stdout)
        say(f"work:     {activity['active_runs']} running, {activity['queued_runs']} queued runs")
    address = registry_address(layout)
    say(
        f"registry: {'up' if registry_ready(address) else 'DOWN'} at {address} ({layout.registry_name})"
    )
    say(f"data:     {layout.data} ({free_bytes(layout.data) / 1024**3:.0f} GiB free)")
    kept = sorted(p.name for p in layout.versions.glob("*") if (p / COMPLETE).exists())
    say(f"versions: {', '.join(kept)}")
    if record.get("updated_at"):
        say(f"updated:  {record['updated_at']}")
    return 0 if state == "active" else 3


def cmd_service(layout: Layout, args) -> int:
    require_root(layout)
    require_installed(layout)
    host = Host(layout)
    if args.command == "stop":
        host.systemctl("stop", layout.unit_name)
    else:
        host.start(args.command)
    if args.command in ("start", "restart"):
        wait_ready(host, args.ready_timeout)
        say(f"ready: {health_url(layout)}")
    return 0


def cmd_logs(layout: Layout, args) -> int:
    extra = ["-n", str(args.lines)]
    if args.follow:
        extra.append("-f")
    return Host(layout).journal(extra)


def make_backup(host: Host, version: Path, label: str, include_files: bool) -> Path:
    layout = host.layout
    layout.backups.mkdir(parents=True, exist_ok=True)
    destination = layout.backups / f"{utc_stamp()}{('-' + label) if label else ''}"
    host.maestro(
        version,
        "backup",
        "--output",
        destination,
        *(["--include-files"] if include_files else []),
        timeout=None,
    )
    return destination


def cmd_backup(layout: Layout, args) -> int:
    require_root(layout)
    version = require_installed(layout)
    destination = make_backup(Host(layout), version, args.label, args.include_files)
    manifest = json.loads((destination / "manifest.json").read_text())
    say(f"backup written to {destination}")
    say(f"  schema {manifest['schema_revision']}, instance {manifest['instance_id']}")
    say(
        f"  files {'included' if args.include_files else 'listed only (add --include-files to copy them)'}"
    )
    key = manifest.get("master_key") or {}
    if key:
        say(
            f"  needs master key generation {key['generation']} ({key['basename']} in {layout.secrets});"
        )
        say("  the key is not in the backup: keep a copy of it somewhere safe and separate")
    return 0


def restore_database(
    host: Host, version: Path, source: Path, *, include_files=False, allow_missing_key=False
) -> None:
    host.maestro(
        version,
        "restore",
        source,
        *(["--include-files"] if include_files else []),
        *(["--allow-missing-key"] if allow_missing_key else []),
    )
    host.maestro(version, "db", "upgrade")


def cmd_restore(layout: Layout, args) -> int:
    require_root(layout)
    version = require_installed(layout)
    host = Host(layout)
    source = Path(args.source).absolute()
    was_active = host.unit_state() == "active"
    step("Stopping the service")
    host.systemctl("stop", layout.unit_name)
    try:
        step(f"Restoring {source}")
        restore_database(
            host,
            version,
            source,
            include_files=args.include_files,
            allow_missing_key=args.allow_missing_key,
        )
        if args.include_files:
            step("Restarting the image registry on the restored files")
            start_registry(host)
    finally:
        if was_active:
            step("Starting the service")
            host.start()
    if was_active:
        wait_ready(host, args.ready_timeout)
    say("restored; runs that were executing when the backup was taken are marked interrupted")
    if not was_active:
        say("the service was not running before; start it with: maestroctl start")
    return 0


def simulated(point: str) -> None:
    """Tests force failures at fixed points of an update, only under --test-root."""
    if os.environ.get("MAESTRO_CTL_FAIL_AT") == point:
        raise CtlError(f"simulated failure at {point}")


def cmd_update(layout: Layout, args) -> int:
    require_root(layout)
    if args.package:
        directory = unpack(Path(args.package), layout)
        release = Release.load(directory)
        if release.directory.resolve() != own_release().directory.resolve():
            # The new release knows how to install itself: hand over to its maestroctl.
            forwarded = [a for a in sys.argv[1:] if a != args.package]
            return subprocess.call(
                [sys.executable, "-I", str(directory / "maestroctl"), *forwarded]
            )
    release = own_release()
    old = require_installed(layout)
    host = Host(layout)
    if old.name == release.build:
        say(f"already at {release.build}")
        return 0
    record = read_record(layout)
    proxy = download_env(args, service_proxy(layout))
    uv = find_uv(layout, args.uv)
    step(f"Installing {release.build} beside {old.name} (the service keeps running)")
    new = install_version(layout, release, keep_uv(layout, uv), proxy)
    activity = json.loads(host.maestro(old, "activity", timeout=60).stdout)
    if activity["busy"] and not args.interrupt_running:
        raise CtlError(
            f"{activity['active_runs']} runs are executing and operations {activity['active_operations'] or 'none'} "
            "are in progress. Wait for them, or pass --interrupt-running: they are marked "
            "interrupted (scores already recorded stay). Queued runs always wait for the new version."
        )
    previous_unit = layout.unit_path.read_text() if layout.unit_path.exists() else None
    step("Stopping the service")
    host.systemctl("stop", layout.unit_name)
    step("Backing up the database")
    backup = make_backup(host, old, f"before-{release.build}", include_files=False)
    say(f"    {backup}")
    switched = False
    try:
        step("Migrating the database")
        simulated("migrate")
        host.maestro(new, "db", "upgrade")
        step(f"Switching to {release.build}")
        switch_current(layout, new)
        switched = True
        write_unit(host)
        link_ctl(layout)
        step("Image registry")
        start_registry(host)
        host.start()
        simulated("start")
        wait_ready(host, args.ready_timeout)
    except CtlError as error:
        say(f"!! update failed: {error}")
        step(f"Rolling back to {old.name} and the database from {backup.name}")
        host.systemctl("stop", layout.unit_name, check=False)
        if switched:
            switch_current(layout, old)
            if previous_unit is not None:
                layout.unit_path.write_text(previous_unit)
                host.systemctl("daemon-reload")
        restore_database(host, old, backup)
        host.start()
        wait_ready(host, args.ready_timeout)
        say(f"rolled back: {old.name} is running again on the database from before the update")
        return 1
    write_record(
        layout,
        build=release.build,
        updated_at=utc_stamp(),
        previous_build=old.name,
        installed_at=record.get("installed_at"),
    )
    removed = prune_versions(layout, [new, old])
    say(f"Maestro {release.build} is running: {health_url(layout)}")
    say(f"previous version {old.name} kept for rollback; backup {backup}")
    if removed:
        say(f"removed old versions: {', '.join(removed)}")
    return 0


def directory_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, name)).st_size
    return total


def instance_id(layout: Layout) -> str | None:
    import sqlite3

    database = layout.data / "app.db"
    if not database.exists():
        return None
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            row = connection.execute("SELECT instance_id FROM instance").fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def docker_resources(layout: Layout, instance: str) -> dict[str, list[str]]:
    """The instance's labelled containers, networks, volumes and run images, and the local
    copies of its prepared images (named after the registry)."""
    env = docker_env(layout)
    found = {}
    for kind, cmd in (
        ("container", ["ps", "-aq", "--no-trunc"]),
        ("network", ["network", "ls", "-q", "--no-trunc"]),
        ("volume", ["volume", "ls", "-q"]),
        ("image", ["images", "-q", "--no-trunc"]),
    ):
        result = _docker([*cmd, "--filter", f"label={LABEL_INSTANCE}={instance}"], env)
        found[kind] = sorted(set(result.stdout.split())) if result and not result.returncode else []
    repository = f"{registry_address(layout)}/maestro-prep/{instance.lower()}"
    result = _docker(["images", "-q", "--no-trunc", repository], env)
    prepared = set(result.stdout.split()) if result and not result.returncode else set()
    found["image"] = sorted(set(found["image"]) | prepared)
    return found


def cmd_uninstall(layout: Layout, args) -> int:
    require_root(layout)
    host = Host(layout)
    instance = instance_id(layout)
    if args.purge:
        targets = [
            (layout.opt, "program, Python and Runner environments"),
            (layout.etc, "configuration and the MASTER KEY (credentials become unreadable)"),
            (layout.data, "database, artifacts, attachments, the prepared-image registry"),
            (layout.cache, "cache"),
            (layout.backups, "backups"),
        ]
        say("Purge removes all of this permanently:")
        for path, what in targets:
            if path.exists():
                say(f"  {path}  {directory_size(path) / 1024**2:,.0f} MiB  {what}")
        resources = (
            docker_resources(layout, instance) if instance and layout.config.exists() else {}
        )
        for kind, items in resources.items():
            if items:
                say(f"  Docker {kind}s of instance {instance}: {len(items)}")
        if not layout.test:
            say(f"  system account and group {SERVICE_USER}")
        say(f"  {layout.unit_path}")
        expected = instance or "maestro"
        if args.confirm != expected:
            say(f"Nothing was removed. To go ahead run again with: --purge --confirm {expected}")
            return 1
    step("Stopping and removing the service")
    if layout.unit_path.exists():
        host.systemctl("disable", "--now", layout.unit_name, check=False, capture=True)
        layout.unit_path.unlink()
        host.systemctl("daemon-reload")
    if layout.ctl_link.is_symlink():
        layout.ctl_link.unlink()
    remove_registry(layout)  # its data stays under data_dir unless purged
    safe_rmtree(layout, layout.opt)
    if args.purge:
        if instance and layout.config.exists():
            for kind, items in docker_resources(layout, instance).items():
                if items:
                    _docker(
                        {
                            "container": ["rm", "-f", "-v"],
                            "network": ["network", "rm"],
                            "volume": ["volume", "rm"],
                            "image": ["image", "rm", "-f"],
                        }[kind]
                        + items,
                        docker_env(layout),
                    )
        for path in (layout.etc, layout.data, layout.cache, layout.backups):
            safe_rmtree(layout, path)
        if not layout.test:
            run(["userdel", SERVICE_USER], check=False)
            run(["groupdel", SERVICE_USER], check=False, capture=True)
        say("Maestro and all of its data were removed.")
        return 0
    if args.remove_cache:
        safe_rmtree(layout, layout.cache)
    say("Program and service removed. Kept:")
    for path in (layout.etc, layout.data, layout.cache, layout.backups):
        if path.exists():
            say(f"  {path}")
    say("Installing again reuses them; to remove everything: maestroctl uninstall --purge")
    return 0


def cmd_version(layout: Layout, args) -> int:
    release = own_release()
    say(f"maestroctl {release.build} (commit {release.info.get('commit', '?')[:12]})")
    return 0


def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(prog="maestroctl", description="Install and maintain Maestro.")
    main.add_argument("--test-root", help=argparse.SUPPRESS)
    sub = main.add_subparsers(dest="command", required=True)

    def with_ready(p):
        p.add_argument("--ready-timeout", type=float, default=READY_TIMEOUT, help="seconds")
        return p

    def with_download(p):
        p.add_argument(
            "--download-proxy",
            nargs="?",
            const=SERVICE_PROXY,
            metavar="URL",
            help="proxy for downloading Python packages; without URL the service's --proxy "
            "(default: download directly)",
        )
        return p

    check = sub.add_parser("check", help="check this host without changing anything")
    install = with_ready(sub.add_parser("install", help="install and start Maestro"))
    for p in (check, install):
        p.add_argument("--listen-host", default="127.0.0.1")
        p.add_argument("--port", type=int, default=DEFAULT_PORT)
        p.add_argument("--proxy", help="HTTP(S) proxy the service uses (Docker client, Hub)")
        p.add_argument("--uv", help="path of the uv binary")
        p.add_argument("--docker-host", help="Docker endpoint if not the system daemon")
        p.add_argument(
            "--registry-port",
            type=int,
            default=int(DEFAULT_REGISTRY.rsplit(":", 1)[1]),
            help="local port of the prepared-image registry (127.0.0.1 only)",
        )
    install.add_argument("--no-proxy", help="hosts that bypass --proxy (comma separated)")
    with_download(install)
    install.add_argument(
        "--insecure-cookie",
        action="store_true",
        help="allow logins over plain HTTP (no HTTPS proxy in front)",
    )
    sub.add_parser("status", help="version, service state and health")
    for name in ("start", "stop", "restart"):
        with_ready(sub.add_parser(name, help=f"{name} the service"))
    logs = sub.add_parser("logs", help="service log from journald")
    logs.add_argument("-f", "--follow", action="store_true")
    logs.add_argument("-n", "--lines", type=int, default=200)
    backup = sub.add_parser("backup", help="back up the database and configuration")
    backup.add_argument("--include-files", action="store_true", help="also copy business files")
    backup.add_argument("--label", default="", help="suffix for the backup directory name")
    restore = with_ready(sub.add_parser("restore", help="restore a backup (stops the service)"))
    restore.add_argument("source")
    restore.add_argument("--include-files", action="store_true")
    restore.add_argument("--allow-missing-key", action="store_true")
    update = with_download(with_ready(sub.add_parser("update", help="update to a release package")))
    update.add_argument("package", nargs="?", help="release .tar.gz or directory")
    update.add_argument("--interrupt-running", action="store_true")
    update.add_argument("--uv")
    uninstall = sub.add_parser("uninstall", help="remove the service; data is kept")
    uninstall.add_argument("--remove-cache", action="store_true")
    uninstall.add_argument("--purge", action="store_true", help="also delete data, keys, backups")
    uninstall.add_argument("--confirm", help="instance id, required with --purge")
    gc = sub.add_parser("registry-gc", help="free registry space no prepared image uses")
    gc.add_argument("--force", action="store_true", help="even while preparations run")
    sub.add_parser("version", help="version of this maestroctl")
    return main


COMMANDS = {
    "check": cmd_check,
    "install": cmd_install,
    "status": cmd_status,
    "start": cmd_service,
    "stop": cmd_service,
    "restart": cmd_service,
    "logs": cmd_logs,
    "backup": cmd_backup,
    "restore": cmd_restore,
    "update": cmd_update,
    "uninstall": cmd_uninstall,
    "registry-gc": cmd_registry_gc,
    "version": cmd_version,
}


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if args.test_root:
        layout = Layout(Path(args.test_root).absolute(), test=True)
    else:
        layout = Layout(Path("/"))
        if "MAESTRO_CTL_FAIL_AT" in os.environ:
            print("MAESTRO_CTL_FAIL_AT only works with --test-root", file=sys.stderr)
            return 2
    try:
        return COMMANDS[args.command](layout, args)
    except CtlError as error:
        print(f"maestroctl: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:  # output piped into head and the like
        with contextlib.suppress(OSError):
            sys.stdout = open(os.devnull, "w")  # noqa: SIM115
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
