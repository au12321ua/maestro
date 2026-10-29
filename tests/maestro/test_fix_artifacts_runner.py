"""Runner evidence handling under a hostile container (C-07 / U-16).

The trial's ``agent/`` and ``verifier/`` directories are mounted writable into the
container, so a process in it can plant links and huge files there. The Runner must
never follow a link to read host files, and its per-attempt limits must hold while it
copies, not after. The Runner module redirects fd 1 on import, so each scenario runs
in its own interpreter and the test reads the protocol lines it prints.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

RUNNER_SRC = Path(__file__).resolve().parents[2] / "runner" / "src"

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


@pytest.fixture
def trial(tmp_path):
    """An output directory with one live trial and a secret file outside of it."""
    out = tmp_path / "run"
    directory = out / "jobs" / "job" / "t1"
    for sub in ("agent", "verifier"):
        (directory / sub).mkdir(parents=True)
    secret = tmp_path / "host-secret.key"
    secret.write_text("TOP-SECRET-MASTER-KEY-MATERIAL\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "stolen.log").write_text("TOP-SECRET-IN-A-LINKED-DIRECTORY\n")
    return out, directory, secret, outside


def logs(events: list[dict]) -> list[dict]:
    return [e for e in events if e["_type"] == "log"]


def test_tail_never_follows_a_symlinked_log_file(trial):
    out, directory, secret, _ = trial
    (directory / "agent" / "leak.log").symlink_to(secret)
    (directory / "agent" / "real.log").write_text("honest output\n")
    events = drive(out, [["tail"]])
    assert [e["source"] for e in logs(events)] == ["agent/real.log"]
    assert "TOP-SECRET" not in json.dumps(events)


def test_tail_never_enters_a_symlinked_directory(trial):
    out, directory, _, outside = trial
    (directory / "agent" / "linked").symlink_to(outside)
    (directory / "verifier").rmdir()
    (directory / "verifier").symlink_to(outside)  # even the log directory itself
    events = drive(out, [["tail"]])
    assert logs(events) == []
    assert "TOP-SECRET" not in json.dumps(events)


def test_tail_streams_normal_logs_and_continues_from_the_offset(trial):
    out, directory, _, _ = trial
    log = directory / "agent" / "nested" / "run.txt"
    log.parent.mkdir()
    log.write_text("first\n")
    (directory / "verifier" / "test-stdout.txt").write_text("verifying\n")
    (directory / "agent" / "ignored.json").write_text("{}")
    events = drive(out, [["tail"], ["append", str(log), "second\n"], ["tail"]])
    texts = [(e["source"], e["text"]) for e in logs(events)]
    assert texts == [
        ("agent/nested/run.txt", "first\n"),
        ("verifier/test-stdout.txt", "verifying\n"),
        ("agent/nested/run.txt", "second\n"),
    ]


def test_opening_refuses_links_and_paths_that_resolve_outside_the_root(trial):
    out, directory, secret, outside = trial
    root = directory / "agent"
    (root / "dir-link").symlink_to(outside)
    (root / "file-link.log").symlink_to(secret)
    (root / "plain.log").write_text("fine")
    os.mkfifo(root / "pipe.log")
    probes = drive(
        out,
        [
            ["open_below", str(root / "plain.log"), str(root)],
            ["open_below", str(root / "file-link.log"), str(root)],
            # A directory swapped for a link between listing and opening.
            ["open_below", str(root / "dir-link" / "stolen.log"), str(root)],
            ["open_below", str(root / "pipe.log"), str(root)],
            ["open_below", str(root / "missing.log"), str(root)],
        ],
    )
    assert [p["opened"] for p in probes] == [True, False, False, False, False]
    assert probes[0]["text"] == "fine"


def test_archive_copies_no_links_and_lists_them_as_skipped(trial):
    out, directory, secret, outside = trial
    (directory / "agent" / "trajectory.json").write_text("{}")
    (directory / "verifier" / "reward.txt").write_text("1\n")
    (directory / "result.json").write_text('{"ok": true}')
    (directory / "agent" / "leak.log").symlink_to(secret)
    (directory / "artifacts").symlink_to(outside)
    os.mkfifo(directory / "agent" / "pipe")  # copying a FIFO would block forever
    (finished,) = [e for e in drive(out, [["finish"]]) if e["_type"] == "attempt_finished"]
    archive = out / finished["archive"]
    copied = sorted(p.relative_to(archive).as_posix() for p in archive.rglob("*") if p.is_file())
    assert copied == ["agent/trajectory.json", "result.json", "verifier/reward.txt"]
    assert not any(p.is_symlink() for p in archive.rglob("*"))
    assert "TOP-SECRET" not in "".join(p.read_text() for p in archive.rglob("*") if p.is_file())
    by_path = {f["path"]: f for f in finished["files"]}
    assert set(by_path) == {*copied, "agent/leak.log", "artifacts"}
    assert by_path["agent/leak.log"]["skipped"] and by_path["artifacts"]["skipped"]
    assert all("sha256" in by_path[p] and not by_path[p].get("skipped") for p in copied)


def test_archive_enforces_size_count_and_total_limits_while_copying(trial):
    out, directory, _, _ = trial
    for name, size in (
        ("a.bin", 10),
        ("b.bin", 10),
        ("big.bin", 500),
        ("c.bin", 10),
        ("d.bin", 10),
    ):
        (directory / "agent" / name).write_bytes(b"x" * size)
    limits = {"max_files": 3, "max_file_bytes": 100, "max_total_bytes": 1000}
    (finished,) = [
        e for e in drive(out, [["finish"]], limits=limits) if e["_type"] == "attempt_finished"
    ]
    archive = out / finished["archive"]
    on_disk = sorted(p.name for p in archive.rglob("*") if p.is_file())
    assert on_disk == ["a.bin", "b.bin", "c.bin"]  # big.bin too large, d.bin over the count
    skipped = {f["path"]: f.get("reason") for f in finished["files"] if f.get("skipped")}
    assert skipped == {"agent/big.bin": "TOO_LARGE", "agent/d.bin": "TOO_MANY_FILES"}
    assert next(f for f in finished["files"] if f["path"] == "agent/big.bin")["size"] == 500

    out2 = out.parent / "run2"
    (out2 / "jobs" / "job" / "t1" / "agent").mkdir(parents=True)
    for name in ("a.bin", "b.bin", "c.bin"):
        (out2 / "jobs" / "job" / "t1" / "agent" / name).write_bytes(b"y" * 80)
    limits = {"max_files": 500, "max_file_bytes": 100, "max_total_bytes": 200}
    (finished,) = [
        e for e in drive(out2, [["finish"]], limits=limits) if e["_type"] == "attempt_finished"
    ]
    archive = out2 / finished["archive"]
    assert sorted(p.name for p in archive.rglob("*") if p.is_file()) == ["a.bin", "b.bin"]
    assert [(f["path"], f.get("reason")) for f in finished["files"] if f.get("skipped")] == [
        ("agent/c.bin", "TOTAL_LIMIT")
    ]


def test_a_huge_number_of_skipped_entries_stays_a_small_manifest(trial):
    out, directory, _, _ = trial
    for index in range(30):
        (directory / "agent" / f"f{index:02}.bin").write_bytes(b"z")
    (finished,) = [
        e
        for e in drive(
            out,
            [["finish"]],
            limits={"max_files": 2},
            constants={"MAX_LISTED_SKIPS": 5},
        )
        if e["_type"] == "attempt_finished"
    ]
    kept = [f for f in finished["files"] if not f.get("skipped")]
    listed = [f for f in finished["files"] if f.get("skipped")]
    assert len(kept) == 2 and len(listed) == 5
    assert finished["skipped_unlisted"] == 30 - 2 - 5


SPEC_DRIVER = """
import json, sys
sys.path.insert(0, sys.argv[1])
from maestro_runner import job

