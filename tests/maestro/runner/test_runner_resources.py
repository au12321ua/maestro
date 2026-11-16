"""The Runner cannot start environments without the attempt's live permission."""

import json
import subprocess

import pytest
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


@pytest.mark.parametrize("phase", ["execution", "verification"])
@pytest.mark.parametrize("start_fails", [False, True])
def test_start_timeout_drains_the_command_before_stopping_and_releasing(
    tmp_path, phase, start_fails
):
    script = """
import asyncio, json, sys
from pathlib import Path
from types import SimpleNamespace
from harbor.environments.docker.docker import DockerEnvironment
from maestro_runner import resources
from maestro_runner.protocol import emit

async def exercise():
    phase, fails = sys.argv[2], sys.argv[3] == "True"
    marker = Path(sys.argv[1]) / "created"
    gate = resources.Permits("attempt")
    process = None
    events = []
    def event(kind, **payload):
        events.append(kind)
        if kind == "resource_requested":
            command = "permit_granted"
        elif kind == "resource_released":
            assert process.returncode is not None, "released while creator still runs"
            command = "permit_released"
        else:
            return
        gate.accept({"command": command, "attempt_id": "attempt", "phase": phase,
                     "permit_id": "permit"})
    resources.emit = event
    spawned = asyncio.Event()
    async def start():
        nonlocal process
        assert process is None, "a new creator entered while timeout cleanup was draining"
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-c",
            "import time,sys; from pathlib import Path; time.sleep(.3); Path(sys.argv[1]).touch()",
            str(marker), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        spawned.set()
        await DockerEnvironment._collect_buffered_output(process, timeout_sec=None)
        if fails:
            raise RuntimeError("start failed after its command finished")
    async def stop():
        assert process.returncode is not None, "cleanup raced a live startup command"
        assert marker.exists()
        marker.unlink()
    environment = gate.wrap_environment(SimpleNamespace(start=start, stop=stop), phase)
    task = asyncio.create_task(environment.start())
    async def probe_closing():
        await asyncio.sleep(.12)
        try:
            await environment.start()
        except RuntimeError:
            return
        raise AssertionError("new start allowed while timeout cleanup was draining")
    probe = None
    try:
        await spawned.wait()
        probe = asyncio.create_task(probe_closing())
        # Repeat cancellation while timeout cleanup is already waiting for the CLI.
        asyncio.get_running_loop().call_later(.1, task.cancel)
        try:
            await asyncio.wait_for(task, .05)
        except TimeoutError:
            pass
        else:
            raise AssertionError("lost the environment-start timeout")
        await probe
        await environment.stop()
        assert not marker.exists() and not gate.held
        assert events == ["resource_requested", "resource_released"]
        emit("checked", creator_exited=True, cleaned=True, phase=phase)
    finally:
        if probe is not None:
            probe.cancel()
            await asyncio.gather(probe, return_exceptions=True)
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()

asyncio.run(exercise())
"""
    result = subprocess.run(
        [str(RUNNER), "-I", "-c", script, str(tmp_path), phase, str(start_fails)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1])["payload"] == {
        "creator_exited": True,
        "cleaned": True,
        "phase": phase,
    }


def test_stop_closes_new_starts_and_finishes_cleanup_despite_repeated_cancel():
    script = """
import asyncio
from types import SimpleNamespace
from maestro_runner import resources
from maestro_runner.protocol import emit

async def exercise():
    gate = resources.Permits("attempt")
    starting, started, stopping, stopped = [asyncio.Event() for _ in range(4)]
    events = []
    def event(kind, **payload):
        events.append(kind)
        command = "permit_granted" if kind == "resource_requested" else "permit_released"
        gate.accept({"command": command, "attempt_id": "attempt", "phase": "execution",
                     "permit_id": "permit"})
    resources.emit = event
    async def start():
        starting.set()
        await started.wait()
    async def stop():
        stopping.set()
        await stopped.wait()
    env = gate.wrap_environment(SimpleNamespace(start=start, stop=stop), "execution")
    launch = asyncio.create_task(env.start())
    await starting.wait()
    close = asyncio.create_task(env.stop())
    await asyncio.sleep(0)
    close.cancel()
    await asyncio.sleep(0)
    assert not stopping.is_set() and gate.held == {"execution": "permit"}
    try:
        await env.start()
    except RuntimeError:
        pass
    else:
        raise AssertionError("started after shutdown began")
    started.set()
    await launch
    await stopping.wait()
    close.cancel()
    await asyncio.sleep(0)
    assert events == ["resource_requested"] and gate.held == {"execution": "permit"}
    stopped.set()
    try:
        await close
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("lost the caller's cancellation")
    assert events == ["resource_requested", "resource_released"] and not gate.held
    await env.stop()
    assert events == ["resource_requested", "resource_released"]
    emit("checked", closed=True)

asyncio.run(exercise())
"""
    result = subprocess.run(
        [str(RUNNER), "-I", "-c", script], capture_output=True, text=True, timeout=20, check=False
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1])["payload"] == {"closed": True}


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
