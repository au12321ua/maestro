"""The setup network (N-08): kept on the admin page as revisions, read when setup work
starts. The proxy reaches the platform's image builds and setup in the task container
(harness install, attachment scripts), its variables setup in the container only; the
agent and the verifier get neither."""

import asyncio
import json
from dataclasses import replace

import pytest
from support import Api, create_pat, invite_member, running, setup_admin, submit, wait_operation
from test_configuration import agent_body, error
from test_e2e_run import prepare_model
from test_fix_prep_docker import make_spec, no_progress
from test_harbor_executor import (
    RecordingSink,
    agent_spec,
    attachment_spec,
    execute,
    make_executor,
)
from test_harbor_executor import (
    make_spec as run_spec,
)

from maestro.config import load
from maestro.core.errors import MaestroError
from maestro.domain import setup_network
from maestro.domain.setup_network import SetupNetwork
from maestro.execution.attachments import AttachmentStore
from maestro.execution.preparation import DockerEnvironment

PROXY = "http://172.17.0.1:7890"
MIRRORS = {
    "npm_config_registry": "https://registry.npmmirror.com",
    "NVM_NODEJS_ORG_MIRROR": "https://npmmirror.com/mirrors/node",
}
NETWORK = SetupNetwork(4, PROXY, "localhost,10.0.0.0/8", MIRRORS, 900)


def body(expected_revision=0, **value) -> dict:
    return {"expected_revision": expected_revision, **value}


def test_values_are_checked_and_split_between_builds_and_containers():
    assert setup_network.BUILT_IN.container_env() == {}
    checked = setup_network.check(
        {"proxy": PROXY, "no_proxy": "localhost,10.0.0.0/8", "env": MIRRORS}, revision=4
    )
    assert checked.revision == 4 and checked.harness_setup_timeout_sec is None
    proxy = {
        "HTTP_PROXY": PROXY,
        "HTTPS_PROXY": PROXY,
        "NO_PROXY": "localhost,10.0.0.0/8",
        "http_proxy": PROXY,
        "https_proxy": PROXY,
        "no_proxy": "localhost,10.0.0.0/8",
    }
    # Image builds get the proxy only; containers get the variables too.
    assert checked.proxy_env() == proxy
    assert checked.container_env() == proxy | MIRRORS
    bad = {
        "/proxy": [
            {"proxy": "socks5://172.17.0.1:7890"},
            {"proxy": "http://172.17.0.1"},
            {"proxy": "172.17.0.1:7890"},
            {"proxy": "http://user:pass@172.17.0.1:7890"},
        ],
        "/no_proxy": [{"no_proxy": "a, b"}],
        "/harness_setup_timeout_sec": [
            {"harness_setup_timeout_sec": 5},
            {"harness_setup_timeout_sec": True},
        ],
        "/env": [{"env": "npm_config_registry=x"}],
    }
    for name, value in (
        ("1BAD", "x"),
        ("A-B", "x"),
        ("PIP_INDEX_URL", "two\nlines"),
        ("PIP_INDEX_URL", 3),
        ("https_proxy", PROXY),
        ("ALL_PROXY", PROXY),
        ("MAESTRO_ATTACHMENT_DIR", "/tmp"),
        ("NODE_AUTH_TOKEN", "x"),
        ("npm_config__authToken", "x"),
        ("PIP_INDEX_URL", "https://user:pa55@pypi.example/simple"),
    ):
        bad[f"/env/{name}"] = [{"env": {name: value}}]
    for path, values in bad.items():
        for value in values:
            with pytest.raises(MaestroError) as raised:
                setup_network.check(value)
            assert [e.path for e in raised.value.field_errors] == [path], value
    loopback = setup_network.check({"proxy": "http://127.0.0.1:7890"})
    assert "127.0.0.1" in loopback.warnings()[0]


def test_old_config_keys_are_ignored_and_reported(tmp_path):
    config = tmp_path / "config.toml"
    base = (
        f'data_dir = "{tmp_path / "data"}"\ncache_dir = "{tmp_path / "cache"}"\n'
        f'master_key_dir = "{tmp_path / "k"}"\n'
    )
    config.write_text(base + f'setup_proxy = "{PROXY}"\nsetup_no_proxy = "localhost"\n')
    settings = load(config)
    assert settings.retired == ("setup_no_proxy", "setup_proxy")
    assert any("setup_proxy in config.toml is ignored" in note for note in settings.advice())
    config.write_text(base + "setup_env = {}\n")
    with pytest.raises(MaestroError, match="Unknown settings"):
        load(config)


