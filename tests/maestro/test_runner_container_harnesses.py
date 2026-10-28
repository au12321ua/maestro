"""Harnesses that run in the task container (OpenCode, Codex, Claude Code; N-09) call the
model from there, so the agent phase may reach the model's host only, and the key is a
file for the length of the agent run, never an argument, an environment variable or
part of Harbor's config. Each installs the exact upstream version of its harness
version, and its event stream reads as steps while it works."""

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
KEY = "sk-test-harness-0123456789abcdef"


def model(protocol="openai-compatible", **params):
    return {
        "protocol": protocol,
        "endpoint": "https://relay.example.test/v1",
        "model_id": "gpt-test",
        "params": params,
    }


def config(agents, adapter, protocol, version="1.2.3", params=None, **model_params):  # noqa: F811
    agents.SECRETS["model_api_key"] = KEY
    try:
        return agents.agent_config(
            {
                "adapter": adapter,
                "model": model(protocol, **model_params),
                "params": params or {},
                "version": version,
            }
        )
    finally:
        agents.SECRETS.clear()


def test_opencode_reads_the_key_from_a_file(agents):  # noqa: F811
    found = config(agents, "opencode", "openai-compatible", temperature=0)
    assert found["import_path"] == "maestro_runner.agents:MaestroOpenCode"
    assert found["model_name"] == "maestro/gpt-test"
    kwargs = found["kwargs"]
    assert kwargs["version"] == "1.2.3" and kwargs["disable_web_search"]
    provider = kwargs["opencode_config"]["provider"]["maestro"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"] == {
        "baseURL": "https://relay.example.test/v1",
        "apiKey": "{file:/run/maestro/model-key}",
    }
    assert provider["models"] == {"gpt-test": {"options": {"temperature": 0}}}
    assert KEY not in json.dumps(found)
    assert config(agents, "opencode", "anthropic")["model_name"] == "anthropic/gpt-test"
    # The 2.x line reads the same config through its own class.
    second = config(agents, "opencode-2", "openai-compatible", temperature=0)
    assert second["import_path"] == "maestro_runner.agents:MaestroOpenCode2"
    assert second["kwargs"] == kwargs


def test_codex_gets_the_endpoint_in_its_config_and_the_key_as_auth_json(agents):  # noqa: F811
    found = config(agents, "codex", "openai", reasoning_effort="high")
    assert found["import_path"] == "maestro_runner.agents:MaestroCodex"
    assert found["model_name"] == "gpt-test"
    # Harbor writes OPENAI_BASE_URL into config.toml as openai_base_url (not a secret).
    assert found["env"] == {"OPENAI_BASE_URL": "https://relay.example.test/v1"}
    assert found["kwargs"] == {
        "version": "1.2.3",
        "disable_web_search": True,
        "reasoning_effort": "high",
    }
    assert KEY not in json.dumps(found)


def test_claude_code_reads_the_key_through_an_api_key_helper(agents):  # noqa: F811
    found = config(
        agents, "claude-code", "anthropic", params={"max_turns": 7}, max_thinking_tokens=2048
    )
    assert found["import_path"] == "maestro_runner.agents:MaestroClaudeCode"
    assert found["model_name"] == "gpt-test"
    # Claude Code adds /v1/messages to its base URL itself.
    assert found["env"] == {"ANTHROPIC_BASE_URL": "https://relay.example.test"}
    assert found["kwargs"] == {
        "max_turns": 7,
        "max_thinking_tokens": 2048,
        "version": "1.2.3",
        "disable_web_search": True,
        "config": {"apiKeyHelper": "cat /run/maestro/model-key"},
    }
    assert KEY not in json.dumps(found)


@pytest.mark.parametrize(
    ("adapter", "protocol", "version", "params", "model_params"),
    [
        ("opencode", "openai-compatible", None, {}, {}),
        ("opencode", "openai-compatible", "latest", {}, {}),
        ("opencode", "openai-compatible", "^1.2", {}, {}),
        ("opencode", "openai-compatible", "1.2.3", {"version": "1.2.3"}, {}),
        ("codex", "anthropic", "1.2.3", {}, {}),
        ("codex", "openai-compatible", "1.2.3", {}, {}),
        ("codex", "openai", "1.2.3", {}, {"temperature": 0}),
        ("claude-code", "openai", "1.2.3", {}, {}),
        ("claude-code", "anthropic", "1.2.3", {"model": "x"}, {}),
        ("claude-code", "anthropic", "1.2.3", {}, {"top_p": 1}),
    ],
)
def test_wrong_inputs_are_refused(agents, adapter, protocol, version, params, model_params):  # noqa: F811
    with pytest.raises(agents.InputError):
        config(agents, adapter, protocol, version, params, **model_params)


def test_codex_refuses_a_model_id_it_would_shorten(agents):  # noqa: F811
    with pytest.raises(agents.InputError, match="'/'"):
        agents.agent_config(
            {
                "adapter": "codex",
                "model": {**model("openai"), "model_id": "org/model"},
                "params": {},
                "version": "1.2.3",
            }
        )


