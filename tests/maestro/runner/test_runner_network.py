"""Compose services must either use the egress sidecar or have no network at all, and a
model run's agent phase always reaches the model whatever the task author allowed."""

import json
import subprocess

import pytest
from support import ROOT

RUNNER = ROOT / "runner/.venv/bin/python"
PROBE = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from maestro_runner.network import unrouted_services
print(json.dumps(unrouted_services(Path(sys.argv[2]))))
"""

# Tightens one private task copy per author setting, then asks Harbor what it would apply
# in each phase, so the check covers the rewritten file as Harbor reads it.
TIGHTEN = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from maestro_runner import network
from harbor.models.task.config import TaskConfig
from harbor.models.task.verifier_mode import VerifierEnvironmentMode
from harbor.models.trial.config import AgentConfig, EnvironmentConfig
from harbor.trial.network_policy import resolve_trial_network_plan

platform = json.loads(sys.argv[2])
found = {}
for name, text in json.loads(sys.argv[3]).items():
    task = Path(sys.argv[4]) / name
    task.mkdir()
    (task / "task.toml").write_text(text)
    plan = network.tighten(task, platform)
    config = TaskConfig.model_validate_toml((task / "task.toml").read_text())
    applied = resolve_trial_network_plan(
        config, AgentConfig(name="oracle"), EnvironmentConfig(), None,
        verifier_mode=VerifierEnvironmentMode.SHARED,
    )
    found[name] = {
        "agent": applied.agent_phase.model_dump(mode="json"),
        "verifier": applied.verifier_phase.model_dump(mode="json"),
        "recorded": plan["agent"]["effective"],
    }
print(json.dumps(found))
"""
MODEL_HOST = "gateway.example.net"
AUTHORS = {
    "public": 'version = "1.0"\n',
    "allowlist_without_the_model": (
        'version = "1.0"\n[agent]\nnetwork_mode = "allowlist"\n'
        'allowed_hosts = ["pypi.org", "files.pythonhosted.org"]\n'
    ),
    "allowlist_with_the_model": (
        'version = "1.0"\n[agent]\nnetwork_mode = "allowlist"\n'
        f'allowed_hosts = ["{MODEL_HOST}", "pypi.org"]\n'
    ),
    "no_network": 'version = "1.0"\n[agent]\nnetwork_mode = "no-network"\n',
}


def _tighten(tmp_path, platform):
    if not RUNNER.is_file():
        pytest.skip("the Runner environment is not installed (uv sync in runner/)")
    result = subprocess.run(
        [
            str(RUNNER),
            "-I",
            "-c",
            TIGHTEN,
            str(ROOT / "runner/src"),
            json.dumps(platform),
            json.dumps(AUTHORS),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_the_model_host_is_reachable_whatever_the_author_allowed(tmp_path):
    """An allowlist without the model's host, or no network, would leave a harness inside
    the container with nothing to call; other hosts the author allows are not passed on."""
    found = _tighten(
        tmp_path,
        {
            "agent": {"mode": "allowlist", "hosts": [MODEL_HOST]},
            "verifier": {"mode": "no-network"},
        },
    )
    agent = {"network_mode": "allowlist", "allowed_hosts": [MODEL_HOST]}
    assert found == {
        name: {
            "agent": agent,
            "verifier": {"network_mode": "no-network", "allowed_hosts": []},
            "recorded": agent,
        }
        for name in AUTHORS
    }


def test_a_runner_side_model_call_leaves_the_agent_phase_without_network(tmp_path):
    found = _tighten(
        tmp_path,
        {"agent": {"mode": "no-network"}, "verifier": {"mode": "no-network"}},
    )
    closed = {"network_mode": "no-network", "allowed_hosts": []}
    assert found == {
        name: {"agent": closed, "verifier": closed, "recorded": closed} for name in AUTHORS
    }


@pytest.mark.parametrize("directory", ["environment", "tests"])
def test_only_explicitly_disconnected_services_may_set_their_own_network(tmp_path, directory):
    if not RUNNER.is_file():
        pytest.skip("the Runner environment is not installed (uv sync in runner/)")
    services = {
        "default": {},
        "replay": {"network_mode": "none"},
        "host": {"network_mode": "host"},
        "bridge": {"network_mode": "bridge"},
        "container": {"network_mode": "container:replay"},
        "service": {"network_mode": "service:replay"},
        "custom": {"networks": ["custom"]},
        "internal": {"networks": {"internal": {}}},
        "empty_networks": {"networks": []},
        "null_networks": {"networks": None},
        "none_with_networks": {"network_mode": "none", "networks": ["custom"]},
        "none_with_empty_networks": {"network_mode": "none", "networks": []},
        "none_with_null_networks": {"network_mode": "none", "networks": None},
        "null_mode": {"network_mode": None},
        "empty_mode": {"network_mode": ""},
        "uppercase_mode": {"network_mode": "NONE"},
        "spaced_mode": {"network_mode": " none "},
        "invalid_mode": {"network_mode": False},
    }
    compose = tmp_path / directory / "docker-compose.yaml"
    compose.parent.mkdir()
    compose.write_text(
        json.dumps({"services": services, "networks": {"internal": {"internal": True}}})
    )
    original = compose.read_bytes()
    result = subprocess.run(
        [str(RUNNER), "-I", "-c", PROBE, str(ROOT / "runner/src"), str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    prefix = "" if directory == "environment" else "tests/"
    assert json.loads(result.stdout) == sorted(
        f"{prefix}{name}" for name in services if name not in {"default", "replay"}
    )
    assert compose.read_bytes() == original


_NETWORK_PROBE = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from maestro_runner import network
from harbor.models.task.config import TaskConfig
from harbor.models.trial.config import AgentConfig, EnvironmentConfig
from harbor.models.task.verifier_mode import VerifierEnvironmentMode
from harbor.trial.network_policy import resolve_trial_network_plan
task = Path(sys.argv[2])
network.tighten(task, {"agent": {"mode": "no-network"}, "verifier": {"mode": "no-network"}})
config = TaskConfig.model_validate_toml((task / "task.toml").read_text())
modes = []
for step in [None, *config.steps]:
    plan = resolve_trial_network_plan(
        config, AgentConfig(name="oracle"), EnvironmentConfig(), step,
        verifier_mode=VerifierEnvironmentMode.SHARED,
    )
    modes += [plan.agent_phase.network_mode.value, plan.verifier_phase.network_mode.value]
print(json.dumps({"modes": modes, "unrouted": network.unrouted_services(task)}))
"""


def test_tightening_reaches_steps_and_finds_services_with_their_own_networks(tmp_path):
    root = ROOT / "runner"
    python = root / ".venv" / "bin" / "python"
    if not python.exists():
        pytest.skip("the Runner environment is not installed (uv sync in runner/)")
    task = tmp_path / "task"
    (task / "environment").mkdir(parents=True)
    (task / "task.toml").write_text(
        'version = "1.0"\n[environment]\nnetwork_mode = "public"\n'
        '[[steps]]\nname = "one"\n[steps.agent]\nnetwork_mode = "public"\n'
        '[steps.verifier]\nnetwork_mode = "public"\n'
    )
    (task / "environment" / "docker-compose.yaml").write_text(
        "services:\n  main:\n    build: .\n    networks: [default]\n"
    )
    found = subprocess.run(
        [str(python), "-I", "-c", _NETWORK_PROBE, str(root / "src"), str(task)],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    result = json.loads(found.stdout.strip().splitlines()[-1])
    assert set(result["modes"]) == {"no-network"}
    assert result["unrouted"] == ["main"]
