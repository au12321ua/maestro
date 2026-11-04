"""Compose services must either use the egress sidecar or have no network at all."""

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "runner/.venv/bin/python"
PROBE = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from maestro_runner.network import unrouted_services
print(json.dumps(unrouted_services(Path(sys.argv[2]))))
"""


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
