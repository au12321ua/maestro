"""Locate commands in saved harness streams without interpreting their outcomes."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any

STREAMS = {"agent/codex.txt", "agent/claude-code.txt", "agent/opencode.txt"}


def is_trace(name: str) -> bool:
    path = PurePosixPath(name)
    return name in STREAMS or (
        "agent" in path.parts and path.suffix in {".json", ".jsonl", ".txt", ".log", ".cast"}
    )


def index_trace(path: Path, name: str) -> dict[str, Any]:
    """JSONL positions are one-based lines with JSON pointers into each record.

    Command text is the recorded tool input, never split into inferred shell commands.
    ATIF offsets are zero-based JSON pointers; a batch may share terminal output.
    No exit code is inferred from prose or an error flag.
    """
    if name not in STREAMS and name != "agent/trajectory.json":
        return {"status": "not indexed", "reason": "unsupported trace format", "events": []}
    events, records = [], []
    try:
        if name == "agent/trajectory.json":
            return _atif(json.loads(path.read_text(encoding="utf-8")))
        for line, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip() or (
                name == "agent/codex.txt" and raw == "Reading prompt from stdin..."
            ):
                continue
            event = json.loads(raw)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise TypeError("Expected typed event record")
            records.append((line, event))
        for line, event in records:
            if name == "agent/codex.txt":
                item = event.get("item", {})
                if event["type"] == "item.completed" and item.get("type") == "command_execution":
                    command = item.get("command")
                    if not isinstance(command, str):
                        raise ValueError("Missing command text")
                    events.append(
                        _entry(
                            line,
                            "/item/command",
                            command,
                            item.get("exit_code"),
                            {"line": line, "pointer": "/item/aggregated_output"}
                            if "aggregated_output" in item
                            else None,
                        )
                    )
            elif name == "agent/opencode.txt":
                part = event.get("part", {})
                state = part.get("state", {})
                if event["type"] == "tool_use" and part.get("tool") == "bash":
                    command = state.get("input", {}).get("command")
                    if not isinstance(command, str):
                        raise ValueError("Missing bash command text")
                    # Older streams omit status but emit tool_use only with its result.
                    if state.get("status") not in (None, "completed", "error"):
                        continue
                    code = state.get("metadata", {}).get("exit")
                    events.append(
                        _entry(
                            line,
                            "/part/state/input/command",
                            command,
                            code,
                            {"line": line, "pointer": "/part/state/output"}
                            if "output" in state
                            else None,
                        )
                    )
            else:
                content = event.get("message", {}).get("content", [])
                if event["type"] != "assistant" or not isinstance(content, list):
                    continue
                for offset, block in enumerate(content):
                    if block.get("type") != "tool_use" or block.get("name") != "Bash":
                        continue
                    command = block.get("input", {}).get("command")
                    if not isinstance(command, str):
                        raise TypeError("Missing Bash command text")
                    call_id = block.get("id")
                    results = [
                        (result_line, i, value)
                        for result_line, result in records
                        if result["type"] == "user"
                        and isinstance(result.get("message", {}).get("content"), list)
                        for i, value in enumerate(result["message"]["content"])
                        if value.get("type") == "tool_result"
                        and call_id is not None
                        and value.get("tool_use_id") == call_id
                    ]
                    if results:
                        outputs = [
                            {"line": result_line, "pointer": f"/message/content/{i}/content"}
                            for result_line, i, value in results
                        ]
                        codes = [
                            value["exit_code"] for _, _, value in results if "exit_code" in value
                        ]
                        events.append(
                            _entry(
                                line,
                                f"/message/content/{offset}/input/command",
                                command,
                                codes[0] if len(codes) == 1 else None,
                                outputs,
                            )
                        )
                    else:
                        raise ValueError("Bash call has no recorded result")
        return {"status": "indexed", "reason": None, "events": events}
    except (ValueError, UnicodeError, AttributeError, TypeError):
        # Do not offer a partial index as if all commands had been accounted for.
        return {
            "status": "not indexed",
            "reason": "malformed or unsupported event schema",
            "events": [],
        }


def _entry(line: int, pointer: str, command: str, code: Any, output: Any) -> dict[str, Any]:
    return {
        "line": line,
        "pointer": pointer,
        "command": command,
        "exit_code": code if isinstance(code, int) and not isinstance(code, bool) else None,
        "output": output,
    }


def _atif(trace: dict[str, Any]) -> dict[str, Any]:
    steps = trace.get("steps")
    if not isinstance(steps, list):
        raise TypeError("Expected ATIF steps")
    events = []
    for offset, step in enumerate(steps):
        results = (step.get("observation") or {}).get("results") or []
        for index, call in enumerate(step.get("tool_calls") or []):
            if call.get("function_name") != "bash_command":
                if call.get("function_name") == "task_complete":
                    continue
                raise ValueError("Unsupported ATIF tool")
            command = call.get("arguments", {}).get("keystrokes")
            if not isinstance(command, str):
                raise TypeError("Expected terminal input")
            outputs = [
                {
                    "pointer": f"/steps/{offset}/observation/results/{i}/content",
                    "shared": result.get("source_call_id") is None,
                }
                for i, result in enumerate(results)
                if result.get("source_call_id") is None
                or result.get("source_call_id") == call.get("tool_call_id")
            ]
            if outputs:
                events.append(
                    {
                        "record_offset": offset,
                        "pointer": f"/steps/{offset}/tool_calls/{index}/arguments/keystrokes",
                        "command": command,
                        "input_kind": "terminal_keystrokes",
                        "exit_code": None,
                        "output": outputs,
                    }
                )
        if step.get("source") == "agent" and not step.get("tool_calls") and results:
            raise ValueError("ATIF raw content has no structured commands")
    return {"status": "indexed", "reason": None, "events": events}
