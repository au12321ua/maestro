"""maestroctl's own rules, without installing anything (the full install, update and
uninstall run in live/test_install.py, opt-in)."""

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tarfile
import threading
import tomllib
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from support import ROOT

from maestro import config


def _load():
    spec = importlib.util.spec_from_file_location("maestroctl", ROOT / "ops" / "maestroctl.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ctl = _load()
SYSTEM = ctl.Layout(Path("/"))


def _args(**values):
    defaults = {
        "listen_host": "127.0.0.1",
        "port": 8420,
        "insecure_cookie": False,
        "proxy": None,
        "no_proxy": None,
        "docker_host": None,
        "registry_port": 8425,
    }
    return argparse.Namespace(**(defaults | values))


def test_system_layout_is_the_designed_one():
    assert SYSTEM.current == Path("/opt/maestro/current")
    assert SYSTEM.config == Path("/etc/maestro/config.toml")
    assert SYSTEM.secrets == Path("/etc/maestro/secrets")
    assert SYSTEM.data == Path("/var/lib/maestro")
    assert SYSTEM.cache == Path("/var/cache/maestro")
    assert SYSTEM.unit_path == Path("/etc/systemd/system/maestro.service")
    assert SYSTEM.ctl_link == Path("/usr/local/sbin/maestroctl")


def test_system_unit_runs_as_the_service_account_with_the_current_version():
    unit = ctl.render_unit(SYSTEM)
    assert "User=maestro\nGroup=maestro\n" in unit
    assert "ExecStart=/opt/maestro/current/venv/bin/maestro serve\n" in unit
    assert "Environment=MAESTRO_CONFIG=/etc/maestro/config.toml\n" in unit
    assert "EnvironmentFile=-/etc/maestro/environment\n" in unit
    assert "Restart=on-failure\n" in unit and "UMask=0077\n" in unit
    # The program is read-only to the service; only the key directory under /etc is not,
    # because rotating the master key writes there.
    assert "ProtectSystem=full\n" in unit and "ReadWritePaths=/etc/maestro/secrets\n" in unit
    assert "WantedBy=multi-user.target" in unit


def test_test_root_unit_is_a_user_unit_without_account(tmp_path):
    layout = ctl.Layout(tmp_path, test=True)
    unit = ctl.render_unit(layout)
    assert "User=" not in unit and "ProtectSystem" not in unit
    assert f"ExecStart={tmp_path}/opt/maestro/current/venv/bin/maestro serve" in unit
    assert layout.unit_name.startswith("maestro-test-") and layout.unit_name != SYSTEM.unit_name
    assert "systemd/user" in str(layout.unit_path)


def test_written_config_is_accepted_by_the_service(tmp_path, monkeypatch):
    layout = ctl.Layout(tmp_path, test=True)
    text = ctl.render_config(layout, _args(port=8431, insecure_cookie=True))
    path = tmp_path / "config.toml"
    path.write_text(text)
    settings = config.load(path)
    assert settings.data_dir == layout.data and settings.master_key_dir == layout.secrets
    assert settings.runner_python == layout.current / "runner-env/venv/bin/python"
    assert settings.listen_port == 8431 and settings.cookie_secure is False
    assert settings.image_registry == "127.0.0.1:8425"
    assert tomllib.loads(ctl.render_config(layout, _args()))["cookie_secure"] is True


def test_compose_versions_are_read_for_the_override_files():
    assert ctl.compose_version("Docker Compose version v2.29.1") == (2, 29)
    assert ctl.compose_version("Docker Compose version 5.6.1") == (5, 6)
    assert ctl.compose_version("Docker Compose version v2.23.3") < ctl.COMPOSE_MIN
    assert ctl.compose_version("missing") == (0, 0)


def test_the_registry_image_is_pinned_and_its_container_named_per_layout(tmp_path):
    assert re.fullmatch(r"registry:[0-9.]+@sha256:[0-9a-f]{64}", ctl.REGISTRY_IMAGE)
    assert ctl.Layout(Path("/")).registry_name == "maestro-registry"
    assert ctl.Layout(Path("/")).registry_dir == Path("/var/lib/maestro/registry")
    test = ctl.Layout(tmp_path, test=True).registry_name
    assert test.startswith("maestro-registry-test-") and test != "maestro-registry"


def test_check_reports_the_daemon_proxy_and_registry_mirrors():
    bare = {c.name: c for c in ctl.daemon_checks({}, proxy_wanted=True)}
    assert bare["docker proxy"].status == "warn"
    assert bare["docker mirrors"].status == "ok" and "none" in bare["docker mirrors"].detail
    info = {
        "HttpsProxy": "http://10.0.0.1:3128",
        "RegistryConfig": {"Mirrors": ["https://m.example/"]},
    }
    found = {c.name: c for c in ctl.daemon_checks(info, proxy_wanted=True)}
    assert "docker proxy" not in found
    assert found["docker mirrors"].detail == "https://m.example/"


def test_environment_file_carries_proxies_and_docker_host():
    text = ctl.render_environment(
        _args(proxy="http://10.0.0.1:7890", docker_host="unix:///run/user/1000/docker.sock")
    )
    lines = set(text.splitlines())
    assert {"HTTPS_PROXY=http://10.0.0.1:7890", "https_proxy=http://10.0.0.1:7890"} <= lines
    assert "NO_PROXY=127.0.0.1,localhost,::1" in lines
    assert "DOCKER_HOST=unix:///run/user/1000/docker.sock" in lines
    assert ctl.render_environment(_args()).count("=") == 0


def test_only_the_layouts_own_directories_can_be_deleted(tmp_path):
    layout = ctl.Layout(tmp_path / "root", test=True)
    layout.data.mkdir(parents=True)
    (layout.data / "app.db").write_text("x")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for path in (Path("/"), tmp_path, elsewhere, layout.data / "app.db", layout.data.parent):
        with pytest.raises(ctl.CtlError):
            ctl.safe_rmtree(layout, path)
    layout.cache.parent.mkdir(parents=True)
    layout.cache.symlink_to(elsewhere)
    with pytest.raises(ctl.CtlError, match="symbolic link"):
        ctl.safe_rmtree(layout, layout.cache)
    assert elsewhere.exists()
    ctl.safe_rmtree(layout, layout.data)
    assert not layout.data.exists()


def _release(directory: Path, build="1.0-abc") -> "ctl.Release":
    directory.mkdir()
    (directory / "maestroctl").write_text("#!/usr/bin/env python3\n")
    files = {"maestroctl": ctl.sha256_file(directory / "maestroctl")}
    info = {"name": "maestro", "build": build, "files": files}
    (directory / "RELEASE.json").write_text(json.dumps(info))
    return ctl.Release.load(directory)


def test_release_files_are_verified(tmp_path):
    release = _release(tmp_path / "rel")
    release.verify()
    (tmp_path / "rel" / "maestroctl").write_text("tampered\n")
    with pytest.raises(ctl.CtlError, match="does not match"):
        release.verify()
    with pytest.raises(ctl.CtlError, match="not a Maestro release"):
        ctl.Release.load(tmp_path)
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "RELEASE.json").write_text(json.dumps({"name": "maestro", "build": "../x"}))
    with pytest.raises(ctl.CtlError):
        ctl.Release.load(bad)


