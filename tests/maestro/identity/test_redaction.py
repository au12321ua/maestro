"""Redaction patterns, the known-secret half at the Hub call sites,
text-file redaction of collected artifacts, and live Runner logs redacted before storage."""

import asyncio
import json
import logging
import time
import uuid

import pytest
from sqlalchemy import select

from maestro.boundaries.dataset_source import SourceError
from maestro.db import schema as t
from maestro.domain.states import Role
from maestro.execution import harbor
from maestro.execution import hub as hub_module
from maestro.execution.hub import HubSource
from maestro.security.redaction import REDACTED, redact, redact_json
from maestro.services.principal import Channel, Principal
from maestro.services.worker import RunSink

# (text, fragments that must be gone, fragments that must stay)
SECRETS = {
    "json api_key": ('{"api_key": "abc123SECRETxyz789"}', ["abc123SECRET"], ['"api_key"']),
    "json authorization": (
        '{"Authorization": "Bearer abc123SECRETxyz789"}',
        ["abc123SECRET"],
        ['"Authorization"'],
    ),
    "python repr": ("{'api_key': 'abc123SECRETxyz789'}", ["abc123SECRET"], ["'api_key'"]),
    "python repr bearer": (
        "{'Authorization': 'Bearer abc123SECRETxyz789'}",
        ["abc123SECRET"],
        ["'Authorization'"],
    ),
    "escaped json in a log line": (
        '{"line": "{\\"api_key\\": \\"abc123SECRETxyz789\\"}"}',
        ["abc123SECRET"],
        ["api_key"],
    ),
    "quoted env": ('export API_KEY="abc123SECRETxyz789"', ["abc123SECRET"], ["export API_KEY="]),
    "single quoted env": ("OPENAI_API_KEY='abc123SECRETxyz789'", ["abc123SECRET"], ["OPENAI_API"]),
    "quoted value with spaces": ('password = "hunter2 hunter2"', ["hunter2"], ["password"]),
    "unquoted env": ("HF_TOKEN=abcdefghij0123456789ZZ", ["abcdefghij0123456789ZZ"], ["HF_TOKEN="]),
    "x-api-key header": ("x-api-key: abc123SECRETxyz789", ["abc123SECRET"], ["x-api-key"]),
    "aws secret": (
        "aws_secret_access_key=wJalrXUtnFEMI/K7MDENGbPxRfiCYEXAMPLEKEY",
        ["wJalrXUtnFEMI"],
        ["aws_secret_access_key"],
    ),
    "hf token": (
        "used hf_abcdefghijklmnopqrstuvwxyzABCDEFGH here",
        ["hf_abcdef"],
        ["used", "here"],
    ),
    "github token": ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", ["ghp_abcdef"], []),
    "github fine grained": (
        "github_pat_11ABCDEFG0abcdefghijkl_mnopqrstuvwxyz0123456789ABCDEFGHIJ",
        ["github_pat_11ABC"],
        [],
    ),
    "aws access key id": ("key AKIAIOSFODNN7EXAMPLE end", ["AKIAIOSFODNN7"], ["key", "end"]),
    "url userinfo": (
        "git clone https://user:SECRETPASS123@host.example/repo.git",
        ["SECRETPASS123"],
        ["https://user:", "@host.example/repo.git"],
    ),
    "bare bearer": (
        "got Bearer abc123SECRETxyz789 from env",
        ["abc123SECRET"],
        ["got", "from env"],
    ),
    "curl header": (
        "curl -H 'Authorization: Bearer abc123SECRETxyz789' https://x",
        ["abc123SECRET"],
        ["curl -H", "https://x"],
    ),
    "openai key": ("sk-abcdefghijklmnopqrstuvwx", ["sk-abcdef"], []),
    "anthropic key": ("sk-ant-api03-abcdefghijklmnopqrstuvwxyz", ["sk-ant", "abcdefghij"], []),
    "maestro pat": ("mst_pat_" + "A" * 43, ["AAAAAAAAAA"], []),
    "query key": (
        "GET /v1/models?key=AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ1234567&a=1",
        ["AIzaSy"],
        ["&a=1"],
    ),
}