@pytest.mark.parametrize("adapter", ["opencode", "opencode-2", "codex", "claude-code"])
def test_the_agent_phase_reaches_the_model_host_only(runner_python, materials, tmp_path, adapter):  # noqa: F811
    spec = agent_spec(materials, tmp_path)
    harness = {
        **spec.effective_config["harness"],
        "agent": adapter,
        "params": {},
        "upstream_version": "1.2.3",
    }
    spec = replace(spec, effective_config={**spec.effective_config, "harness": harness})
    executor = make_executor(runner_python, materials, tmp_path)
    assert executor._network(spec) == {
        "agent": {"mode": "allowlist", "hosts": ["relay.example.test"]},
        "verifier": {"mode": "no-network"},
    }
    assert executor._agent(spec)["version"] == "1.2.3"


def test_opencode_runs_accepted_before_the_upstream_version_install_what_they_did(
    runner_python,  # noqa: F811
    materials,  # noqa: F811
    tmp_path,
):
    spec = agent_spec(materials, tmp_path)
    executor = make_executor(runner_python, materials, tmp_path)
    for params, version in (({"version": "1.9.0"}, "1.9.0"), ({}, "1.18.35")):
        harness = {**spec.effective_config["harness"], "agent": "opencode", "params": params}
        old = replace(spec, effective_config={**spec.effective_config, "harness": harness})
        agent = executor._agent(old)
        assert agent["version"] == version and agent["params"] == {}


DRIVER = """
import asyncio, json, sys, types
sys.path.insert(0, sys.argv[1])
from maestro_runner import agents
from harbor.agents.installed.opencode import OpenCode
from harbor.agents.installed.claude_code import ClaudeCode
from harbor.agents.installed.codex import Codex

adapter, outcome, key = sys.argv[2:5]
seen = {}

class Env:
    default_user = "agent"
    def __init__(self):
        self.files, self.commands = {}, []
    async def exec(self, command, user=None, **kw):
        self.commands.append((command, user))
        if command.startswith("rm -f"):
            self.files.pop(command.split()[-1].strip("'"), None)
        return types.SimpleNamespace(return_code=0, stdout="")
    async def upload_file(self, source, target):
        self.files[target] = open(source).read()

async def fake_run(self, instruction, environment, context):
    seen["during"] = dict(environment.files)
    if adapter == "codex":
        path = self._resolve_auth_json_path()
        seen["local"] = open(path).read()
        await environment.upload_file(path, "/tmp/codex-secrets/auth.json")
        seen["during"] = dict(environment.files)
    if outcome == "fail":
        raise RuntimeError("model said no")

agents.SECRETS["model_api_key"] = key
cls = {"opencode": agents.MaestroOpenCode, "opencode-2": agents.MaestroOpenCode2,
       "claude-code": agents.MaestroClaudeCode, "codex": agents.MaestroCodex}[adapter]
for base in cls.__mro__:
    if base.__name__ in ("OpenCode", "OpenCode2", "ClaudeCode", "Codex"):
        base.run = fake_run
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
@pytest.mark.parametrize("adapter", ["opencode", "opencode-2", "claude-code", "codex"])
def test_the_key_file_exists_only_while_the_agent_runs(tmp_path, adapter, outcome):
    if not RUNNER.exists():
        pytest.skip("Runner environment not installed")
    result = subprocess.run(
        [str(RUNNER), "-c", DRIVER, str(RUNNER_SRC), adapter, outcome, KEY],
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "TMPDIR": str(tmp_path)},
    )
    seen = json.loads(result.stdout.strip().splitlines()[-1])
    if adapter == "codex":
        assert json.loads(seen["during"]["/tmp/codex-secrets/auth.json"]) == {"OPENAI_API_KEY": KEY}
    else:
        assert seen["during"] == {"/run/maestro/model-key": KEY}
        owner = "chown agent /run/maestro/model-key && chmod 400 /run/maestro/model-key"
        assert (owner, "root") in [tuple(c) for c in seen["commands"]]
    assert seen["after"] == {}
    assert ("error" in seen) == (outcome == "fail")
    assert all(KEY not in command for command, _ in seen["commands"])
    assert list(tmp_path.iterdir()) == []  # the local copy is gone


OPENCODE_2 = """
import asyncio, json, sys, types
from pathlib import Path, PurePosixPath
sys.path.insert(0, sys.argv[1])
from maestro_runner import agents

cls = agents.MaestroOpenCode2
base = next(b for b in cls.__mro__ if b.__name__ == "OpenCode2")
agent = base(logs_dir=Path(sys.argv[2]), model_name="maestro/gpt-test", version="2.2.0",
             opencode_config={"provider": {"maestro": {"models": {"gpt-test": {}}}}})
calls = []

class Env:
    default_user = "agent"
    async def exec(self, command, user=None, env=None, **kw):
        calls.append({"command": command, "env": env or {}})
        return types.SimpleNamespace(return_code=0, stdout="", stderr="")