base = json.loads(sys.argv[2])
outcomes = []
for limits in json.loads(sys.argv[3]):
    spec = dict(base)
    if limits != "MISSING":
        spec["artifact_limits"] = limits
    try:
        job.check_spec(spec)
        outcomes.append("ok")
    except job.Rejected as error:
        outcomes.append(error.code)
job.emit("probe", outcomes=outcomes)
"""

GOOD = {"max_files": 500, "max_file_bytes": 64 << 20, "max_total_bytes": 1 << 30}


def test_the_runner_insists_on_sane_artifact_limits():
    base = {
        "protocol_version": 2,
        "command": "execute",
        "run_id": "run_x",
        "output_dir": "/tmp/x",
        "agent": {"name": "oracle"},
        "tasks": [],
        "labels": {},
        "retry": {},
        "n_concurrent_trials": 1,
        "log_cap_bytes": 1,
        "heartbeat_sec": 1,
        "tail_interval_sec": 1,
    }
    variants = [
        GOOD,
        "MISSING",
        {**GOOD, "max_files": 0},
        {**GOOD, "max_file_bytes": -1},
        {**GOOD, "max_total_bytes": True},
        {**GOOD, "max_files": "500"},
        {"max_files": 5, "max_file_bytes": 5},
        {**GOOD, "max_file_bytes": 2 << 30},  # one file may not exceed the whole attempt
    ]
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            SPEC_DRIVER,
            str(RUNNER_SRC),
            json.dumps(base),
            json.dumps(variants),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    (probe,) = [json.loads(line)["payload"] for line in result.stdout.splitlines()]
    assert probe["outcomes"] == ["ok"] + ["RUNNER_INPUT_INVALID"] * 7
