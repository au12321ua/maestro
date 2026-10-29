"""The whole life of an installation, through release packages and maestroctl (12, 13
"Linux 运维"): install, status, backup, restore, update, failed updates rolled back,
uninstall keeping data, reinstall on it, purge.

Opt-in, because it builds packages, installs Python environments with uv and registers
a systemd *user* unit (``--test-root``: no root, no ``maestro`` account; the real system
install differs in the account, ownership and the unit's sandboxing):

    MAESTRO_INSTALL_TESTS=1 uv run pytest -q tests/maestro/test_install.py

Needs systemd with a user manager, uv, and a reachable Docker daemon (the install checks
it; set DOCKER_HOST for rootless Docker). Nothing outside the scratch root is touched
except the user unit, which the test removes.
"""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    os.environ.get("MAESTRO_INSTALL_TESTS") != "1", reason="set MAESTRO_INSTALL_TESTS=1"
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _package(out: Path, label: str) -> Path:
    result = subprocess.run(
        [sys.executable, "tools/build_release.py", "--allow-dirty", "--label", label,
         "--out", str(out)],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )  # fmt: skip
    return Path(result.stdout.strip())


class Installation:
    def __init__(self, root: Path, port: int):
        self.root = root
        self.port = port

    def ctl(self, *args, script=None, env=None, check=True):
        script = script or self.root / "opt/maestro/current/maestroctl"
        result = subprocess.run(
            [sys.executable, "-I", str(script), "--test-root", str(self.root), *args],
            cwd="/", capture_output=True, text=True, env={**os.environ, **(env or {})}, check=False,
        )  # fmt: skip
        if check and result.returncode:
            raise AssertionError(
                f"maestroctl {args} -> {result.returncode}\n{result.stdout}\n{result.stderr}"
            )
        return result

    def api(self, path, body=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            headers={"content-type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def users(self) -> list[str]:
        with sqlite3.connect(self.root / "var/lib/maestro/app.db") as connection:
            return [r[0] for r in connection.execute("SELECT username FROM users ORDER BY 1")]

    def version(self) -> str:
        return (self.root / "opt/maestro/current").resolve().name


def _unpack(package: Path, into: Path) -> Path:
    into.mkdir()
    with tarfile.open(package) as archive:
        archive.extractall(into, filter="data")
    (top,) = into.iterdir()
    return top


def test_install_update_rollback_backup_and_uninstall(tmp_path):
    first = _package(tmp_path / "dist", "one")
    second = _package(tmp_path / "dist", "two")
    third = _package(tmp_path / "dist", "three")
    site = Installation(tmp_path / "root", _free_port())
    release = _unpack(first, tmp_path / "unpacked")
    registry_port = _free_port()
    flags = ["--port", str(site.port), "--insecure-cookie", "--registry-port", str(registry_port)]
    if os.environ.get("DOCKER_HOST"):
        flags += ["--docker-host", os.environ["DOCKER_HOST"]]
    try:
        installed = site.ctl("install", *flags, script=release / "maestroctl")
        token = installed.stdout.split("shown once):\n")[1].split()[0]
        assert token.startswith("mst_set_") and installed.stdout.count(token) == 1
        site.api("/api/v1/setup", {"setup_token": token, "username": "admin",
                                   "password": "correct-horse-battery"})  # fmt: skip
        status = site.ctl("status").stdout
        assert "health:   ready" in status and first.name.removeprefix("maestro-")[:-7] in status
        # The prepared-image registry runs beside the service, its data under data_dir.
        assert f"registry: up at 127.0.0.1:{registry_port}" in status
        assert (site.root / "var/lib/maestro/registry").is_dir()
        # The service reads only what the installer wrote; the program is not its data.
        assert (site.root / "etc/maestro/secrets").stat().st_mode & 0o077 == 0
        assert "Application startup complete" in site.ctl("logs", "-n", "50").stdout

        # Update: the new version is installed beside the running one, the database is
        # backed up first, and the Runner environment is reused (same Harbor and code).
        site.ctl("update", str(second))
        assert site.version().endswith("-two") and site.users() == ["admin"]
        assert len(list((site.root / "opt/maestro/harbor/0.24.0").iterdir())) == 1
        backups = sorted((site.root / "var/backups/maestro").iterdir())
        assert backups[-1].name.endswith("before-" + site.version())

        # A failed migration or start puts back the old version and the old database.
        for point in ("migrate", "start"):
            failed = site.ctl("update", str(third), env={"MAESTRO_CTL_FAIL_AT": point},
                              check=False)  # fmt: skip
            assert failed.returncode == 1 and "rolled back" in failed.stdout, failed.stdout
            assert site.version().endswith("-two") and site.users() == ["admin"]
            assert site.api("/health/ready")["status"] == "ready"

        # Backup and restore undo later changes, the master key staying outside the set.
        site.ctl("backup", "--label", "known")
        (known,) = (site.root / "var/backups/maestro").glob("*-known")
        assert not list(known.glob("master-*"))
        with sqlite3.connect(site.root / "var/lib/maestro/app.db") as connection:
            connection.execute("UPDATE users SET display_name = 'changed'")
        site.ctl("restore", str(known))
        with sqlite3.connect(site.root / "var/lib/maestro/app.db") as connection:
            assert connection.execute("SELECT display_name FROM users").fetchone()[0] == "admin"

        # Garbage collection runs with nothing to free.
        assert "registry " in site.ctl("registry-gc").stdout
        assert "registry: up" in site.ctl("status").stdout

        # Uninstall keeps configuration, key and data; installing again picks them up.
        site.ctl("uninstall")
        assert not subprocess.run(
            ["docker", "ps", "-q", "--filter", f"publish={registry_port}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()  # fmt: skip
        assert not (site.root / "opt/maestro").exists()
        assert (site.root / "var/lib/maestro/app.db").exists()
        assert list((site.root / "etc/maestro/secrets").iterdir())
        again = site.ctl("install", *flags, script=release / "maestroctl")
        assert "An administrator already exists" in again.stdout
        assert site.users() == ["admin"]

        # Purge lists what goes and needs the instance id typed back.
        refused = site.ctl("uninstall", "--purge", check=False)
        assert refused.returncode == 1 and "Nothing was removed" in refused.stdout
        instance = refused.stdout.split("--confirm ")[1].split()[0]
        site.ctl("uninstall", "--purge", "--confirm", instance)
        assert not any((site.root / p).exists() for p in ("opt/maestro", "etc/maestro",
                                                            "var/lib/maestro"))  # fmt: skip
    finally:
        if (site.root / "opt/maestro/current").exists():
            subprocess.run(
                [sys.executable, "-I", str(site.root / "opt/maestro/current/maestroctl"),
                 "--test-root", str(site.root), "uninstall"],
                capture_output=True, check=False,
            )  # fmt: skip