@pytest.mark.parametrize("name", sorted(SECRETS))
def test_known_secret_shapes_are_redacted(name):
    text, gone, kept = SECRETS[name]
    out = redact(text)
    assert REDACTED in out, out
    for fragment in gone:
        assert fragment not in out, out
    for fragment in kept:
        assert fragment in out, out


PROSE = [
    "The model used 1200 max_tokens and a token budget of 5.",
    "tokens: 5000 prompt, 300 completion; num_tokens=17",
    "Token: ok",
    "The bearer of bad news arrived.",
    "Authorization is required for this endpoint.",
    "The secretary: Bob joined; password policy requires 10 characters.",
    "See https://example.com/docs?page=2 and http://localhost:8080/path for details.",
    "Contact me@example.com or visit ssh://git@github.com/org/repo.git",
    "api key rotation happens monthly; the key is stored elsewhere",
    "Ran 14 tests in 0.31s\nAll good: sha256 of file is 9f86d081884c7d659a2feaa0c55ad015",
    "def verify(stored, password):\n    return hasher.verify(stored, password)",
    "hf_ is a prefix, ghp_ too; AKIA alone is not a key",
    '{"reward": 0.5, "steps": [1, 2, 3], "name": "task-01"}',
]


@pytest.mark.parametrize("text", PROSE)
def test_normal_text_is_left_alone(text):
    assert redact(text) == text


def test_known_values_are_removed_in_every_encoding():
    secret = "my-very-secret-value-0123"
    assert redact(f"x {secret} y", [secret]) == f"x {REDACTED} y"
    assert secret not in redact(json.dumps({"k": secret}), [secret])
    assert redact_json({"a": [f"v {secret}"]}, [secret]) == {"a": [f"v {REDACTED}"]}


def test_redaction_stays_linear_on_hostile_input():
    hostile = [
        "a" * 2_000_000,
        "api_key=" * 100_000,
        "api_key" + " " * 500_000 + "=",
        "Bearer " * 100_000,
        '"' * 300_000,
        "password=" + '"' * 1 + "x" * 500_000,
        "://" * 100_000,
        "http://" + "a:" * 200_000 + "@",
        ("k" * 50 + " ") * 40_000,
        "Authorization:" + " " * 500_000,
    ]
    started = time.monotonic()
    for text in hostile:
        redact(text)
    assert time.monotonic() - started < 15


# --- Hub call sites ---------------------------------------------------------------


class FakeProcess:
    pid = 0

    def __init__(self, out: bytes, err: bytes, code: int):
        self.out, self.err, self.returncode = out, err, code

    async def communicate(self, _request):
        return self.out, self.err


def hub_answering(monkeypatch, tmp_path, payload=None, err=b""):
    out = b""
    if payload is not None:
        event = {"type": "result", "protocol_version": hub_module.PROTOCOL_VERSION}
        out = json.dumps({**event, "payload": payload}).encode() + b"\n"

    async def spawn(*args, **kwargs):
        return FakeProcess(out, err, 0 if payload is not None else 1)

    monkeypatch.setattr(hub_module.asyncio, "create_subprocess_exec", spawn)
    return HubSource(tmp_path / "python", tmp_path / "work")


def test_hub_errors_never_carry_the_credential_that_was_used(monkeypatch, tmp_path):
    value = "plain-hub-credential-0001"
    hub = hub_answering(
        monkeypatch,
        tmp_path,
        {
            "status": "error",
            "kind": "auth",
            "message": f"401 for token {value}",
            "evidence": {"url": f"https://hub/x?t={value}", "n": 3},
        },
    )
    with pytest.raises(SourceError) as error:
        asyncio.run(hub.search("q", value))
    assert value not in error.value.message and value not in json.dumps(error.value.evidence)
    assert error.value.evidence["n"] == 3