def test_administrators_save_revisions_that_everyone_reads(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "member")
        pat = create_pat(member)
        first = member.get("/api/v1/setup-network", expect=200).json()["data"]
        assert first["revision"] == 0 and first["proxy"] == "" and first["env"] == {}
        change = {"proxy": PROXY, "env": MIRRORS, "harness_setup_timeout_sec": 900}
        assert member.put("/api/v1/admin/setup-network", json=body(**change)).status_code == 403
        assert pat.put("/api/v1/admin/setup-network", json=body(**change)).status_code == 403
        saved = admin.put(
            "/api/v1/admin/setup-network", json=body(**change, note="npmmirror"), expect=200
        ).json()["data"]
        assert saved["revision"] == 1 and saved["created_by_username"] == "admin"
        assert saved["env"] == MIRRORS and saved["note"] == "npmmirror"
        stale = admin.put("/api/v1/admin/setup-network", json=body(**change))
        assert error(stale) == (409, "SETUP_NETWORK_REVISION_CHANGED")
        invalid = admin.put(
            "/api/v1/admin/setup-network",
            json=body(1, env={"NODE_AUTH_TOKEN": "x"}, proxy="socks5://h:1"),
        )
        assert error(invalid) == (422, "SETUP_NETWORK_INVALID")
        assert {e["path"] for e in invalid.json()["error"]["field_errors"]} == {
            "/proxy",
            "/env/NODE_AUTH_TOKEN",
        }
        # Saving everything empty turns it off again: a whole new revision.
        admin.put("/api/v1/admin/setup-network", json=body(1), expect=200)
        history = pat.get("/api/v1/setup-network/revisions", expect=200).json()["data"]
        assert [h["revision"] for h in history] == [2, 1, 0]
        assert history[1]["proxy"] == PROXY and history[0]["proxy"] == ""
        assert service.setup_networks.current().revision == 2
        deployment = admin.get("/api/v1/admin/deployment", expect=200).json()["data"]
        assert deployment["values"]["listen_port"] == service.settings.listen_port
        assert deployment["retired"] == [] and "setup_proxy" not in deployment["values"]
        assert member.get("/api/v1/admin/deployment").status_code == 403


def test_runs_read_the_revision_in_force_when_they_execute(service, version_id, executor):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        model_id, harness_id = prepare_model(admin)
        pat = create_pat(admin, run_limit=5)
        model_run = submit(pat, version_id, **agent_body(model_id, harness_id)).json()["data"]
        oracle_run = submit(pat, version_id).json()["data"]
        # Saved after acceptance, before a worker picks the runs up.
        admin.put("/api/v1/admin/setup-network", json=body(proxy=PROXY, env=MIRRORS), expect=200)
    with running(service) as client:
        api = Api(client, token=pat.token)
        for run in (model_run, oracle_run):
            wait_operation(api, run["operation_id"])
        by_run = {spec.run_id: spec for spec in executor.specs}
        assert by_run[model_run["run_id"]].setup_network.revision == 1
        assert by_run[model_run["run_id"]].setup_network.env == MIRRORS

        def revisions(run_id):
            trials = api.get(f"/api/v1/runs/{run_id}/trials", expect=200).json()["data"]
            return {a["setup_network_revision"] for x in trials for a in x["attempts"]}

        assert revisions(model_run["run_id"]) == {1}
        # Oracle runs have no setup work: nothing was offered.
        assert revisions(oracle_run["run_id"]) == {None}


def test_preparations_name_the_revision_they_were_built_with(service, source):
    from conftest import import_version

    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        admin.put("/api/v1/admin/setup-network", json=body(proxy=PROXY), expect=200)
        source.add("demo", "other", "v1", ["sub-01"])
        result = import_version(service, name="other")
        assert result["preparation"]["status"] == "succeeded", result
        tasks = admin.get(
            f"/api/v1/dataset-versions/{result['dataset_version_id']}/tasks", expect=200
        ).json()["data"]
        prep = admin.get(
            f"/api/v1/preparations/{tasks[0]['local_preparation']['id']}", expect=200
        ).json()["data"]
        assert prep["setup_network_revision"] == 1


