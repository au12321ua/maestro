"""The whole life of an installation, through release packages and maestroctl: install,
status, backup, restore, update, failed updates rolled back, an update started in the web
pages (from a fake GitHub on 127.0.0.1, through the systemd path unit), uninstall keeping
data, reinstall on it, purge.

Opt-in, because it builds packages, installs Python environments with uv and registers
a systemd *user* unit (``--test-root``: no root, no ``maestro`` account; the real system
install differs in the account, ownership and the unit's sandboxing):

    MAESTRO_INSTALL_TESTS=1 uv run pytest -q tests/maestro/live/test_install.py

Needs systemd with a user manager, uv, and a reachable Docker daemon (the install checks
it; set DOCKER_HOST for rootless Docker). Nothing outside the scratch root is touched
except the user unit, which the test removes.
"""

import hashlib
import http.cookiejar
import http.server
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from support import ROOT

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
        # A browser: the session cookie and the CSRF token of the administrator.
        self.browser = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.ProxyHandler({}),
        )
        self.csrf = None

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

    def api(self, path, body=None, method=None):
        headers = {"content-type": "application/json"}
        if self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method=method,
        )
        with self.browser.open(request, timeout=60) as response:
            return json.loads(response.read())

    def update_status(self, timeout=900, done=lambda status: status["pending"] is None):
        """Poll the admin page's update status through the restart an update causes."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                status = self.api("/api/v1/admin/update")["data"]
                if done(status):
                    return status
            except (OSError, urllib.error.URLError, ValueError):
                pass  # the service is restarting
            if time.monotonic() > deadline:
                raise AssertionError(f"update did not finish: {self.ctl('status').stdout}")
            time.sleep(2)

    def sql(self, statement, *values):
        with sqlite3.connect(self.root / "var/lib/maestro/app.db") as connection:
            connection.execute(statement, values)

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


class FakeGitHub:
    """The part of the GitHub API maestroctl and the service use: the latest release, a
    release by tag, and downloads of its assets."""

    def __init__(self, repository="test/maestro"):
        self.repository = repository
        self.releases: dict[str, dict] = {}
        self.files: dict[str, bytes] = {}
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                prefix = f"/repos/{fake.repository}/releases/"
                body = None
                if self.path == prefix + "latest" and fake.releases:
                    body = json.dumps(list(fake.releases.values())[-1]).encode()
                elif self.path.startswith(prefix + "tags/"):
                    found = fake.releases.get(self.path.removeprefix(prefix + "tags/"))
                    body = json.dumps(found).encode() if found else None
                elif self.path.startswith("/download/"):
                    body = fake.files.get(self.path.removeprefix("/download/"))
                self.send_response(200 if body is not None else 404)
                self.end_headers()
                self.wfile.write(body or b"{}")

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def publish(self, version: str, package: Path, *, corrupt=False) -> None:
        """Release ``package`` as ``version``: its RELEASE.json says that version (the
        service's own version stays that of its wheel)."""
        work = package.parent / f"as-{version}"
        top = _unpack(package, work)
        info = json.loads((top / "RELEASE.json").read_text())
        (top / "RELEASE.json").write_text(json.dumps({**info, "version": version}))
        name = f"maestro-{version}-test.tar.gz"
        with tarfile.open(work / name, "w:gz") as archive:
            archive.add(top, arcname=top.name)
        data = (work / name).read_bytes()
        shutil.rmtree(top)
        digest = hashlib.sha256(b"not the package" if corrupt else data).hexdigest()
        self.files[name] = data
        self.files[f"SHA256SUMS-{version}"] = f"{digest}  {name}\n".encode()
        tag = f"v{version}"
        self.releases[tag] = {
            "tag_name": tag, "name": tag, "draft": False, "prerelease": False,
            "html_url": f"{self.url}/{tag}", "published_at": "2026-11-07T00:00:00Z",
            "body": "## 更新后\n\nNothing to do.\n",
            "assets": [
                {"name": name, "browser_download_url": f"{self.url}/download/{name}"},
                {"name": "SHA256SUMS",
                 "browser_download_url": f"{self.url}/download/SHA256SUMS-{version}"},
            ],
        }  # fmt: skip


def _web_update(site: Installation, github: FakeGitHub, fourth: Path) -> None:
    """An update started in the web pages waits for in-flight work, then the root task
    started by the path unit installs it; a release whose digest does not match is
    refused and the service carries on."""
    with (site.root / "etc/maestro/config.toml").open("a") as config:
        config.write(f'update_repository = "{github.repository}"\n')
        config.write(f'update_api_url = "{github.url}"\n')
    site.ctl("restart")
    assert "update:   watching" in site.ctl("status").stdout
    github.publish("99.0.0", fourth)
    status = site.api("/api/v1/admin/update/check", {})["data"]
    assert status["update_available"] and status["latest"]["version"] == "99.0.0", status
    assert status["latest"]["highlights"][0]["title"] == "更新后"

    # An import in progress holds the update back; queued work would wait meanwhile.
    at = "2026-11-07T00:00:00Z"
    site.sql(
        "INSERT INTO operations (id, kind, status, progress_json, revision, created_at, "
        "updated_at) VALUES ('op_inflight', 'dataset_import', 'running', '{}', 1, ?, ?)",
        at, at,
    )  # fmt: skip
    status = site.api("/api/v1/admin/update/request", {"version": "99.0.0"})["data"]
    assert status["pending"]["state"] == "draining", status
    assert [o["id"] for o in status["in_flight"]["operations"]] == ["op_inflight"]
    time.sleep(6)
    assert not (site.root / "var/lib/maestro/update/request.json").exists()
    notice = site.api("/api/v1/system-status")["data"]["detail"]["update"]
    assert notice["state"] == "draining" and notice["version"] == "99.0.0"
    site.sql("UPDATE operations SET status = 'succeeded' WHERE id = 'op_inflight'")
    status = site.update_status()
    assert status["last"]["state"] == "succeeded", status
    assert site.version().endswith("-four") and site.users() == ["admin"]
    assert site.api("/api/v1/system-status")["data"]["detail"]["update"] is None

    # A digest that does not match: nothing is switched, the pending update ends.
    github.publish("99.0.1", fourth, corrupt=True)
    site.api("/api/v1/admin/update/check", {})
    site.api("/api/v1/admin/update/request", {"version": "99.0.1"})
    status = site.update_status()
    assert status["last"]["state"] == "failed", status
    assert "digest" in status["last"]["result"]["message"], status["last"]
    assert site.version().endswith("-four")
    assert site.api("/health/ready")["status"] == "ready"


def test_install_update_rollback_backup_and_uninstall(tmp_path):
    first = _package(tmp_path / "dist", "one")
    second = _package(tmp_path / "dist", "two")
    third = _package(tmp_path / "dist", "three")
    fourth = _package(tmp_path / "dist", "four")
    github = FakeGitHub()
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
        site.csrf = site.api("/api/v1/setup", {"setup_token": token, "username": "admin",
                                               "password": "correct-horse-battery"})["data"][
            "csrf_token"
        ]  # fmt: skip
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

        _web_update(site, github, fourth)

        # Backup and restore undo later changes, the master key staying outside the set.
        site.ctl("backup", "--label", "known")
        (known,) = (site.root / "var/backups/maestro").glob("*-known")
        assert not list(known.glob("master-*"))
        with sqlite3.connect(site.root / "var/lib/maestro/app.db") as connection:
            connection.execute("UPDATE users SET display_name = 'changed'")
        site.ctl("restore", str(known))
        with sqlite3.connect(site.root / "var/lib/maestro/app.db") as connection:
            assert connection.execute("SELECT display_name FROM users").fetchone()[0] == "admin"

        # Garbage collection: nothing to do while nothing is stored; then an image whose
        # manifest is deleted (as a purge does) gives its space back.
        assert "nothing to collect" in site.ctl("registry-gc").stdout
        registry = f"127.0.0.1:{registry_port}"
        pushed = f"{registry}/maestro-prep/gc-check:1"
        for cmd in (["tag", "ubuntu:22.04", pushed], ["push", "-q", pushed]):
            subprocess.run(["docker", *cmd], capture_output=True, check=True, timeout=600)
        local = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        digest = local.open(
            urllib.request.Request(
                f"http://{registry}/v2/maestro-prep/gc-check/manifests/1",
                method="HEAD",
                headers={"Accept": "application/vnd.oci.image.manifest.v1+json, "
                         "application/vnd.docker.distribution.manifest.v2+json"},
            ),
            timeout=10,
        ).headers["Docker-Content-Digest"]  # fmt: skip
        local.open(
            urllib.request.Request(
                f"http://{registry}/v2/maestro-prep/gc-check/manifests/{digest}", method="DELETE"
            ),
            timeout=10,
        )
        subprocess.run(["docker", "image", "rm", pushed], capture_output=True, check=False)
        before, after = site.ctl("registry-gc").stdout.rsplit("registry ", 1)[1].split(" MiB -> ")
        assert float(after.split()[0].replace(",", "")) < float(before.replace(",", ""))
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
        github.server.shutdown()
        if (site.root / "opt/maestro/current").exists():
            subprocess.run(
                [sys.executable, "-I", str(site.root / "opt/maestro/current/maestroctl"),
                 "--test-root", str(site.root), "uninstall"],
                capture_output=True, check=False,
            )  # fmt: skip
