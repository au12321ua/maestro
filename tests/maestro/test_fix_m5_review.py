"""Regressions for the findings of the independent M5 review (2026-10-24)."""

import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from support import invite_member, running, setup_admin, submit, wait_operation
from test_attachments import manifest, package, publish, upload
from test_batches import batch_body, post_batch
from test_configuration import error
from test_datasets import import_and_prepare
from test_reruns import rerun

from maestro.core.errors import MaestroError
from maestro.execution.agents import endpoint_problem
from maestro.testing.fakes import FakeDatasetSource


class _Low:
    def refuse_if_low(self, work="runs"):
        raise MaestroError("STORAGE_LOW", "low", 503)


# --- idempotency: a retry finds its answer even after what it relied on changed ----------


def test_a_retry_replays_after_the_plan_changed_or_was_archived(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        template = {"dataset_version_id": version_id, "execution_mode": "oracle"}
        plan = admin.post(
            "/api/v1/plans", json={"name": "p", "template": template}, expect=201
        ).json()["data"]
        body = {"name": "from plan", "plan_id": plan["id"]}
        key = uuid.uuid4().hex
        first = admin.post("/api/v1/runs", json=body, headers={"Idempotency-Key": key})
        assert first.status_code == 202, first.text
        admin.put(
            f"/api/v1/plans/{plan['id']}",
            json={"expected_revision": 1, "template": {**template, "repetitions": 2}},
            expect=200,
        )
        again = admin.post("/api/v1/runs", json=body, headers={"Idempotency-Key": key})
        assert again.status_code == 202, again.text
        assert again.json()["data"]["run_id"] == first.json()["data"]["run_id"]
        admin.post(f"/api/v1/plans/{plan['id']}/archive", json={"archived": True}, expect=200)
        again = admin.post("/api/v1/runs", json=body, headers={"Idempotency-Key": key})
        assert again.json()["data"]["run_id"] == first.json()["data"]["run_id"]


def test_batch_and_rerun_retries_replay_after_limits_and_sources_changed(
    service, version_id, executor
):
    with running(service) as client:
        admin = setup_admin(service, client)
        oracle = {"execution_mode": "oracle"}
        body = batch_body(version_id, oracle, oracle)
        accepted = post_batch(admin, body, key="batch-key").json()["data"]
        wait_operation(admin, accepted["operation_id"])
        admin.put(
            "/api/v1/admin/team-policy",
            json={"expected_revision": 0, "values": {"batch_max_runs": 1}},
            expect=200,
        )
        again = post_batch(admin, body, key="batch-key")
        assert again.status_code == 202, again.text
        assert again.json()["data"]["batch_id"] == accepted["batch_id"]

        source = accepted["runs"][0]["run_id"]
        first = rerun(admin, source, expect=202, key="rerun-key")
        admin.delete(f"/api/v1/runs/{source}", expect=200)
        again = rerun(admin, source, expect=202, key="rerun-key")
        assert again.json()["data"]["run_id"] == first.json()["data"]["run_id"]


def test_fields_beside_a_resolution_id_are_refused(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        found = admin.post(
            "/api/v1/config-resolutions",
            json={"dataset_version_id": version_id, "execution_mode": "oracle"},
            expect=201,
        ).json()["data"]
        response = admin.post(
            "/api/v1/runs",
            json={"name": "x", "resolution_id": found["resolution_id"], "repetitions": 3},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert error(response) == (422, "CONFIG_VALIDATION_FAILED")
        assert response.json()["error"]["field_errors"][0]["path"] == "/repetitions"


# --- reruns and batches --------------------------------------------------------------------


def test_a_rerun_respects_the_storage_watermark(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        source = submit(admin, version_id).json()["data"]["run_id"]
        admin.post(f"/api/v1/runs/{source}/cancel", expect=200)
        service.runs.disk = _Low()
        assert error(rerun(admin, source)) == (503, "STORAGE_LOW")


def test_a_batch_shows_its_members_deletion_and_ignores_cancel_once_ended(
    service, version_id, executor
):
    with running(service) as client:
        admin = setup_admin(service, client)
        oracle = {"execution_mode": "oracle"}
        accepted = post_batch(admin, batch_body(version_id, oracle, oracle)).json()["data"]
        wait_operation(admin, accepted["operation_id"])
        batch_url = f"/api/v1/run-batches/{accepted['batch_id']}"

        cancelled = admin.post(f"{batch_url}/cancel", expect=200).json()["data"]
        assert cancelled["stop_requested"] is False
        assert cancelled["status"] == "succeeded"

        member = accepted["runs"][0]["run_id"]
        admin.delete(f"/api/v1/runs/{member}", expect=200)
        batch = admin.get(batch_url, expect=200).json()["data"]
        shown = {r["id"]: r for r in batch["runs"]}[member]
        assert shown["deletion"]["state"] == "recycled"
        assert "rerun" not in shown["allowed_actions"]
        assert batch["progress"]["deleted_runs"] == 1


# --- configuration ---------------------------------------------------------------------


def test_plan_templates_check_task_ids_and_agent_only_fields(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "member")
        response = member.post(
            "/api/v1/plans",
            json={
                "name": "p",
                "template": {"task_selection": {"mode": "all_eligible", "task_ids": 5}},
            },
        )
        assert error(response) == (422, "REQUEST_INVALID")  # the contract types task_ids
        paths = [e["path"] for e in response.json()["error"]["field_errors"]]
        assert paths == ["/template/task_selection/task_ids"]
        plan = member.post(
            "/api/v1/plans",
            json={"name": "q", "template": {"harness_params": {"max_turns": 5}}},
            expect=201,
        ).json()["data"]
        assert plan["template"] == {"harness_params": {"max_turns": 5}}
        plan = member.post(
            "/api/v1/plans", json={"name": "r", "template": {"model_revision": 3}}, expect=201
        ).json()["data"]
        assert plan["template"] == {"model_revision": 3}


def test_an_endpoint_with_a_key_in_its_path_is_refused():
    assert endpoint_problem("https://gw.example.test/sk-proj-ABCDEFGHIJKLMNOPQRSTUV/v1")
    assert endpoint_problem("https://gw.example.test/v1;key=x")
    assert endpoint_problem("https://gw.example.test/v1") is None


# --- attachments -----------------------------------------------------------------------------


def test_odd_manifests_are_invalid_not_internal_errors(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        for data in (
            json.dumps({**manifest(), "effects": [{}]}).encode(),
            json.dumps({**manifest(), "provides": {"tools": [[1]]}}).encode(),
            b'{"x":' + b"[" * 30000 + b"]" * 30000 + b"}",
        ):
            assert error(publish(admin, {"manifest.json": data})) == (422, "ATTACHMENT_INVALID")


def test_publishing_again_repairs_a_lost_file_list(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        created = publish(admin, package(), expect=201).json()["data"]
        hex_digest = created["content_digest"].split(":")[1]
        listed = service.settings.data_dir / "attachments" / "snapshots" / f"{hex_digest}.json"
        listed.unlink()
        assert service.attachments.store.verified(created["content_digest"]) is None
        publish(admin, package(), expect=200)
        assert listed.is_file()


def test_low_disk_refuses_an_upload_before_it_is_stored(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        service.disk = _Low()
        assert error(upload(admin, b"data")) == (503, "STORAGE_LOW")
    assert not (service.settings.data_dir / "attachments" / "blobs").exists()


# --- author parameters never reach the Runner's own environment ----------------------------


def test_a_task_declaring_a_reserved_name_is_blocked_even_with_only_a_default(service, source):
    files = {
        "evil": {
            **FakeDatasetSource.task_files("demo", "params", "evil"),
            "task.toml": (
                "[environment]\ncpus = 1\nmemory_mb = 2048\n"
                '[verifier.env]\nA = "${LD_PRELOAD:-/tmp/evil.so}"\n'
                'B = "${PATH:-/tmp/bin:/usr/bin}"\n'
                'C = "${HTTPS_PROXY:-http://proxy.example:8080}"\n'
            ),
        },
    }
    source.add("demo", "params", "v1", list(files), files=files)
    with running(service) as client:
        admin = setup_admin(service, client)
        version_id, _ = import_and_prepare(admin, name="params")
        found = admin.post(
            "/api/v1/config-resolutions",
            json={"dataset_version_id": version_id, "execution_mode": "oracle"},
            expect=201,
        ).json()["data"]
        assert found["effective_config"].get("parameters") in ({}, None)
        assert found["blockers"] or found["excluded_tasks"]


def test_the_runner_refuses_reserved_names_in_task_env():
    """The Runner's own copy of the list agrees with the platform's."""
    from maestro.execution.materials import RESERVED_PARAMETER

    probe = (
        "import sys; sys.path.insert(0, sys.argv[1]); from maestro_runner import job; "
        "print(job.RESERVED_ENV.pattern, file=sys.stderr)"
    )
    runner_src = Path(__file__).resolve().parents[2] / "runner" / "src"
    found = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(runner_src)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert found.stderr.strip() == RESERVED_PARAMETER.pattern


# --- network tightening covers every step and refuses services it cannot route ------------

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
    root = Path(__file__).resolve().parents[2] / "runner"
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
