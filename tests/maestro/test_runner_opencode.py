"""OpenCode adapter (U-25 follow-up): calls the model from inside the task container, so
the agent phase may reach the model's host only, and the key is a file for the length of
the agent run, never an argument, an environment variable or part of Harbor's config."""

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from test_harbor_executor import agent_spec, make_executor
from test_runner_attachments import RUNNER_SRC, agents  # noqa: F401 - fixture
from test_setup_network import materials, runner_python  # noqa: F401 - fixtures

ROOT = Path(__file__).resolve().parents[2]
RUNNER = Path(os.environ.get("MAESTRO_RUNNER_PYTHON", ROOT / "runner/.venv/bin/python"))
KEY = "sk-test-opencode-0123456789abcdef"


def model(protocol="openai-compatible"):
    return {
        "protocol": protocol,
        "endpoint": "https://relay.example.test/v1",
        "model_id": "gpt-test",
        "params": {"temperature": 0},
    }


def test_the_config_pins_the_version_and_reads_the_key_from_a_file(agents):  # noqa: F811
    agents.SECRETS["model_api_key"] = KEY
    try:
        found = agents.agent_config({"adapter": "opencode", "model": model(), "params": {}})
    finally:
        agents.SECRETS.clear()
    assert found["import_path"] == "maestro_runner.agents:MaestroOpenCode"
    assert found["model_name"] == "maestro/gpt-test"
    kwargs = found["kwargs"]
    assert kwargs["version"] == agents.OPENCODE_VERSION and kwargs["disable_web_search"]
    provider = kwargs["opencode_config"]["provider"]["maestro"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"] == {
        "baseURL": "https://relay.example.test/v1",
        "apiKey": "{file:/run/maestro/model-key}",
    }
    assert provider["models"] == {"gpt-test": {"options": {"temperature": 0}}}
    assert KEY not in json.dumps(found)
    native = agents.agent_config(
        {"adapter": "opencode", "model": model("anthropic"), "params": {"version": "1.2.3"}}
    )
    assert native["model_name"] == "anthropic/gpt-test" and native["kwargs"]["version"] == "1.2.3"
    for bad in ({"version": "latest"}, {"max_turns": 3}):
        with pytest.raises(agents.InputError):
            agents.agent_config({"adapter": "opencode", "model": model(), "params": bad})


def test_the_agent_phase_reaches_the_model_host_only(runner_python, materials, tmp_path):  # noqa: F811
    spec = agent_spec(materials, tmp_path)
    harness = {**spec.effective_config["harness"], "agent": "opencode", "params": {}}
    spec = replace(spec, effective_config={**spec.effective_config, "harness": harness})
    executor = make_executor(runner_python, materials, tmp_path)
    assert executor._network(spec) == {
        "agent": {"mode": "allowlist", "hosts": ["relay.example.test"]},
        "verifier": {"mode": "no-network"},
    }


DRIVER = """
import asyncio, json, sys, types
sys.path.insert(0, sys.argv[1])
from maestro_runner import agents
from harbor.agents.installed.opencode import OpenCode

seen = {}

async def fake_run(self, instruction, environment, context):
    seen["during"] = environment.files.get(agents.OPENCODE_KEY)
    if sys.argv[2] == "fail":
        raise RuntimeError("model said no")

OpenCode.run = fake_run

class Env:
    default_user = "agent"
    def __init__(self):
        self.files, self.commands = {}, []
    async def exec(self, command, user=None, **kw):
        self.commands.append((command, user))
        if command.startswith("rm -f"):
            self.files.pop(agents.OPENCODE_KEY, None)
        return types.SimpleNamespace(return_code=0, stdout="")
    async def upload_file(self, source, target):
        self.files[target] = open(source).read()

agents.SECRETS["model_api_key"] = sys.argv[3]
cls = agents.MaestroOpenCode
agent = cls.__new__(cls)
env = Env()
try:
    asyncio.run(cls.run(agent, "do it", env, None))
except RuntimeError as error:
    seen["error"] = str(error)
seen["after"] = env.files
seen["commands"] = env.commands
print(json.dumps(seen))
"""


@pytest.mark.parametrize("outcome", ["ok", "fail"])
def test_the_key_file_exists_only_while_the_agent_runs(tmp_path, outcome):
    if not RUNNER.exists():
        pytest.skip("Runner environment not installed")
    result = subprocess.run(
        [str(RUNNER), "-c", DRIVER, str(RUNNER_SRC), outcome, KEY],
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "TMPDIR": str(tmp_path)},
    )
    seen = json.loads(result.stdout.strip().splitlines()[-1])
    assert seen["during"] == KEY and seen["after"] == {}
    assert ("error" in seen) == (outcome == "fail")
    assert all(KEY not in command for command, _ in seen["commands"])
    assert ("chown agent /run/maestro/model-key && chmod 400 /run/maestro/model-key", "root") in [
        tuple(c) for c in seen["commands"]
    ]
    assert list(tmp_path.iterdir()) == []  # the local copy is gone


def test_the_event_stream_is_shown_as_readable_steps(tmp_path):
    from test_fix_artifacts_runner import drive, logs

    out = tmp_path / "run"
    directory = out / "jobs" / "job" / "t1" / "agent"
    directory.mkdir(parents=True)
    stream = directory / "opencode.txt"
    first = [
        {"type": "step_start", "part": {}},
        {"type": "text", "part": {"type": "text", "text": "I will write the answer."}},
        {
            "type": "tool_use",
            "part": {
                "type": "tool",
                "tool": "bash",
                "state": {"input": {"command": "echo ok > /app/answer"}, "output": ""},
            },
        },
    ]
    stream.write_text("".join(json.dumps(e) + "\n" for e in first) + '{"type": "te')
    rest = (
        'xt", "part": {"text": "Done."}}\n'
        + json.dumps(
            {"type": "tool_use", "part": {"tool": "read", "state": {"input": {"path": "/app"}}}}
        )
        + "\n"
    )
    events = drive(out, [["tail"], ["append", str(stream), rest], ["tail"]])
    shown = [(e["source"], e["text"]) for e in logs(events)]
    assert shown == [
        (
            "agent/opencode",
            "--- step 1 · agent ---\nI will write the answer.\n$ echo ok > /app/answer\n",
        ),
        ("agent/opencode", 'Done.\n> read {"path": "/app"}\n'),
    ]
