"""Every image a trial starts (N-01): inspection of a separate verifier and Compose
services, frozen variables, what cannot be prepared, the resources a trial reserves, and
the Runner's per-directory overrides. Compose files are read by the Docker client
(nothing is started); the Runner parts run in the Runner's own environment."""

import json
import runpy
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from maestro.domain import limits
from maestro.execution import compose
from maestro.execution.materials import image_key, inspect_task, tree_digest
from maestro.services import policy
from maestro.services.configuration import Resolver

DIGEST = "sha256:" + "ab" * 32
RUNNER = Path(__file__).resolve().parents[2] / "runner"
RUNNER_PYTHON = RUNNER / ".venv" / "bin" / "python"


def compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    found = subprocess.run(["docker", "compose", "version"], capture_output=True, check=False)
    return found.returncode == 0


needs_compose = pytest.mark.skipif(
    not compose_available(), reason="needs the docker client with Compose"
)


def make_task(root: Path, toml: str = "", files: dict | None = None) -> Path:
    files = {"environment/Dockerfile": "FROM ubuntu:22.04\n", **(files or {})}
    root.mkdir(parents=True)
    (root / "task.toml").write_text(textwrap.dedent(toml))
    (root / "tests").mkdir()
    (root / "tests" / "test.sh").write_text("#!/bin/sh\n")
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
    return root


def inspect(task: Path) -> dict:
    return inspect_task(task, DIGEST, tree_digest(task))


def roles(definition: dict) -> dict:
    return {i["role"]: i for i in definition["preparation"]["spec"]["images"]}


COMPOSE = """\
services:
  main:
    environment:
      GREETING: ${GREETING:-hi}
    depends_on: [db, api]
  db:
    image: postgres:${PG_VERSION:-16}
    cpus: 0.5
    mem_limit: 512m
  api:
    build:
      context: ./api
      dockerfile: Dockerfile.api
      args:
        RELEASE: ${API_RELEASE:-1.0}
      labels: {team: x}
volumes:
  data: {}
"""


def test_a_task_without_compose_or_verifier_has_one_image(tmp_path):
    definition = inspect(make_task(tmp_path / "t"))
    (main,) = definition["preparation"]["spec"]["images"]
    assert main["role"] == "environment/main" and main["source"] == "build"
    assert (main["context"], main["dockerfile"]) == ("environment", "Dockerfile")
    assert definition["resources"]["environments"] == []
    assert definition["unsupported"] == []


def test_a_named_image_is_pulled(tmp_path):
    task = make_task(tmp_path / "t", '[environment]\ndocker_image = "acme/x:1"\n')
    assert roles(inspect(task))["environment/main"] == {
        "role": "environment/main",
        "source": "pull",
        "reference": "acme/x:1",
    }


@needs_compose
def test_compose_services_are_prepared_with_their_variables_frozen(tmp_path):
    task = make_task(
        tmp_path / "t",
        files={
            "environment/docker-compose.yaml": COMPOSE,
            "environment/api/Dockerfile.api": "FROM busybox\n",
        },
    )
    definition = inspect(task)
    assert definition["unsupported"] == [], definition["unsupported_reason"]
    images = roles(definition)
    assert images["environment/db"] == {
        "role": "environment/db",
        "source": "pull",
        "reference": "postgres:16",
    }
    api = images["environment/api"]
    assert (api["source"], api["context"], api["dockerfile"]) == (
        "build",
        "environment/api",
        "Dockerfile.api",
    )
    assert api["args"] == {"RELEASE": "1.0"}
    assert api["definition_digest"] == tree_digest(task / "environment" / "api")
    # Frozen into an image: no longer a parameter. Used at run time: still one.
    names = {p["name"] for p in definition["parameters"]}
    assert "GREETING" in names and not names & {"PG_VERSION", "API_RELEASE"}
    assert definition["resources"]["environments"] == [
        {"role": "environment/api", "cpus": None, "memory_mb": None},
        {"role": "environment/db", "cpus": 0.5, "memory_mb": 512},
    ]