def test_hub_stderr_in_the_log_never_carries_the_credential(monkeypatch, tmp_path, caplog):
    value = "plain-hub-credential-0002"
    hub = hub_answering(monkeypatch, tmp_path, None, err=f"boom while using {value}".encode())
    with caplog.at_level(logging.WARNING, logger=hub_module.log.name), pytest.raises(SourceError):
        asyncio.run(hub.search("q", value))
    assert "boom while using" in caplog.text and value not in caplog.text


# --- collected artifacts ----------------------------------------------------------


LIMIT = 1 << 20  # the per-file artifact limit a test executor is configured with


def test_any_text_is_redacted_whatever_the_file_is_called():
    # Names like trajectory.out, reward or out.csv were skipped by the old suffix list.
    data = b"line one\nOPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx\nthe end\n"
    out = harbor._redacted(data, LIMIT)
    assert b"sk-abcdef" not in out and b"line one\n" in out and b"the end\n" in out


def test_text_with_stray_bytes_is_redacted_and_the_stray_bytes_survive():
    # A stderr tail is cut at byte offsets: the cut may split a multi-byte character.
    data = "caf\u00e9 sk-abcdefghijklmnopqrstuvwx ok".encode()[:-1] + b" \xe2\x82"
    out = harbor._redacted(data, LIMIT)
    assert b"sk-abcdef" not in out and out.endswith(b" \xe2\x82") and "caf\u00e9".encode() in out


def test_binary_and_oversized_files_stay_untouched():
    secret = b"sk-abcdefghijklmnopqrstuvwx"
    with_nul = b"\x89PNG\r\n\x00\x00" + secret
    assert harbor._redacted(with_nul, LIMIT) == with_nul
    big = secret + b"\n" + b"x" * LIMIT
    assert harbor._redacted(big, LIMIT) == big


def test_deliverables_are_checked_only_for_this_runs_known_secrets():
    # Deliverables are stored byte-for-byte, so generic secret shapes stay; a value the
    # run was given (a model key) makes the file refused instead of stored.
    step = b"ISO-10303-21;\nDATA;\n#1=CARTESIAN_POINT('sk-abcdefghijklmnopqrstuvwx');\n"
    assert not harbor._holds_secret(step, ())
    assert not harbor._holds_secret(step, ("", "other-key"))
    assert harbor._holds_secret(step + b"real-model-key", ("real-model-key",))


# --- live Runner logs -------------------------------------------------------------


def test_live_runner_log_text_is_redacted_before_it_is_stored(service, version_id):
    with service.db.write() as conn:
        conn.execute(
            t.users.insert().values(
                id="usr_owner",
                username="owner",
                display_name="o",
                role="member",
                status="active",
                password_hash="x",
                revision=1,
                created_at="t",
                updated_at="t",
            )
        )
    owner = Principal("usr_owner", "owner", Role.MEMBER, Channel.WEB_SESSION, session_id="s")
    service.runs.submit(
        owner,
        uuid.uuid4().hex,
        {"name": "r", "dataset_version_id": version_id, "execution_mode": "oracle"},
    )
    service.worker.start_epoch()
    run_id, lease, spec = service.worker._claim()
    sink = RunSink(service.worker, run_id, lease)
    trial_id = spec.trials[0].trial_id

    async def stream():
        await sink.attempt_started(trial_id, 1)
        await sink.log(
            trial_id, 1, "agent/stdout.log", "starting OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx\n"
        )
        await sink.log(
            trial_id, 1, "agent/stdout.log", "Authorization: Bearer abc123SECRETxyz789\n"
        )

    asyncio.run(stream())
    with service.db.read() as conn:
        key = conn.execute(
            select(t.artifacts.c.storage_key).where(
                t.artifacts.c.relative_path == "agent/stdout.log"
            )
        ).scalar_one()
    stored = service.worker.store.get(key).decode()
    assert "starting" in stored and stored.count(REDACTED) == 2
    assert "sk-abcdef" not in stored and "abc123SECRET" not in stored