def test_switching_current_is_atomic_and_pruning_keeps_what_is_used(tmp_path):
    layout = ctl.Layout(tmp_path, test=True)
    runners = {}
    for name, revision in (("v1", "r1"), ("v2", "r1"), ("v3", "r2")):
        runner = layout.harbor / "0.24.0" / revision
        runner.mkdir(parents=True, exist_ok=True)
        runners[name] = runner
        version = layout.versions / name
        version.mkdir(parents=True)
        (version / "runner-env").symlink_to(os.path.relpath(runner, version))
    ctl.switch_current(layout, layout.versions / "v2")
    ctl.switch_current(layout, layout.versions / "v3")
    assert ctl.current_version(layout) == layout.versions / "v3"
    removed = ctl.prune_versions(layout, [layout.versions / "v3", layout.versions / "v2"])
    assert removed == ["v1"]
    assert runners["v2"].exists() and runners["v3"].exists()
    removed = ctl.prune_versions(layout, [layout.versions / "v3"])
    assert removed == ["v2", "runner 0.24.0/r1"]


def test_failure_injection_is_refused_outside_a_test_root(monkeypatch, capsys):
    monkeypatch.setenv("MAESTRO_CTL_FAIL_AT", "start")
    assert ctl.main(["status"]) == 2
    assert "only works with --test-root" in capsys.readouterr().err


def test_commands_needing_root_say_so(tmp_path, monkeypatch, capsys):
    if os.geteuid() == 0:
        pytest.skip("running as root")
    for command in (["install"], ["update"], ["uninstall"], ["backup"], ["restart"]):
        assert ctl.main(command) == 1
        assert "needs root" in capsys.readouterr().err