@needs_compose
@pytest.mark.parametrize(
    ("compose", "reason"),
    [
        ("services:\n  db:\n    image: postgres:${PG}\n", "PG needs a default"),
        ("services:\n  db:\n    image: x:${API_TOKEN:-1}\n", "API_TOKEN looks like a secret"),
        (
            "services:\n  db:\n    build:\n      context: .\n      ssh: [default]\n",
            "build option ssh is unsupported",
        ),
        ("services:\n  db:\n    build: ../tests\n", "build context must be inside"),
        ("services:\n  main:\n    image: other:1\n", "main's image comes from the task"),
        ("services:\n  db:\n    command: sleep 1\n", "neither image nor build"),
        (
            "services:\n  db:\n    image: x:1\nnetworks:\n  outside:\n    external: true\n",
            "external or named resources",
        ),
    ],
)
def test_what_cannot_be_prepared_is_named_without_values(tmp_path, compose, reason):
    task = make_task(tmp_path / "t", files={"environment/docker-compose.yaml": compose})
    definition = inspect(task)
    assert definition["unsupported"], compose
    assert reason in definition["unsupported_reason"]


# Both boundaries apply this policy: preparation alone cannot protect older admissions.
RUNTIME_CHECKS = [
    compose.check_runtime,
    runpy.run_path(str(RUNNER / "src/maestro_runner/compose.py"))["check_runtime"],
]
UNSUPPORTED_RUNTIME = [
    {"volumes": ["/host/canary:/canary:ro"]},
    {"volumes": ["./canary:/canary"]},
    {"volumes": ["${SOURCE:-data}:/canary"]},
    {"volumes": [{"type": "bind", "source": "/host/canary", "target": "/canary"}]},
    {"volumes_from": ["container:outside:ro"]},
    {"configs": ["host_file"]},
    {"secrets": ["host_file"]},
    {"devices": ["/dev/sda:/dev/sda"]},
    {"use_api_socket": True},
    {"privileged": True},
    {"scale": 3},
    {"scale": "${REPLICAS:-1}"},
    {"deploy": {"replicas": 3}},
    {"deploy": {"replicas": "${REPLICAS:-1}"}},
    {"deploy": {"mode": "global"}},
]


@pytest.mark.parametrize("check", RUNTIME_CHECKS, ids=["preparation", "runner"])
@pytest.mark.parametrize("service", UNSUPPORTED_RUNTIME)
@pytest.mark.parametrize("name", ["main", "helper"])
def test_host_access_and_unaccounted_replicas_are_refused(check, service, name):
    with pytest.raises(ValueError) as error:
        check({"services": {name: service}, "volumes": {"data": {}}}, "tests/compose")
    assert f"services.{name}" in str(error.value)
    assert "/host/canary" not in str(error.value)


@pytest.mark.parametrize("check", RUNTIME_CHECKS, ids=["preparation", "runner"])
@pytest.mark.parametrize(
    "volume",
    [
        {"driver_opts": {"type": "none", "o": "bind", "device": "/host"}},
        {"driver": "host-driver"},
        {"external": True},
        {"name": "outside"},
    ],
)
def test_volume_definitions_cannot_reach_the_host(check, volume):
    with pytest.raises(ValueError):
        check({"volumes": {"data": volume}}, "environment/compose")


@pytest.mark.parametrize("check", RUNTIME_CHECKS, ids=["preparation", "runner"])
def test_internal_volumes_and_a_fixed_single_replica_remain_supported(check):
    check(
        {
            "services": {
                "main": {
                    "volumes": ["data:/data", "/scratch", {"type": "tmpfs", "target": "/ram"}],
                    "scale": 1,
                    "deploy": {"replicas": 1, "mode": "replicated"},
                }
            },
            "volumes": {"data": {"driver": "local"}},
        },
        "environment/compose",
    )