def test_image_builds_get_the_proxy_as_build_arguments_and_nothing_else(tmp_path):
    calls = []

    async def run(*args, timeout):
        calls.append(args)
        return 0, "", ""

    environment = DockerEnvironment(tmp_path / "archives")
    environment._run = run
    spec = replace(make_spec(tmp_path), setup_network=NETWORK)
    detail = asyncio.run(environment._build(spec, "t:tmp", no_progress))
    (args,) = calls
    assert f"HTTP_PROXY={PROXY}" in args and f"https_proxy={PROXY}" in args
    assert args[args.index(f"HTTP_PROXY={PROXY}") - 1] == "--build-arg"
    assert not any("npm_config_registry" in a for a in args)
    assert detail == {"method": "build", "proxy": True, "setup_network_revision": 4}
    calls.clear()
    detail = asyncio.run(environment._build(make_spec(tmp_path), "t:tmp", no_progress))
    assert not any("PROXY" in a.upper() for a in calls[0])
    assert detail["setup_network_revision"] == 0


def test_setup_in_the_container_gets_the_network_and_nothing_else_does(
    runner_python, materials, tmp_path
):
    secret = "http://user:pa55word@172.17.0.1:7890"
    network = replace(NETWORK, proxy=secret)  # check() refuses it now
    store = AttachmentStore(tmp_path / "data")
    executor = make_executor(runner_python, materials, tmp_path, attachments=store)

    def request_for(spec):
        execute(executor, replace(spec, setup_network=network), RecordingSink())
        return json.loads((tmp_path / "work" / "request.json").read_text())

    request = request_for(attachment_spec(materials, tmp_path, store))
    assert request["setup_env"] == network.container_env()
    assert request["harness_setup_timeout_sec"] == 900
    # Not in the agent's or the task's environment.
    assert "PROXY" not in json.dumps(request["agent"]).upper()
    assert "npmmirror" not in json.dumps(request["agent"])
    assert all("proxy" not in k.lower() for k in request["task_env"])
    # A model run's harness installs itself in setup too; Oracle has no setup work.
    assert request_for(agent_spec(materials, tmp_path))["setup_env"] == network.container_env()
    request = request_for(run_spec(materials, tmp_path, "ok", trials=1))
    assert request["agent"] == {"name": "oracle"} and request["setup_env"] == {}
    assert request["harness_setup_timeout_sec"] is None


CONFIG_DRIVER = """
import json, sys
sys.path.insert(0, sys.argv[1])
from maestro_runner import job
spec = json.load(open(sys.argv[2]))
spec["network"] = None  # leave the host's firewall alone
__import__("os").makedirs(spec["output_dir"], exist_ok=True)
# The Runner's protocol owns stdout; answer through a file.
limit = job.Execution(spec)._config().agents[0].override_setup_timeout_sec
open(sys.argv[3], "w").write(json.dumps(limit))
"""


def test_the_runner_gives_harbor_the_harness_setup_time_limit(runner_python, materials, tmp_path):
    import subprocess

    from test_runner_opencode import RUNNER, RUNNER_SRC

    if not RUNNER.exists():
        pytest.skip("Runner environment not installed")
    store = AttachmentStore(tmp_path / "data")
    executor = make_executor(runner_python, materials, tmp_path, attachments=store)

    def limit(spec, network):
        execute(executor, replace(spec, setup_network=network), RecordingSink())
        request = tmp_path / "request.json"
        (tmp_path / "work" / "request.json").rename(request)
        answer = tmp_path / "answer.json"
        subprocess.run(
            [str(RUNNER), "-c", CONFIG_DRIVER, str(RUNNER_SRC), str(request), str(answer)],
            capture_output=True,
            check=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        )
        return json.loads(answer.read_text())

    agent = agent_spec(materials, tmp_path)
    assert limit(agent, NETWORK) == 900
    # Unset: Harbor keeps the harness's own default.
    assert limit(agent, setup_network.BUILT_IN) is None
    with_attachments = attachment_spec(materials, tmp_path, store)
    bootstrap = with_attachments.effective_config["bootstrap_timeout_sec"]
    assert limit(with_attachments, NETWORK) == 900 + bootstrap + 120


@pytest.fixture
def runner_python(tmp_path):
    import stat
    import sys

    from test_harbor_executor import FAKE_RUNNER

    wrapper = tmp_path / "runner-python"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{FAKE_RUNNER}"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    return wrapper


@pytest.fixture
def materials(tmp_path):
    from maestro.execution.materials import TaskMaterials

    return TaskMaterials(tmp_path / "cache")