def test_downloads_go_direct_unless_a_download_proxy_is_asked_for(tmp_path):
    service = "http://127.0.0.1:7890"
    assert ctl.download_env(argparse.Namespace(download_proxy=None), service) == {}
    alone = ctl.download_env(argparse.Namespace(download_proxy=ctl.SERVICE_PROXY), service)
    assert alone["HTTPS_PROXY"] == alone["http_proxy"] == service
    other = ctl.download_env(argparse.Namespace(download_proxy="http://10.0.0.9:3128"), service)
    assert other["HTTPS_PROXY"] == "http://10.0.0.9:3128"
    with pytest.raises(ctl.CtlError, match="needs a service proxy"):
        ctl.download_env(argparse.Namespace(download_proxy=ctl.SERVICE_PROXY), None)
    with pytest.raises(ctl.CtlError, match="package path before"):
        ctl.download_env(argparse.Namespace(download_proxy="maestro-1.tar.gz"), service)
    # On update, "the service's proxy" is read back from the environment file.
    layout = ctl.Layout(tmp_path, test=True)
    layout.etc.mkdir(parents=True)
    layout.environment.write_text(ctl.render_environment(_args(proxy=service)))
    assert ctl.service_proxy(layout) == service


@pytest.mark.parametrize("test", [False, True])
def test_update_units_are_independent_and_watch_the_request(tmp_path, monkeypatch, test):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "units"))
    monkeypatch.setattr(ctl.shutil, "which", lambda _: "/test-tools/docker")
    layout = ctl.Layout(tmp_path, test=True) if test else SYSTEM
    path = ctl.render_update_path(layout)
    service = ctl.render_update_service(layout)
    assert f"PathExists={layout.data}/update/request.json\n" in path
    assert f"Unit={layout.update_service_name}\n" in path
    assert f"WantedBy={'default.target' if test else 'paths.target'}\n" in path
    command = f"ExecStart={sys.executable} -I {layout.current}/maestroctl"
    if test:
        command += f" --test-root {layout.root}"
    assert f"{command} update --request\n" in service
    assert "Type=oneshot\n" in service and "TimeoutStartSec=3h\n" in service
    assert "Environment=LANG=C.UTF-8\n" in service
    assert "[Install]" not in service and "User=" not in service
    assert "BindsTo=" not in service and "PartOf=" not in service
    assert ("Environment=PATH=/test-tools:" in service) is test
    assert layout.update_path == layout.unit_path.with_name(layout.update_path_name)
    assert layout.update_service_path == layout.unit_path.with_name(layout.update_service_name)
    if test:
        assert re.fullmatch(r"maestro-test-[0-9a-f]{10}-update.path", layout.update_path_name)
    else:
        assert layout.update_path == Path("/etc/systemd/system/maestro-update.path")
        assert layout.update_service_name == "maestro-update.service"


def test_units_are_written_together_and_rollback_restores_or_removes_them(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "units"))
    layout = ctl.Layout(tmp_path, test=True)
    host = ctl.Host(layout)
    calls = []
    monkeypatch.setattr(host, "systemctl", lambda *args, **kwargs: calls.append(args))
    layout.unit_path.parent.mkdir(parents=True)
    layout.unit_path.write_text("old service\n")
    previous = {p: p.read_text() if p.exists() else None for p in layout.unit_paths}
    ctl.write_unit(host)
    assert all(p.is_file() and p.stat().st_mode & 0o777 == 0o644 for p in layout.unit_paths)
    assert calls == [("daemon-reload",), ("enable", "--now", layout.update_path_name)]
    ctl.restore_units(host, previous)
    assert layout.unit_path.read_text() == "old service\n"
    assert not layout.update_path.exists() and not layout.update_service_path.exists()
    assert calls[-2:] == [("disable", "--now", layout.update_path_name), ("daemon-reload",)]
    ctl.write_unit(host)
    previous = {p: p.read_text() for p in layout.unit_paths}
    for p in layout.unit_paths:
        p.write_text("changed\n")
    ctl.restore_units(host, previous)
    assert {p: p.read_text() for p in layout.unit_paths} == previous


def test_update_directory_is_private_and_service_owned(tmp_path, monkeypatch):
    layout = ctl.Layout(tmp_path, test=True)
    host = ctl.Host(layout)
    owned = []
    original = host.own

    def own(path, mode):
        owned.append((path, mode))
        original(path, mode)

    monkeypatch.setattr(host, "own", own)
    ctl.make_directories(host)
    assert (layout.update_dir, 0o700) in owned
    assert layout.update_dir.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    ("older", "newer"),
    [
        ("0.4.0", "0.5.0.dev0"),
        ("0.5.0.dev9", "0.5.0a0"),
        ("0.5.0a2", "0.5.0b0"),
        ("0.5.0b9", "0.5.0rc0"),
        ("0.5.0rc10", "0.5.0"),
        ("0.5.0.dev0", "0.5.0"),
        ("0.5.0", "0.5.1.dev0"),
        ("0.9.0", "0.10.0"),
    ],
)
def test_release_version_order(older, newer):
    assert ctl.version_key(older) < ctl.version_key(newer)
    assert ctl.version_key(older + "+local") == ctl.version_key(older)
    assert not ctl.version_key(newer) > ctl.version_key(newer)


