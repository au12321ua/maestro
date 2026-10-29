"""Scripted stand-in for the Runner: speaks protocol v1 without Harbor or Docker.

The first task key picks the behaviour, so one executor can exercise protocol
errors, crashes, silence, cancellation and stderr floods.
"""

import json
import os
import signal
import sys
import time
from pathlib import Path

spec = json.loads(sys.stdin.read())
if spec.get("command") == "probe":
    # The wrapper's argument scripts the host: network, no-network, or silent.
    answer = sys.argv[1] if len(sys.argv) > 1 else "network"
    if answer != "silent":
        payload = {"status": "ok", "data": {"network_restriction": answer == "network"}}
        print(json.dumps({"protocol_version": 2, "type": "result", "payload": payload}))
    sys.exit(0)
out = Path(spec["output_dir"])
mode = spec["tasks"][0]["task_key"]
# What arrived, for tests that check the request.
(out.parent / "request.json").write_text(json.dumps(spec))
sequence = 0


def emit(kind, run_id=None, **payload):
    global sequence
    sequence += 1
    line = {
        "protocol_version": 2,
        "run_id": run_id or spec["run_id"],
        "sequence": sequence,
        "type": kind,
        "at": "2026-10-23T00:00:00+00:00",
        "payload": payload,
    }
    sys.stdout.write(json.dumps(line) + "\n")
    sys.stdout.flush()


def finish_attempt(trial_id, attempt, rewards, exception=None, retry=False, files=None):
    archive = out / "attempts" / trial_id / str(attempt)
    (archive / "verifier").mkdir(parents=True, exist_ok=True)
    (archive / "verifier" / "reward.txt").write_text(f"{rewards and rewards['reward']}\n")
    (archive / "trial.log").write_text("api_key=sk-abcdefghijklmnopqrstuv\n")
    if files is None:
        import hashlib

        files = [
            {
                "path": p,
                "size": (archive / p).stat().st_size,
                "sha256": hashlib.sha256((archive / p).read_bytes()).hexdigest(),
            }
            for p in ("verifier/reward.txt", "trial.log")
        ]
    emit(
        "attempt_finished",
        trial_id=trial_id,
        attempt=attempt,
        exception_type=exception,
        exception_message=exception and f"{exception} raised",
        rewards=rewards,
        verifier_ran=rewards is not None,
        will_retry=retry,
        archive=archive.relative_to(out).as_posix(),
        files=files,
        timing={},
    )


def run_trial(trial_id, rewards=None, steps=("environment", "agent", "collecting", "verifying")):
    emit("attempt_started", trial_id=trial_id, attempt=1, harbor_trial_name=f"t__{trial_id}")
    for step in steps:
        emit("phase_changed", trial_id=trial_id, attempt=1, step=step)
    emit("log", trial_id=trial_id, attempt=1, source="agent/oracle.txt", text="hello\n")


trials = [t for task in spec["tasks"] for t in task["trial_ids"]]
harbor = "9.9.9" if mode == "harbor-mismatch" else "0.24.0"
emit("started", harbor_version=harbor, runner_version="fake", n_trials=len(trials))

if mode in ("ok", "flood"):
    if mode == "flood":
        chunk = b"x" * 65536 + b"\n"
        for _ in range(320):  # ~20 MiB: the worker must keep draining stderr
            sys.stderr.buffer.write(chunk)
        sys.stderr.flush()
    for index, trial_id in enumerate(trials):
        run_trial(trial_id)
        finish_attempt(trial_id, 1, {"reward": float(index % 2)})
    emit("completed", status="completed")
elif mode == "retry":
    trial_id = trials[0]
    run_trial(trial_id)
    finish_attempt(trial_id, 1, None, exception="ApiRateLimitError", retry=True)
    emit("attempt_started", trial_id=trial_id, attempt=2)
    emit("phase_changed", trial_id=trial_id, attempt=2, step="verifying")
    finish_attempt(trial_id, 2, {"reward": 1.0})
    for trial_id in trials[1:]:
        run_trial(trial_id)
        finish_attempt(trial_id, 1, {"reward": 0.0})
    emit("completed", status="completed")
elif mode == "garbage":
    sys.stdout.write("progress 50% |#####     |\n")
    sys.stdout.flush()
    time.sleep(30)
