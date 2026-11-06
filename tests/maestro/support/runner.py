"""Shared runner helpers for tests."""

import json
import os
import subprocess
import sys
from pathlib import Path

from support import ROOT

RUNNER_SRC = ROOT / "runner" / "src"


RUNNER = Path(os.environ.get("MAESTRO_RUNNER_PYTHON", ROOT / "runner/.venv/bin/python"))


DRIVER = """
import json, sys, types
sys.path.insert(0, sys.argv[1])
from maestro_runner import job

spec = json.loads(sys.argv[2])
for name, value in spec.get("constants", {}).items():
    setattr(job, name, value)
limits = {"max_files": 500, "max_file_bytes": 64 << 20, "max_total_bytes": 1 << 30}
limits.update(spec.get("limits", {}))
ex = job.Execution({"output_dir": spec["out"], "retry": {"max_retries": 0, "exclude_exceptions": []},
                    "log_cap_bytes": spec.get("cap", 1 << 20), "artifact_limits": limits})
ex.bound["t1"] = "TRIAL_X"
ex.live.add("t1")
ex.attempt["t1"] = 1
none = None
event = types.SimpleNamespace(
    trial_name="t1",
    result=types.SimpleNamespace(
        exception_info=none, verifier_result=none, verifier=none,
        environment_setup=none, agent_setup=none, agent_execution=none, agent_result=None,
    ),
)
for step, *rest in spec["steps"]:
    if step == "tail":
        ex.tail()
    elif step == "append":
        with open(rest[0], "a") as handle:
            handle.write(rest[1])
    elif step == "retry":
        ex.attempt["t1"] += 1
        ex.live.add("t1")
    elif step == "finish":
        ex._finish(event, "TRIAL_X")
    elif step == "open_below":
        handle = job._open_below(__import__("pathlib").Path(rest[0]), __import__("pathlib").Path(rest[1]))
        job.emit("probe", opened=handle is not None, text=handle.read().decode() if handle else None)
"""


def drive(out: Path, steps: list, **spec) -> list[dict]:
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            DRIVER,
            str(RUNNER_SRC),
            json.dumps({"out": str(out), "steps": steps, **spec}),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [
        json.loads(line)["payload"] | {"_type": json.loads(line)["type"]}
        for line in result.stdout.splitlines()
    ]


def logs(events: list[dict]) -> list[dict]:
    return [e for e in events if e["_type"] == "log"]