@pytest.mark.parametrize("version", ["v0.5.0", "../x", "0.5", "0.5.0.dev", None])
def test_unknown_version_forms_are_admin_errors(version):
    with pytest.raises(ctl.CtlError, match="unsupported release version"):
        ctl.version_key(version)


def _installed(tmp_path, version="0.4.0"):
    layout = ctl.Layout(tmp_path, test=True)
    layout.versions.mkdir(parents=True)
    old = _release(layout.versions / "0.4.0-abc", "0.4.0-abc")
    (old.directory / ctl.RELEASE_FILE).write_text(json.dumps(old.info | {"version": version}))
    ctl.switch_current(layout, old.directory)
    layout.etc.mkdir(parents=True)
    layout.config.write_text("")
    layout.update_dir.mkdir(parents=True)
    return layout


def _update_args(layout, *args):
    return ctl.parser().parse_args(["--test-root", str(layout.root), "update", *args])


def _request(layout, **values):
    request = {
        "format": 1,
        "id": "upd_test",
        "version": "0.5.0",
        "interrupt_running": False,
        "requested_by": "usr_test",
        "requested_at": "2026-11-07T10:00:00Z",
    } | values
    (layout.update_dir / "request.json").write_text(json.dumps(request))


def test_missing_request_does_nothing(tmp_path):
    layout = ctl.Layout(tmp_path, test=True)
    assert ctl.cmd_update(layout, _update_args(layout, "--request")) == 0
    assert not layout.update_dir.exists()


@pytest.mark.parametrize(
    ("changes", "message", "identifier"),
    [
        ({"format": True}, "format", "upd_test"),
        ({"format": 2}, "format", "upd_test"),
        ({"format": 1.0}, "format", "upd_test"),
        ({"version": "0.5.0.dev1"}, "final version", "upd_test"),
        ({"version": "../x"}, "final version", "upd_test"),
        ({"version": None}, "final version", "upd_test"),
        ({"id": "../x"}, "id", None),
        ({"id": "x" * 65}, "id", None),
        ({"id": ""}, "id", None),
        ({"interrupt_running": 1}, "bool", "upd_test"),
    ],
)
def test_invalid_request_is_claimed_and_reports_failure(tmp_path, changes, message, identifier):
    layout = _installed(tmp_path)
    _request(layout, **changes)
    assert ctl.cmd_update(layout, _update_args(layout, "--request")) == 1
    status = json.loads((layout.update_dir / "status.json").read_text())
    assert status["state"] == "failed" and status["id"] == identifier
    assert message in status["message"]
    assert status["pid"] == os.getpid() and status["finished_at"]
    assert not (layout.update_dir / "request.json").exists()
    assert not (layout.update_dir / "request.taken.json").exists()


@pytest.mark.parametrize("kind", ["symlink", "oversize", "fifo", "invalid-json", "array"])
def test_unsafe_request_is_refused_without_following_or_blocking(tmp_path, monkeypatch, kind):
    layout = _installed(tmp_path)
    request = layout.update_dir / "request.json"
    target = tmp_path / "untouched"
    target_text = json.dumps(
        {"format": 1, "id": "upd_target", "version": "0.5.0", "interrupt_running": False}
    )
    target.write_text(target_text)
    monkeypatch.setattr(
        ctl, "update_release", lambda *a: pytest.fail("must not execute an unsafe request")
    )
    if kind == "symlink":
        request.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(request)
    else:
        request.write_text({"oversize": " " * 4097, "invalid-json": "{", "array": "[]"}[kind])
    assert ctl.cmd_update(layout, _update_args(layout, "--request")) == 1
    status = json.loads((layout.update_dir / "status.json").read_text())
    assert status["state"] == "failed" and status["id"] is None
    assert target.read_text() == target_text
    assert not request.exists() and not request.is_symlink()
    assert not (layout.update_dir / "request.taken.json").exists()
    if kind == "oversize":
        assert "4096" in status["message"]


def test_status_replaces_a_symlink_atomically_and_sets_ownership(tmp_path, monkeypatch):
    layout = _installed(tmp_path)
    host = ctl.Host(layout)
    target = tmp_path / "untouched"
    target.write_text("must stay\n")
    path = layout.update_dir / "status.json"
    path.symlink_to(target)
    original = ctl.os.replace
    replacements = []
    owned = []
    monkeypatch.setattr(host, "own", lambda p, mode: owned.append((p, mode)))

    def replace(source, destination):
        source = Path(source)
        assert source.parent == layout.update_dir and source != path
        assert source.stat().st_mode & 0o777 == 0o644
        assert json.loads(source.read_text()) == {"state": "failed"}
        assert owned == [(source, 0o644)]
        replacements.append(destination)
        original(source, destination)

    monkeypatch.setattr(ctl.os, "replace", replace)
    ctl.write_update_status(host, {"state": "failed"})
    assert replacements == [path] and not path.is_symlink()
    assert json.loads(path.read_text()) == {"state": "failed"}
    assert target.read_text() == "must stay\n"
    assert list(layout.update_dir.iterdir()) == [path]