elif mode == "gap":
    sequence += 1
    emit("heartbeat")
    time.sleep(30)
elif mode == "harbor-mismatch":
    time.sleep(30)
elif mode == "badpath":
    run_trial(trials[0])
    finish_attempt(
        trials[0],
        1,
        {"reward": 1.0},
        files=[{"path": "../../../etc/hostname", "size": 1, "sha256": "0"}],
    )
    time.sleep(30)
elif mode == "silent":
    run_trial(trials[0])
    time.sleep(30)
elif mode == "crash":
    run_trial(trials[0])
    finish_attempt(trials[0], 1, {"reward": 1.0})
    run_trial(trials[1])
    os._exit(9)
elif mode == "cancel":

    def on_term(*_):
        for trial_id in trials:
            finish_attempt(trial_id, 1, None, exception="CancelledError")
        emit("completed", status="cancelled")
        sys.exit(3)

    signal.signal(signal.SIGTERM, on_term)
    for trial_id in trials:
        run_trial(trial_id, steps=("environment", "agent"))
    while True:
        time.sleep(1)
        emit("heartbeat")
elif mode == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    run_trial(trials[0], steps=("environment",))
    while True:
        time.sleep(0.2)
        emit("heartbeat")
elif mode == "agent":
    # Echo what arrived so the test can check it, and put the key everywhere it could
    # leak: a live log, an archived file, an exception message and stderr.
    (out.parent / "request.json").write_text(json.dumps(spec))
    key = spec["secrets"]["model_api_key"]
    emit("network_plan", task_key=mode, plan={"agent": {"effective": spec["network"]["agent"]}})
    sys.stderr.write(f"litellm said {key}\n")
    trial_id = trials[0]
    emit("attempt_started", trial_id=trial_id, attempt=1)
    emit("log", trial_id=trial_id, attempt=1, source="agent/episode-0/response.txt", text=key)
    archive = out / "attempts" / trial_id / "1"
    (archive / "agent").mkdir(parents=True, exist_ok=True)
    (archive / "agent" / "trajectory.json").write_text(json.dumps({"key": key}))
    emit(
        "attempt_finished",
        trial_id=trial_id,
        attempt=1,
        exception_type="AuthenticationError",
        exception_message=f"Incorrect API key provided: {key}",
        rewards=None,
        verifier_ran=False,
        will_retry=False,
        archive=archive.relative_to(out).as_posix(),
        files=[
            {
                "path": "agent/trajectory.json",
                "size": (archive / "agent" / "trajectory.json").stat().st_size,
                "sha256": __import__("hashlib")
                .sha256((archive / "agent" / "trajectory.json").read_bytes())
                .hexdigest(),
            }
        ],
        usage={"input_tokens": 12, "cache_tokens": 0, "output_tokens": 3, "cost_usd": None},
        timing={},
    )
    emit("completed", status="completed")
elif mode == "attachments":
    # One trial installs and scores; the other's install script fails.
    (out.parent / "request.json").write_text(json.dumps(spec))
    record = {
        "name": spec["attachments"][0]["name"],
        "label": spec["attachments"][0]["label"],
        "content_digest": spec["attachments"][0]["content_digest"],
        "target": "/opt/maestro/attachments/00-tools",
    }
    for index, trial_id in enumerate(trials):
        emit("attempt_started", trial_id=trial_id, attempt=1)
        emit("phase_changed", trial_id=trial_id, attempt=1, step="environment")
        emit("phase_changed", trial_id=trial_id, attempt=1, step="installing")
        failed = index == 1
        emit(
            "attempt_finished",
            trial_id=trial_id,
            attempt=1,
            exception_type="AttachmentInstallFailed" if failed else None,
            exception_message="bootstrap.sh of tools exited with 3" if failed else None,
            rewards=None if failed else {"reward": 1.0},
            verifier_ran=not failed,
            will_retry=False,
            archive=None,
            files=[],
            attachments=[
                {**record, "status": "failed" if failed else "ok", "exit_code": 3 if failed else 0}
            ],
            timing={},
        )
    emit("completed", status="completed")
elif mode == "rejected":
    emit("completed", status="rejected", code="TASK_MATERIAL_MISSING", message="no task.toml")
