"""The Runner cannot start environments without the attempt's live permission."""

import json
import subprocess

from support.runner import RUNNER


def test_resource_commands_are_idempotent_and_fenced_to_the_attempt():
    script = """
import asyncio
from maestro_runner.resources import Permits
from maestro_runner.protocol import emit
async def exercise():
    gate = Permits("attempt_a")
    request = asyncio.create_task(gate.acquire("execution"))
    await asyncio.sleep(0)
    grant = {"attempt_id": "attempt_a", "phase": "execution", "command": "permit_granted", "permit_id": "p1"}
    gate.accept(grant)
    gate.accept(grant)
    assert await request == "p1"
    assert await gate.acquire("execution") == "p1"
    release = asyncio.create_task(gate.release("execution"))
    await asyncio.sleep(0)
    try:
        await gate.acquire("execution")
    except RuntimeError:
        pass
    else:
        raise AssertionError("restarted an environment during release")
    receipt = {**grant, "command": "permit_released"}
    gate.accept(receipt)
    await release
    gate.accept(receipt)
    try:
        await gate.acquire("execution")
    except RuntimeError:
        pass
    else:
        raise AssertionError("reused a retired environment permit")
    for bad in ({**grant, "attempt_id": "old_attempt"}, {**grant, "permit_id": "other_permit"}):
        try:
            gate.accept(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("stale or conflicting grant was accepted")
    gate.close()
    try:
        await gate.acquire("verification")
    except RuntimeError:
        pass
    else:
        raise AssertionError("started after losing channel")
    emit("checked", ok=True)
asyncio.run(exercise())
"""
    result = subprocess.run(
        [str(RUNNER), "-I", "-c", script], capture_output=True, text=True, timeout=20, check=False
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1])["payload"] == {"ok": True}


def test_multi_step_agent_end_keeps_model_quota_until_the_trial_ends():
    script = """
from pathlib import Path
from types import SimpleNamespace as NS
from maestro_runner.job import Execution
ex = Execution({"output_dir": "/tmp", "retry": {}, "artifact_limits": {}, "attempt_id": "att"})
ex._trial_id = lambda event: "trial"
ex._finish = lambda *args: None
ex.attempt["trial"] = 1
ex.multi_step_paths.add("/tmp/multistep")
def event(kind, path):
    return NS(event=NS(value=kind), trial_name="trial", config=NS(task=NS(path=Path(path))),
              timestamp=NS(isoformat=lambda: "now"))
ex._handle(event("agent-end", "/tmp/multistep"))
ex._handle(event("agent-end", "/tmp/multistep"))
ex._handle(event("end", "/tmp/multistep"))
ex._handle(event("agent-end", "/tmp/singlestep"))
"""
    result = subprocess.run(
        [str(RUNNER), "-I", "-c", script], capture_output=True, text=True, timeout=20, check=False
    )
    assert result.returncode == 0, result.stderr
    kinds = [json.loads(line)["type"] for line in result.stdout.splitlines()]
    assert kinds == [
        "phase_changed",
        "phase_changed",
        "model_finished",
        "model_finished",
        "phase_changed",
    ]