@pytest.fixture
def github_release(tmp_path):
    layout = _installed(tmp_path / "install")
    release = _release(tmp_path / "release", "0.5.0-def")
    (release.directory / ctl.RELEASE_FILE).write_text(
        json.dumps(release.info | {"version": "0.5.0"})
    )
    archive = tmp_path / "maestro-0.5.0-def.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        output.add(release.directory, arcname="release")
    package = archive.read_bytes()
    state = {
        "metadata": {"tag_name": "v0.5.0", "draft": False, "prerelease": False},
        "files": {
            "/package": package,
            "/SHA256SUMS": f"{hashlib.sha256(package).hexdigest()}  {archive.name}\n".encode(),
        },
        "requests": [],
        "layout": layout,
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append((self.path, self.headers))
            if self.path == "/repos/team/maestro/releases/tags/v0.5.0":
                body = json.dumps(state["metadata"]).encode()
            elif self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/package")
                self.end_headers()
                return
            elif self.path in state["files"]:
                body = state["files"][self.path]
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}"
    state["metadata"]["assets"] = [
        {"name": archive.name, "browser_download_url": url + "/redirect"},
        {"name": "SHA256SUMS", "browser_download_url": url + "/SHA256SUMS"},
    ]
    state["url"] = url
    layout.config.write_text(f'update_repository = "team/maestro"\nupdate_api_url = "{url}/"\n')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_release_download_verifies_then_hands_over_and_cleans_only_its_directory(
    github_release, monkeypatch
):
    state = github_release
    layout = state["layout"]
    layout.incoming.mkdir()
    kept = layout.incoming / "unrelated"
    kept.mkdir()
    (kept / "keep").write_text("keep")
    # Neither root's proxy nor root's bypass list may control the downloader.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "*")
    layout.environment.write_text("HTTP_PROXY=http://127.0.0.1:1\nNO_PROXY=127.0.0.1\n")
    handed = []

    def hand_over(received_layout, release, args, *, capture):
        assert received_layout == layout and capture is False
        assert release.info["version"] == "0.5.0" and release.build == "0.5.0-def"
        release.verify()
        assert release.directory.is_relative_to(layout.incoming)
        handed.append(release.directory)
        return ctl.HandoverResult(0)

    monkeypatch.setattr(ctl, "hand_over", hand_over)
    assert ctl.cmd_update(layout, _update_args(layout, "--release", "0.5.0")) == 0
    assert len(handed) == 1 and not handed[0].exists()
    assert list(layout.incoming.iterdir()) == [kept]
    assert (kept / "keep").read_text() == "keep"
    paths = [path for path, _ in state["requests"]]
    assert paths == [
        "/repos/team/maestro/releases/tags/v0.5.0",
        "/redirect",
        "/package",
        "/SHA256SUMS",
    ]
    for _, headers in state["requests"]:
        assert headers["Accept"] == "application/vnd.github+json"
        assert headers["User-Agent"] == "maestroctl/0.4.0-abc"


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("prerelease", "final release"),
        ("draft", "final release"),
        ("tag", "final release"),
        ("digest", "digest mismatch"),
        ("missing-digest", "digest mismatch"),
        ("duplicate-digest", "digest mismatch"),
        ("older", "not newer than the installed"),
        ("same", "not newer than the installed"),
        ("missing-package", "exactly one"),
        ("missing-sums", "exactly one"),
        ("duplicate-package", "exactly one"),
    ],
)
def test_invalid_github_release_never_hands_over(github_release, monkeypatch, failure, message):
    state = github_release
    layout = state["layout"]
    metadata = state["metadata"]
    if failure in ("draft", "prerelease"):
        metadata[failure] = True
    elif failure == "tag":
        metadata["tag_name"] = "v9.0.0"
    elif failure in ("digest", "missing-digest", "duplicate-digest"):
        digest = state["files"]["/SHA256SUMS"]
        state["files"]["/SHA256SUMS"] = {
            "digest": b"0" * 64 + digest[64:],
            "missing-digest": b"",
            "duplicate-digest": digest + digest,
        }[failure]
    elif failure in ("same", "older"):
        path = ctl.current_version(layout) / ctl.RELEASE_FILE
        info = json.loads(path.read_text())
        path.write_text(
            json.dumps(info | {"version": "0.5.0" if failure == "same" else "0.6.0.dev0"})
        )
    elif failure == "missing-package":
        metadata["assets"] = metadata["assets"][1:]
    elif failure == "missing-sums":
        metadata["assets"] = metadata["assets"][:1]
    else:
        metadata["assets"].append(metadata["assets"][0])
    monkeypatch.setattr(ctl, "hand_over", lambda *a, **k: pytest.fail("must not hand over"))
    with pytest.raises(ctl.CtlError, match=message):
        ctl.cmd_update(layout, _update_args(layout, "--release", "0.5.0"))
    assert not list(layout.incoming.iterdir())