@needs_compose
@pytest.mark.parametrize("directory", ["environment", "tests"])
@pytest.mark.parametrize("service", [UNSUPPORTED_RUNTIME[0], UNSUPPORTED_RUNTIME[12]])
def test_inspection_checks_both_environment_directories(tmp_path, directory, service):
    task = make_task(
        tmp_path / "t",
        '[verifier]\nenvironment_mode = "separate"\n',
        files={
            "tests/Dockerfile": "FROM ubuntu:22.04\n",
            f"{directory}/docker-compose.yaml": json.dumps({"services": {"main": service}}),
        },
    )
    definition = inspect(task)
    assert definition["unsupported"]
    assert f"{directory}/docker-compose.yaml services.main" in definition["unsupported_reason"]


def test_the_verifier_s_own_image_is_pulled(tmp_path):
    task = make_task(
        tmp_path / "t",
        """\
        [verifier.environment]
        docker_image = "grader:2"
        cpus = 2
        memory_mb = 4096
        """,
    )
    definition = inspect(task)
    assert roles(definition)["tests/main"] == {
        "role": "tests/main",
        "source": "pull",
        "reference": "grader:2",
    }
    assert definition["resources"]["environments"] == [
        {"role": "verifier", "cpus": 2, "memory_mb": 4096}
    ]


@needs_compose
def test_a_verifier_built_from_tests_brings_its_services(tmp_path):
    task = make_task(
        tmp_path / "t",
        '[verifier]\nenvironment_mode = "separate"\n',
        files={
            "tests/Dockerfile": "FROM python:3.12\n",
            "tests/docker-compose.yaml": "services:\n  cache:\n    image: redis:7\n",
        },
    )
    definition = inspect(task)
    images = roles(definition)
    assert images["tests/main"]["context"] == "tests"
    assert images["tests/cache"]["reference"] == "redis:7"
    environments = definition["resources"]["environments"]
    assert [e["role"] for e in environments] == ["tests/cache", "verifier"]


def test_a_verifier_copying_the_main_environment_needs_no_image(tmp_path):
    task = make_task(
        tmp_path / "t",
        '[verifier]\nenvironment_mode = "separate"\n[environment]\ncpus = 3\nmemory_mb = 3000\n',
    )
    definition = inspect(task)
    assert list(roles(definition)) == ["environment/main"]
    assert definition["resources"]["environments"] == [
        {"role": "verifier", "cpus": 3, "memory_mb": 3000}
    ]


def test_separate_verifiers_of_multi_step_tasks_are_unsupported(tmp_path):
    task = make_task(
        tmp_path / "t",
        '[[steps]]\nname = "one"\n[steps.verifier]\nenvironment_mode = "separate"\n',
    )
    assert inspect(task)["unsupported"] == ["separate_verifier_steps"]


def test_identical_definitions_share_a_key_wherever_they_sit(tmp_path):
    """Two tasks with the same environment files build one image (N-01)."""
    one = make_task(tmp_path / "one", files={"tests/test.sh": "echo one\n"})
    two = make_task(tmp_path / "two", files={"tests/test.sh": "echo two\n"})
    first = roles(inspect(one))["environment/main"]
    second = roles(inspect(two))["environment/main"]
    assert image_key(first) == image_key(second)
    (two / "environment" / "Dockerfile").write_text("FROM ubuntu:24.04\n")
    assert image_key(roles(inspect(two))["environment/main"]) != image_key(first)


def test_a_trial_reserves_the_sum_and_the_team_fills_in_missing_limits():
    definition = {
        "resources": {
            "cpus": 2,
            "memory_mb": 2048,
            "environments": [
                {"role": "verifier", "cpus": 2, "memory_mb": 2048},
                {"role": "environment/db", "cpus": 0.5, "memory_mb": None},
            ],
        }
    }
    team = policy.BUILT_IN
    assert (
        (team.service_cpus_default, team.service_memory_mb_default)
        == (
            limits.SERVICE_CPUS_DEFAULT,
            limits.SERVICE_MEMORY_MB_DEFAULT,
        )
        == (1, 1024)
    )
    overrides = dict.fromkeys(("cpus", "memory_mb", "agent_timeout_sec", "verifier_timeout_sec"))
    settings = Resolver._task_settings(definition, overrides, team)
    assert (settings["cpus"], settings["memory_mb"]) == (4.5, 2048 + 2048 + 1024)
    db = settings["environments"][2]
    assert db == {"role": "environment/db", "cpus": 0.5, "memory_mb": 1024, "default": True}
    raised = Resolver._task_settings(definition, {**overrides, "cpus": 8}, team)
    assert raised["cpus"] == 8 and raised["environments"] == settings["environments"]