asyncio.run(base.run(agent, "do it", Env(), types.SimpleNamespace()))
print(json.dumps({"calls": calls, "version": agent.parse_version("opencode v2.2.0")}))
"""


def test_opencode_2_runs_standalone_without_updates_or_the_model_catalogue(tmp_path):
    if not RUNNER.exists():
        pytest.skip("Runner environment not installed")
    result = subprocess.run(
        [str(RUNNER), "-c", OPENCODE_2, str(RUNNER_SRC), str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "TMPDIR": str(tmp_path)},
    )
    seen = json.loads(result.stdout.strip().splitlines()[-1])
    assert seen["version"] == "2.2.0"
    config, run = seen["calls"]
    assert "~/.config/opencode/opencode.json" in config["command"]
    assert (
        "opencode run --standalone --auto --format json --thinking --model maestro/gpt-test"
        in (run["command"])
    )
    assert "--dangerously-skip-permissions" not in run["command"]
    assert run["env"]["OPENCODE_DISABLE_AUTOUPDATE"] == "1"
    assert run["env"]["OPENCODE_DISABLE_MODELS_FETCH"] == "1"


def show(tmp_path, name: str, chunks: list[str]) -> list[tuple[str, str]]:
    from test_fix_artifacts_runner import drive, logs

    out = tmp_path / "run"
    directory = out / "jobs" / "job" / "t1" / "agent"
    directory.mkdir(parents=True)
    stream = directory / name
    stream.write_text(chunks[0])
    steps = [["tail"]]
    for chunk in chunks[1:]:
        steps += [["append", str(stream), chunk], ["tail"]]
    return [(e["source"], e["text"]) for e in logs(drive(out, steps))]


def lines(*events) -> str:
    return "".join(json.dumps(e) + "\n" for e in events)


def test_the_opencode_stream_is_shown_as_readable_steps(tmp_path):
    first = lines(
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
    )
    rest = 'xt", "part": {"text": "Done."}}\n' + lines(
        {"type": "tool_use", "part": {"tool": "read", "state": {"input": {"path": "/app"}}}}
    )
    assert show(tmp_path, "opencode.txt", [first + '{"type": "te', rest]) == [
        (
            "agent/opencode",
            "--- step 1 · agent ---\nI will write the answer.\n$ echo ok > /app/answer\n",
        ),
        ("agent/opencode", 'Done.\n> read {"path": "/app"}\n'),
    ]


def test_the_codex_stream_is_shown_as_readable_steps(tmp_path):
    stream = "Reading prompt from stdin...\n" + lines(
        {"type": "thread.started", "thread_id": "t"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "reasoning", "text": "Plan it."}},
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "bash -lc 'echo ok > /app/answer'",
                "aggregated_output": "",
                "exit_code": 0,
            },
        },
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "cat /nope",
                "aggregated_output": "No such file",
                "exit_code": 1,
            },
        },
        {
            "type": "item.completed",
            "item": {"type": "file_change", "changes": [{"path": "/app/a", "kind": "add"}]},
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Done."}},
        {"type": "turn.completed", "usage": {"input_tokens": 10}},
    )
    assert show(tmp_path, "codex.txt", [stream]) == [
        (
            "agent/codex",
            (
                "--- step 1 · agent ---\n(thinking) Plan it.\n$ bash -lc 'echo ok > /app/answer'\n"
                "$ cat /nope\nNo such file\n(exit 1)\n> add /app/a\nDone.\n"
            ),
        )
    ]


def test_the_claude_code_stream_is_shown_as_readable_steps(tmp_path):
    stream = lines(
        {"type": "system", "subtype": "init", "model": "m"},
        {
            "type": "assistant",
            "message": {"id": "m1", "content": [{"type": "thinking", "thinking": "Plan it."}]},
        },
        {
            "type": "assistant",
            "message": {
                "id": "m1",
                "content": [
                    {"type": "text", "text": "Writing it."},
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "echo ok > /app/answer"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "content": [{"type": "text", "text": "done"}]}]
            },
        },
        {
            "type": "assistant",
            "message": {
                "id": "m2",
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/app/answer"}}
                ],
            },
        },
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": "nope", "is_error": True}]},
        },
        {"type": "result", "subtype": "success", "is_error": False},
    )
    assert show(tmp_path, "claude-code.txt", [stream]) == [
        (
            "agent/claude-code",
            (
                "--- step 1 · agent ---\n(thinking) Plan it.\nWriting it.\n"
                "$ echo ok > /app/answer\ndone\n--- step 2 · agent ---\n"
                '> Read {"file_path": "/app/answer"}\n! nope\n--- result: success ---\n'
            ),
        )
    ]


def test_a_trajectory_left_by_a_streaming_harness_is_not_shown_again(tmp_path):
    from test_fix_artifacts_runner import drive, logs

    out = tmp_path / "run"
    directory = out / "jobs" / "job" / "t1" / "agent"
    directory.mkdir(parents=True)
    (directory / "codex.txt").write_text(lines({"type": "turn.started"}))
    (directory / "trajectory.json").write_text(
        json.dumps({"steps": [{"step_id": 1, "source": "agent", "message": "again"}]})
    )
    shown = [e["source"] for e in logs(drive(out, [["tail"]]))]
    assert shown == ["agent/codex"]