@pytest.mark.parametrize("version", ["0.5.0.dev1", "../x", "v0.5.0"])
def test_release_argument_only_accepts_final_versions(tmp_path, version):
    layout = _installed(tmp_path)
    with pytest.raises(ctl.CtlError, match="final version"):
        ctl.cmd_update(layout, _update_args(layout, "--release", version))
    assert not layout.incoming.exists()


@pytest.mark.parametrize("outcome", ["success", "rollback", "ctl-error", "unexpected"])
def test_web_request_publishes_progress_and_terminal_result(tmp_path, monkeypatch, outcome):
    layout = _installed(tmp_path)
    release = _release(tmp_path / "downloaded", "0.5.0-def")
    monkeypatch.setattr(ctl, "downloaded_release", lambda *a: contextlib.nullcontext(release))
    _request(layout, interrupt_running=True)
    layout.environment.write_text("HTTPS_PROXY=http://127.0.0.1:1\nNO_PROXY=127.0.0.1\n")
    statuses = []
    original = ctl.write_update_status

    def write(host, status):
        statuses.append(dict(status))
        original(host, status)

    def hand_over(received_layout, release, args, *, capture):
        assert capture and received_layout == layout and args.interrupt_running is True
        assert args.download_proxy == ctl.SERVICE_PROXY
        assert not (layout.update_dir / "request.json").exists()
        assert not (layout.update_dir / "request.taken.json").exists()
        progress = json.loads((layout.update_dir / "status.json").read_text())
        assert progress["phase"] == "installing" and progress["state"] == "running"
        if outcome == "ctl-error":
            raise ctl.CtlError("cannot start installer")
        if outcome == "unexpected":
            raise RuntimeError("unexpected installer crash")
        return ctl.HandoverResult(
            0 if outcome == "success" else 1, "!! update failed: migration", outcome == "rollback"
        )

    monkeypatch.setattr(ctl, "write_update_status", write)
    monkeypatch.setattr(ctl, "hand_over", hand_over)
    assert ctl.cmd_update(layout, _update_args(layout, "--request")) == (
        0 if outcome == "success" else 1
    )
    result = json.loads((layout.update_dir / "status.json").read_text())
    assert result["id"] == "upd_test" and result["version"] == "0.5.0"
    assert result["from_build"] == "0.4.0-abc" and result["pid"] == os.getpid()
    assert statuses[0]["state"] == "running" and statuses[0]["phase"] == "downloading"
    assert statuses[0]["finished_at"] is None
    assert result["phase"] == "done"
    assert result["state"] == ("succeeded" if outcome == "success" else "failed")
    assert result["to_build"] == ("0.5.0-def" if outcome == "success" else None)
    assert result["rolled_back"] is (outcome == "rollback")
    assert (
        result["message"]
        == {
            "success": "Updated to 0.5.0-def",
            "rollback": "!! update failed: migration",
            "ctl-error": "cannot start installer",
            "unexpected": "unexpected installer crash",
        }[outcome]
    )
    for name in ("started_at", "finished_at"):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", result[name])


def test_web_requests_are_refused_when_update_checks_are_off(tmp_path, monkeypatch):
    layout = _installed(tmp_path)
    layout.config.write_text("update_check = false\n")
    _request(layout)
    monkeypatch.setattr(ctl, "update_release", lambda *a, **k: pytest.fail("must not update"))
    assert ctl.cmd_update(layout, _update_args(layout, "--request")) == 1
    status = json.loads((layout.update_dir / "status.json").read_text())
    assert status["state"] == "failed" and "turned off" in status["message"]
    assert not (layout.update_dir / "request.json").exists()


def test_handover_forwards_only_install_flags_and_uses_the_unpacked_package(tmp_path, monkeypatch):
    layout = ctl.Layout(tmp_path, test=True)
    release = _release(tmp_path / "release")
    args = _update_args(
        layout,
        "--release",
        "0.5.0",
        "--interrupt-running",
        "--ready-timeout",
        "23",
        "--download-proxy",
        "--uv",
        "/tools/uv",
    )
    commands = []
    monkeypatch.setattr(ctl.subprocess, "call", lambda command: commands.append(command) or 0)
    assert ctl.hand_over(layout, release, args).returncode == 0
    assert commands == [
        [
            sys.executable,
            "-I",
            str(release.directory / "maestroctl"),
            "--test-root",
            str(tmp_path),
            "update",
            str(release.directory),
            "--ready-timeout",
            "23.0",
            "--interrupt-running",
            "--download-proxy",
            "service",
            "--uv",
            "/tools/uv",
        ]
    ]


