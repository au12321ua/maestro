"""Model runs need the host to restrict task networks. The service learns it
from the Runner at start and refuses model runs at acceptance when the host cannot, so
no quota is spent on a run the Runner would refuse; the Runner's own check stays last."""

import asyncio
import stat
import sys

import pytest
from sqlalchemy import func, select
from support import create_pat, invite_member, running, setup_admin, submit
from support.configuration import prepare_model
from support.executor import FAKE_RUNNER, make_executor
from support.runs import _drain, accept

from maestro.boundaries.executor import ExecutionReport
from maestro.db import schema as t
from maestro.execution.materials import TaskMaterials


def _wrapper(tmp_path, answer):
    wrapper = tmp_path / f"runner-python-{answer}"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{FAKE_RUNNER}" {answer}\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    return wrapper


@pytest.mark.parametrize(
    ("answer", "expected"), [("network", True), ("no-network", False), ("silent", None)]
)
def test_the_executor_asks_the_runner(tmp_path, answer, expected):
    executor = make_executor(_wrapper(tmp_path, answer), TaskMaterials(tmp_path / "c"), tmp_path)
    assert executor.network_restriction is None
    asyncio.run(executor.probe())
    assert executor.network_restriction is expected


def _accepted_runs(service) -> int:
    with service.db.read() as conn:
        return conn.execute(select(func.sum(t.pats.c.accepted_runs))).scalar() or 0


def test_model_runs_are_refused_at_acceptance_when_the_host_cannot_restrict(
    service, version_id, executor
):
    executor.network_restriction = False
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        model_id, harness_id = prepare_model(admin)
        member = invite_member(service, admin, "member")
        pat = create_pat(member, run_limit=3)
        agent = {
            "execution_mode": "agent",
            "model_config_id": model_id,
            "harness_version_id": harness_id,
        }
        resolution = pat.post(
            "/api/v1/config-resolutions",
            json={"dataset_version_id": version_id, **agent},
            expect=201,
        ).json()["data"]
        assert [b["code"] for b in resolution["blockers"]] == ["NETWORK_POLICY_UNSUPPORTED"]
        refused = submit(pat, version_id, **agent)
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == "CONFIG_INCOMPATIBLE"
        assert refused.json()["error"]["field_errors"][0]["path"] == "/execution_mode"
        assert _accepted_runs(service) == 0
        # Oracle runs never restrict the network and are still accepted.
        assert submit(pat, version_id).status_code == 202
        features = pat.get("/api/v1/capabilities").json()["data"]["features"]
        assert features["restricted_network"] is False

        # Unknown (the probe failed) does not block: the Runner refuses if it must.
        executor.network_restriction = None
        assert submit(pat, version_id, **agent).status_code == 202
        executor.network_restriction = True
        assert submit(pat, version_id, **agent).status_code == 202
        assert pat.get("/api/v1/capabilities").json()["data"]["features"]["restricted_network"]


def test_a_runner_refusal_ends_trials_as_errors_not_scores(service, owner, version_id, executor):
    async def refuse(spec, sink, cancel):
        return ExecutionReport(cleanup_complete=True, failure_code="NETWORK_POLICY_UNSUPPORTED")

    executor.execute = refuse
    data = accept(service, owner, version_id)
    asyncio.run(_drain(service))
    run = service.runs.get(owner, data["run_id"])
    assert run["state"] == "finished" and run["counts"]["scored"] == 0
    trials = service.runs.trials(data["run_id"])
    assert {trial["state"] for trial in trials} <= {"execution_error", "not_started"}
    assert all(trial["result"] is None or trial["result"]["score"] is None for trial in trials)
    assert {trial["reason_code"] for trial in trials} == {"NETWORK_POLICY_UNSUPPORTED"}