# --- the Runner's side ------------------------------------------------------------------

_PROBE = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from maestro_runner import job
from harbor.environments.factory import EnvironmentFactory
from harbor.models.task.config import TaskConfig
from harbor.models.task.paths import TaskPaths
from harbor.models.task.verifier_mode import resolve_verifier_environment_definition

task = Path(sys.argv[2]); out = Path(sys.argv[3]); spec = json.loads(sys.argv[4])
job._with_images(task, spec["task"], spec["overrides"])
config = TaskConfig.model_validate_toml((task / "task.toml").read_text())
definition = resolve_verifier_environment_definition(config, TaskPaths(task))
pins = job._pin_overrides(task, spec["task"], {"io.maestro.run_id": "run_1"}, out / "pins")
seen = []
def original(cls, config, *args, **kwargs):
    seen.append([str(p) for p in config.extra_docker_compose])
EnvironmentFactory.create_environment_from_config = classmethod(original)
job._environment_overrides(out, {"io.maestro.run_id": "run_1"}, pins)
from harbor.models.trial.config import EnvironmentConfig
agent = EnvironmentConfig(extra_docker_compose=[str(out / "labels.yaml")])
EnvironmentFactory.create_environment_from_config(agent, environment_dir=task / "environment")
EnvironmentFactory.create_environment_from_config(EnvironmentConfig(), environment_dir=task / "tests")
# Importing the Runner sends stdout to stderr (its protocol stream is private).
(out / "result.json").write_text(json.dumps({
    "main": config.environment.docker_image,
    "cpus": config.environment.cpus,
    "memory_mb": config.environment.memory_mb,
    "verifier": config.verifier.environment.docker_image if config.verifier.environment else None,
    "verifier_cpus": definition.config.cpus if definition else None,
    "verifier_memory_mb": definition.config.memory_mb if definition else None,
    "verifier_directory": definition.directory.name if definition else None,
    "bundled_tests": definition.bundled_tests if definition else None,
    "files": {k: Path(v).read_text() for k, v in pins.items()},
    "seen": seen,
}))
"""

needs_runner = pytest.mark.skipif(
    not RUNNER_PYTHON.exists(), reason="the Runner environment is not installed (uv sync)"
)


def probe(tmp_path, task: Path, spec: dict) -> dict:
    out = tmp_path / "out"
    out.mkdir()
    found = subprocess.run(
        [str(RUNNER_PYTHON), "-I", "-c", _PROBE, str(RUNNER / "src"), str(task), str(out)]
        + [json.dumps(spec)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert found.returncode == 0, found.stderr
    return json.loads((out / "result.json").read_text())


@needs_runner
@needs_compose
def test_the_runner_pins_every_image_per_directory(tmp_path):
    task = make_task(
        tmp_path / "t",
        '[verifier]\nenvironment_mode = "separate"\n',
        files={
            "environment/docker-compose.yaml": COMPOSE,
            "tests/Dockerfile": "FROM python:3.12\n",
        },
    )
    images = {
        "environment/main": "reg/p@sha256:" + "01" * 32,
        "environment/db": "reg/p@sha256:" + "02" * 32,
        "environment/api": "reg/p@sha256:" + "03" * 32,
        "tests/main": "reg/p@sha256:" + "04" * 32,
    }
    environments = [
        {"role": "environment/main", "cpus": 1, "memory_mb": 2048, "default": False},
        {"role": "environment/api", "cpus": 1, "memory_mb": 1024, "default": True},
        {"role": "environment/db", "cpus": 0.5, "memory_mb": 512, "default": False},
        {"role": "verifier", "cpus": 1, "memory_mb": 2048, "default": False},
    ]
    task_spec = {"images": images, "environments": environments}
    result = probe(tmp_path, task, {"task": task_spec, "overrides": {"cpus": 6}})
    assert result["main"] == images["environment/main"]
    assert result["verifier"] == images["tests/main"]
    assert result["cpus"] == 3  # 6 asked, 3.5 reserved by the author: 2 more for main
    environment = result["files"][str((task / "environment").resolve())]
    assert (
        f'"api":\n    image: "{images["environment/api"]}"\n    pull_policy: never' in environment
    )
    assert "build: !reset null" in environment  # api is built by the author's file
    assert 'cpus: 1\n    mem_limit: "1024m"' in environment  # the team default, api only
    assert environment.count("cpus:") == 1
    assert '"data":\n    labels:' in environment and '"default":\n    labels:' in environment
    tests = result["files"][str((task / "tests").resolve())]
    assert f'"main":\n    image: "{images["tests/main"]}"' in tests
    # The agent's environment keeps the job's overlay and gets its directory's pins; the
    # separate verifier, which Harbor gives no overlay, gets the platform labels and pins.
    agent, verifier = result["seen"]
    assert agent[0].endswith("labels.yaml") and agent[1].endswith("environment.yaml")
    assert verifier[0].endswith("verifier-labels-0.yaml") and verifier[1].endswith("tests.yaml")


@needs_runner
@pytest.mark.parametrize("own_resources", [False, True])
@pytest.mark.parametrize("own_image", [False, True])
def test_resource_overrides_reach_main_and_preserve_the_verifier(
    tmp_path, own_resources, own_image
):
    task = make_task(
        tmp_path / "t",
        "[environment]\ncpus = 1\nmemory_mb = 2048\n"
        '[verifier]\nenvironment_mode = "separate"\n'
        + ("[verifier.environment]\ncpus = 2\nmemory_mb = 3000\n" if own_resources else ""),
    )
    verifier_cpu, verifier_memory = (2, 3000) if own_resources else (1, 2048)
    images = {"environment/main": "reg/main@" + DIGEST}
    if own_image:
        images["tests/main"] = "reg/verifier@" + DIGEST
    found = probe(
        tmp_path,
        task,
        {
            "task": {
                "images": images,
                "environments": [
                    {"role": "environment/main", "cpus": 1, "memory_mb": 2048},
                    {"role": "verifier", "cpus": verifier_cpu, "memory_mb": verifier_memory},
                ],
            },
            # An odd increment must not disappear when the verifier inherits resources.
            "overrides": {"cpus": 8, "memory_mb": 8193},
        },
    )
    assert found["cpus"] + found["verifier_cpus"] == 8
    assert found["memory_mb"] + found["verifier_memory_mb"] == 8193
    assert (found["verifier_cpus"], found["verifier_memory_mb"]) == (verifier_cpu, verifier_memory)
    assert found["verifier_directory"] == ("tests" if own_image else "environment")
    assert found["bundled_tests"] == own_image


@needs_runner
def test_harbor_still_creates_environments_where_the_runner_hooks_in():
    """The verifier override rides on Harbor internals (15): fail loudly when they move."""
    check = """
import inspect
from harbor.environments.factory import EnvironmentFactory
from harbor.trial import trial
signature = inspect.signature(EnvironmentFactory.create_environment_from_config)
assert "environment_dir" in signature.parameters, signature
source = inspect.getsource(trial)
assert source.count("EnvironmentFactory.create_environment_from_config(") == 2
assert 'update={"extra_docker_compose": []}' in source
print("ok")
"""
    found = subprocess.run(
        [str(RUNNER_PYTHON), "-I", "-c", check], capture_output=True, text=True, check=False
    )
    assert found.stdout.strip() == "ok", found.stderr