def test_handover_streams_child_output_and_remembers_rollback_beyond_tail(tmp_path, capsys):
    layout = ctl.Layout(tmp_path, test=True)
    release = _release(tmp_path / "release")
    (release.directory / "maestroctl").write_text(
        "import sys\n"
        "print('!! update failed: migration', flush=True)\n"
        "print('rolled back: old is running', flush=True)\n"
        "for i in range(120): print('progress', i, flush=True)\n"
        "print('maestroctl: final error', file=sys.stderr, flush=True)\n"
        "sys.exit(1)\n"
    )
    result = ctl.hand_over(layout, release, _update_args(layout), capture=True)
    assert result.returncode == 1 and result.rolled_back
    assert result.message == "maestroctl: final error"
    output = capsys.readouterr().out
    assert "progress 119" in output and "rolled back:" in output and "final error" in output


@pytest.mark.parametrize(
    "args",
    [
        ["--request", "--release", "0.5.0"],
        ["package", "--release", "0.5.0"],
        ["package", "--request"],
    ],
)
def test_update_sources_are_mutually_exclusive(args):
    with pytest.raises(SystemExit):
        ctl.parser().parse_args(["update", *args])
    assert (
        "--request"
        not in ctl.parser()._subparsers._group_actions[0].choices["update"].format_help()
    )


def test_downloader_limits_each_download(github_release, tmp_path):
    opener = urllib.request.build_opener(ctl.ServiceProxyHandler({}))
    with pytest.raises(ctl.CtlError, match="exceeds 8 bytes"):
        ctl.release_download(
            opener, github_release["url"] + "/package", tmp_path / "partial", 8, "test"
        )


def test_downloads_use_the_service_proxy_except_for_its_bypass_list():
    proxy = "http://proxy.example:3128"
    handler = ctl.ServiceProxyHandler({"http": proxy, "https": proxy, "no": "127.0.0.1"})
    proxied = urllib.request.Request("http://example.com/path")
    handler.proxy_open(proxied, proxy, "http")
    assert proxied.host == "proxy.example:3128" and proxied.selector == "http://example.com/path"
    local = urllib.request.Request("http://127.0.0.1:8123/package")
    handler.proxy_open(local, proxy, "http")
    assert local.host == "127.0.0.1:8123"


@pytest.mark.parametrize(
    "text",
    ['update_repository = "../x/y"', 'update_api_url = "file:///tmp"', 'update_check = "yes"'],
)
def test_update_source_settings_are_validated(tmp_path, text):
    layout = _installed(tmp_path)
    layout.config.write_text(text)
    with pytest.raises(ctl.CtlError):
        ctl.update_settings(layout)


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("watching", [True, False])
def test_status_reports_the_watcher_and_last_web_result(
    tmp_path, monkeypatch, capsys, enabled, watching
):
    layout = _installed(tmp_path)
    layout.config.write_text(f"update_check = {str(enabled).lower()}\n")
    ctl.write_update_status(
        ctl.Host(layout),
        {
            "version": "0.5.0",
            "state": "failed",
            "finished_at": "2026-11-07T10:00:00Z",
            "message": "digest mismatch",
        },
    )

    def systemctl(host, *args, **kwargs):
        state = "active" if args[1] == layout.unit_name or watching else "inactive"
        return subprocess.CompletedProcess(args, 0 if state == "active" else 3, state)

    monkeypatch.setattr(ctl.Host, "systemctl", systemctl)
    monkeypatch.setattr(
        ctl.Host,
        "maestro",
        lambda *a, **k: subprocess.CompletedProcess([], 0, '{"active_runs": 0, "queued_runs": 2}'),
    )
    monkeypatch.setattr(ctl, "probe", lambda _: None)
    monkeypatch.setattr(ctl, "registry_ready", lambda _: True)
    assert ctl.cmd_status(layout, _args()) == 0
    output = capsys.readouterr().out
    assert (
        "update:   watching (au12321ua/maestro)"
        if enabled and watching
        else "update:   not watching"
    ) in output
    assert "last web update: 0.5.0 failed at 2026-11-07T10:00:00Z: digest mismatch" in output


