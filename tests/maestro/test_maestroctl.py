"""maestroctl's own rules, without installing anything (the full install, update and
uninstall run in test_install.py, opt-in)."""

import argparse
import importlib.util
import json
import os
import re
import tomllib
from pathlib import Path

import pytest

from maestro import config

ROOT = Path(__file__).resolve().parents[2]


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
