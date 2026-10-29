"""Deployment configuration from ``config.toml`` (12 section 3).

Business defaults, quotas and policies live in the database; this file only holds
what must be known before the database opens.
"""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from maestro.core.errors import MaestroError

DEFAULT_CONFIG = Path("/etc/maestro/config.toml")

MIB = 1024**2
GIB = 1024**3

# Limits that depend on the machine and its disk: (minimum, maximum), integers only. The
# defaults are the D-011 initial values (14 section 5). Team policies (PAT lifetime, run
# size, retention, ...) are not here: they move to the database with M5.
TUNING = {
    "artifact_max_files": (1, 10_000),
    "artifact_file_max_mb": (1, 4096),
    "artifact_attempt_max_mb": (1, 65_536),
    "export_max_gb": (1, 1024),
    "build_timeout_default_sec": (30, 86_400),
    "build_timeout_max_sec": (30, 86_400),
    "pull_timeout_sec": (30, 86_400),
    "push_timeout_sec": (30, 86_400),
    "restore_slots": (1, 16),
    "hub_runner_slots": (1, 16),
    "runner_heartbeat_sec": (1, 60),
    "runner_silence_sec": (10, 3600),
}
# The Runner reports every ``runner_heartbeat_sec``; this many in a row may go missing
# before the Runner counts as lost.
HEARTBEATS_BEFORE_LOST = 3
# Settings that moved elsewhere: still accepted so an older config.toml does not stop an
# update, ignored, and named in the start log (U-39).
RETIRED = {
    "setup_proxy": "the setup network is set on the admin page now",
    "setup_no_proxy": "the setup network is set on the admin page now",
    "save_timeout_sec": "prepared images are kept in the local image registry now",
}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    cache_dir: Path
    master_key_dir: Path
    listen_host: str = "127.0.0.1"
    listen_port: int = 8420
    root_path: str = ""
    cookie_secure: bool = True
    session_hours: int = 12
    # Execution capacity: concurrent trial slots, and CPU / memory totals for trial
    # containers (0 = the whole host). Runs reserve concurrency x per-trial resources.
    worker_slots: int = 10
    execution_cpus: float = 0
    execution_memory_mb: int = 0
    # Python of the separate Runner environment (locked Harbor); empty disables execution.
    runner_python: Path | None = None
    runner_term_grace_sec: float = 30
    cleanup_timeout_sec: float = 120
    # A Runner that reports nothing for ``runner_silence_sec`` is treated as lost and
    # stopped. Archiving a large trial directory blocks its heartbeats, so raise the
    # silence limit together with ``artifact_attempt_max_mb`` on a slow disk.
    runner_heartbeat_sec: int = 10
    runner_silence_sec: int = 120
    # Evidence kept per execution attempt, enforced by the Runner while it archives the
    # trial directory: anything over a limit is listed as skipped and never copied. Text
    # files up to the per-file limit are redacted, so nothing collected goes unscanned.
    artifact_max_files: int = 500
    artifact_file_max_mb: int = 64
    artifact_attempt_max_mb: int = 1024
    # Environment preparation (docker build, pull, push). A task may ask for its own build
    # time up to the maximum; the default applies when it asks for none. A build holds one
    # of ``restore_slots`` for as long as it runs. Imported versions record the limit they
    # were imported with; the maximum is applied again at every build.
    build_timeout_default_sec: int = 600
    build_timeout_max_sec: int = 3600
    pull_timeout_sec: int = 1800
    push_timeout_sec: int = 1800
    # The local image registry prepared images are kept in (N-01): ``host:port`` that the
    # Docker daemon reaches over plain HTTP. ``maestroctl`` runs it as a container with its
    # data under ``data_dir/registry``.
    image_registry: str = "127.0.0.1:8425"
    restore_slots: int = 2
    # One-shot Runner processes for Hub searches and downloads that may run at once.
    hub_runner_slots: int = 4
    # Largest batch export (07 section 6), checked against the selected files up front.
    export_max_gb: int = 10
    # Free space under data_dir (07 section 7, D-011 initial values; tune per disk):
    # below either "accept" threshold new runs are refused, below either "dispatch"
    # threshold queued runs wait. Queries, cancellation and downloads keep working.
    storage_accept_min_free_gb: float = 20
    storage_accept_min_free_percent: float = 15
    storage_dispatch_min_free_gb: float = 10
    storage_dispatch_min_free_percent: float = 8
    # Retired keys found in the file (``RETIRED``); reported by ``advice``.
    retired: tuple[str, ...] = ()

    @property
    def database(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def registry_dir(self) -> Path:
        """The local image registry's storage: prepared images are data, not cache (U-17)."""
        return self.data_dir / "registry"

    @property
    def legacy_preparation_dir(self) -> Path:
        """Where ``docker save`` originals were kept before the registry (N-01)."""
        return self.data_dir / "preparations"

    @property
    def work_dir(self) -> Path:
        """Runner working directories: transient Harbor output until artifacts are stored."""
        return self.data_dir / "work"

    @property
    def export_max_bytes(self) -> int:
        return self.export_max_gb * GIB

    def artifact_limits(self) -> dict[str, int]:
        """What the Runner enforces while archiving one attempt (sent with every run)."""
        return {
            "max_files": self.artifact_max_files,
            "max_file_bytes": self.artifact_file_max_mb * MIB,
            "max_total_bytes": self.artifact_attempt_max_mb * MIB,
        }

    def effective_limits(self) -> dict[str, int]:
        """The deployment limits in force, as ``/capabilities`` reports them."""
        artifact = self.artifact_limits()
        return {
            "export_max_bytes": self.export_max_bytes,
            "artifact_max_files": artifact["max_files"],
            "artifact_file_max_bytes": artifact["max_file_bytes"],
            "artifact_attempt_max_bytes": artifact["max_total_bytes"],
            "build_timeout_default_sec": self.build_timeout_default_sec,
            "build_timeout_max_sec": self.build_timeout_max_sec,
            "pull_timeout_sec": self.pull_timeout_sec,
            "push_timeout_sec": self.push_timeout_sec,
            "restore_slots": self.restore_slots,
            "hub_runner_slots": self.hub_runner_slots,
        }

    def advice(self) -> list[str]:
        """Settings that are valid but probably not what was meant; logged at start."""
        notes = [f"{name} in config.toml is ignored: {RETIRED[name]}" for name in self.retired]
        if self.export_max_gb > self.storage_dispatch_min_free_gb:
            notes.append(
                f"export_max_gb ({self.export_max_gb}) is above storage_dispatch_min_free_gb "
                f"({self.storage_dispatch_min_free_gb:g}): one export can fill the disk below "
                "the level where queued runs stop starting"
            )
        return notes

    @property
    def lock_file(self) -> Path:
        return self.data_dir / "maestro.lock"

    @property
    def setup_token_file(self) -> Path:
        return self.data_dir / "setup-token"

    def check(self) -> "Settings":
        if self.runner_python is not None and not self.runner_python.is_absolute():
            raise MaestroError("CONFIG_INVALID", "runner_python must be an absolute path", 500)
        for name in ("data_dir", "cache_dir", "master_key_dir"):
            if not getattr(self, name).is_absolute():
                raise MaestroError("CONFIG_INVALID", f"{name} must be an absolute path", 500)
        data = self.data_dir.resolve()
        keys = self.master_key_dir.resolve()
        if keys.is_relative_to(data) or data.is_relative_to(keys):
            # Backups copy data_dir; the master key must travel separately (O-08).
            raise MaestroError("CONFIG_INVALID", "master_key_dir must be outside data_dir", 500)
        if not re.fullmatch(r"[A-Za-z0-9.-]+:[0-9]{1,5}", self.image_registry):
            raise MaestroError("CONFIG_INVALID", "image_registry must be host:port", 500)
        self._check_limits()
        return self

    def _check_limits(self) -> None:
        for name, (low, high) in TUNING.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise MaestroError(
                    "CONFIG_INVALID", f"{name} must be an integer from {low} to {high}", 500
                )
        if self.artifact_file_max_mb > self.artifact_attempt_max_mb:
            raise MaestroError(
                "CONFIG_INVALID",
                "artifact_file_max_mb cannot be larger than artifact_attempt_max_mb",
                500,
            )
        if self.build_timeout_default_sec > self.build_timeout_max_sec:
            raise MaestroError(
                "CONFIG_INVALID",
                "build_timeout_default_sec cannot be larger than build_timeout_max_sec",
                500,
            )
        if self.runner_silence_sec < HEARTBEATS_BEFORE_LOST * self.runner_heartbeat_sec:
            raise MaestroError(
                "CONFIG_INVALID",
                f"runner_silence_sec must be at least {HEARTBEATS_BEFORE_LOST} times "
                "runner_heartbeat_sec",
                500,
            )


def load(path: Path | None = None) -> Settings:
    path = path or DEFAULT_CONFIG
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise MaestroError("CONFIG_INVALID", f"Cannot read {path}: {error}", 500) from None
    known = set(Settings.__dataclass_fields__) - {"retired"}
    unknown = set(raw) - known - set(RETIRED)
    if unknown:
        raise MaestroError("CONFIG_INVALID", f"Unknown settings: {sorted(unknown)}", 500)
    values = {k: v for k, v in raw.items() if k not in RETIRED}
    values["retired"] = tuple(sorted(set(raw) & set(RETIRED)))
    for name in ("data_dir", "cache_dir", "master_key_dir", "runner_python"):
        if values.get(name):
            values[name] = Path(values[name])
        elif name == "runner_python":
            values.pop(name, None)
    try:
        return Settings(**values).check()
    except TypeError as error:
        raise MaestroError("CONFIG_INVALID", str(error), 500) from None