@pytest.mark.parametrize("watching", [True, False])
def test_installed_check_reports_the_update_path_unit(tmp_path, monkeypatch, watching):
    layout = _installed(tmp_path)
    calls = []

    def systemctl(host, *args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 0 if watching else 3, "active" if watching else "inactive"
        )

    def no_uv(*args):
        raise ctl.CtlError("no uv in fixture")

    monkeypatch.setattr(ctl.Host, "systemctl", systemctl)
    monkeypatch.setattr(ctl, "find_uv", no_uv)
    monkeypatch.setattr(ctl, "_docker", lambda *a, **k: None)
    monkeypatch.setattr(ctl, "registry_ready", lambda _: True)
    found = {c.name: c for c in ctl.run_checks(layout, _args(), installing=False)}
    check = found["web updates"]
    assert check.status == ("ok" if watching else "warn")
    assert calls == [("is-active", layout.update_path_name)]
    if not watching:
        assert "maestroctl update" in check.fix
        assert f"systemctl enable --now {layout.update_path_name}" in check.fix


def test_uninstall_removes_all_units_and_purge_lists_them_before_confirmation(
    tmp_path, monkeypatch, capsys
):
    layout = _installed(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "units"))
    calls = []
    monkeypatch.setattr(ctl.Host, "systemctl", lambda self, *a, **k: calls.append(a))
    monkeypatch.setattr(ctl, "instance_id", lambda _: None)
    monkeypatch.setattr(ctl, "remove_registry", lambda _: None)
    ctl.write_unit(ctl.Host(layout))
    args = argparse.Namespace(purge=True, confirm=None, remove_cache=False)
    assert ctl.cmd_uninstall(layout, args) == 1
    output = capsys.readouterr().out
    assert all(str(p) in output and p.exists() for p in layout.unit_paths)
    calls.clear()
    args.purge = False
    assert ctl.cmd_uninstall(layout, args) == 0
    assert not any(p.exists() for p in layout.unit_paths)
    for name in (layout.unit_name, layout.update_path_name, layout.update_service_name):
        assert ("disable", "--now", name) in calls and ("reset-failed", name) in calls
    assert calls.count(("daemon-reload",)) == 1
    assert layout.data.exists()


@pytest.mark.parametrize("existing_updates", [True, False])
def test_update_rollback_restores_all_previous_unit_contents(
    tmp_path, monkeypatch, existing_updates
):
    layout = _installed(tmp_path)
    old = ctl.current_version(layout)
    release = _release(tmp_path / "new", "0.5.0-def")
    new = layout.versions / release.build
    new.mkdir()
    layout.ctl_link.parent.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "units"))
    layout.unit_path.parent.mkdir(parents=True)
    layout.unit_path.write_text("old service\n")
    if existing_updates:
        layout.update_path.write_text("old path\n")
        layout.update_service_path.write_text("old updater\n")
    previous = {p: p.read_text() if p.exists() else None for p in layout.unit_paths}
    calls = []
    restored = []
    monkeypatch.setattr(ctl, "own_release", lambda: release)
    monkeypatch.setattr(ctl, "find_uv", lambda *a: Path("/tools/uv"))
    monkeypatch.setattr(ctl, "keep_uv", lambda *a: Path("/tools/uv"))
    monkeypatch.setattr(ctl, "install_version", lambda *a: new)
    monkeypatch.setattr(ctl.Host, "systemctl", lambda self, *a, **k: calls.append(a))
    monkeypatch.setattr(
        ctl.Host, "maestro", lambda *a, **k: subprocess.CompletedProcess([], 0, '{"busy": false}')
    )
    monkeypatch.setattr(ctl, "make_backup", lambda *a, **k: tmp_path / "backup")
    monkeypatch.setattr(ctl, "start_registry", lambda *a: None)
    monkeypatch.setattr(ctl, "restore_database", lambda *a: restored.append(a[1:]))
    monkeypatch.setattr(ctl, "wait_ready", lambda *a: {})
    monkeypatch.setenv("MAESTRO_CTL_FAIL_AT", "start")
    assert ctl.cmd_update(layout, _update_args(layout)) == 1
    assert ctl.current_version(layout) == old
    assert {p: p.read_text() if p.exists() else None for p in layout.unit_paths} == previous
    assert restored == [(old, tmp_path / "backup")]
    if not existing_updates:
        assert ("disable", "--now", layout.update_path_name) in calls


def test_update_at_current_build_repairs_watcher_and_update_directory(tmp_path, monkeypatch):
    layout = _installed(tmp_path)
    layout.update_dir.rmdir()
    release = ctl.Release.load(ctl.current_version(layout))
    monkeypatch.setattr(ctl, "own_release", lambda: release)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "units"))
    calls = []
    monkeypatch.setattr(ctl.Host, "systemctl", lambda self, *a, **k: calls.append(a))
    assert ctl.cmd_update(layout, _update_args(layout)) == 0
    assert layout.update_dir.stat().st_mode & 0o777 == 0o700
    assert all(p.exists() for p in layout.unit_paths)
    assert calls[-1] == ("enable", "--now", layout.update_path_name)
