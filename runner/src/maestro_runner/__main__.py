"""Runner entry: ``python -I -m maestro_runner`` with the JSON input on stdin.

Exit codes: 0 completed, 2 runner error, 3 cancelled, 4 rejected. The worker
decides business outcomes from events, never from the exit code alone.
"""

import asyncio
import ctypes
import json
import os
import signal
import sys
import traceback

from maestro_runner import protocol  # first: claims stdout before Harbor loads
from maestro_runner.agents import SECRETS
from maestro_runner.job import Execution, Rejected, check_spec
from maestro_runner.protocol import emit


def _stop_with_parent() -> None:
    """SIGTERM when the service process dies (Linux), so an orphaned Runner ends its
    trials cooperatively instead of executing on; restart reconciliation still cleans."""
    parent = os.getppid()
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        return
    if os.getppid() != parent:  # the parent died before the request took effect
        os.kill(os.getpid(), signal.SIGTERM)


def hub_command(spec: dict) -> int:
    """One catalog command; the answer is a single ``result`` event."""
    from maestro_runner import hub

    command = hub.COMMANDS.get(spec.get("op"))
    if command is None:
        emit("result", status="error", kind="unsupported", message="unknown hub command")
        return 4
    _stop_with_parent()
    try:
        data = asyncio.run(command(spec))
    except Exception as error:  # noqa: BLE001 - classified and reported as an event
        traceback.print_exc()
        failure = hub.classify(error)
        emit(
            "result",
            status="error",
            kind=failure.kind,
            message=str(failure)[: hub.MESSAGE_LIMIT],
            evidence=failure.evidence,
        )
        return 2
    emit("result", status="ok", data=data)
    return 0


def probe_command() -> int:
    """What the service checks before accepting a model run (exit condition): the same
    test ``Execution`` applies before restricting a run's network, so acceptance and
    execution agree. Harbor's kernel check is optimistic when the kernel configuration
    cannot be read; the restriction itself is verified by the real-Docker tests."""
    from maestro_runner import network

    try:
        supported = network.supported()
    except Exception as error:  # noqa: BLE001 - reported, the service keeps "unknown"
        emit("result", status="error", kind="unsupported", message=str(error)[:500])
        return 2
    emit("result", status="ok", data={"network_restriction": bool(supported)})
    return 0


def main() -> int:
    try:
        spec = json.loads(sys.stdin.read())
        if spec.get("protocol_version") == protocol.PROTOCOL_VERSION:
            if spec.get("command") == "hub":
                return hub_command(spec)
            if spec.get("command") == "probe":
                return probe_command()
        protocol.run_id = spec.get("run_id")
        check_spec(spec)
        # Model credentials stay in memory (SECRETS), never in the environment or
        # any file. Author parameter values are the task's own input: Harbor resolves
        # task.toml templates from the environment.
        SECRETS.update(spec.pop("secrets", None) or {})
        os.environ.update(spec.pop("task_env", None) or {})
    except (ValueError, Rejected) as error:
        code = error.code if isinstance(error, Rejected) else "RUNNER_INPUT_INVALID"
        emit("completed", status="rejected", code=code, message=str(error))
        return 4
    execution = Execution(spec)
    loop = asyncio.new_event_loop()
    task = loop.create_task(execution.run())

    parent = os.getppid()

    def cancel() -> None:
        # Cooperative cancel: Harbor ends each trial (CANCEL, END) and stops its containers.
        if execution.cancelling:
            return
        if os.getppid() != parent or protocol.lost():
            # Orphaned: nobody reads stdout or stderr any more. Writes must not fail
            # inside Harbor's cleanup, so diagnostics go nowhere from here on.
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
        execution.cancelling = True
        emit("cancel_requested")
        task.cancel()

    loop.add_signal_handler(signal.SIGTERM, cancel)
    protocol.on_lost = lambda: loop.call_soon_threadsafe(cancel)
    _stop_with_parent()
    try:
        status = loop.run_until_complete(task)
    except asyncio.CancelledError:
        status = "cancelled"
    except Rejected as error:
        emit("completed", status="rejected", code=error.code, message=str(error))
        return 4
    except Exception as error:  # noqa: BLE001 - reported as a protocol event
        traceback.print_exc()
        emit(
            "completed",
            status="runner_error",
            code="RUNNER_FAILED",
            message=f"{type(error).__name__}: {error}"[:2000],
        )
        return 2
    emit("completed", status=status)
    return 0 if status == "completed" else 3


if __name__ == "__main__":
    sys.exit(main())
