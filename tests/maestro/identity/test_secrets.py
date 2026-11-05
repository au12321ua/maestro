import os

import pytest
from sqlalchemy import select, update

from maestro.core.errors import MaestroError
from maestro.db import schema as t
from maestro.domain.states import Role
from maestro.security import master_key, passwords, tokens
from maestro.security.master_key import MasterKeyUnavailable
from maestro.security.redaction import redact
from maestro.services.principal import Channel, Principal

ADMIN = Principal("usr_x", "admin", Role.ADMIN, Channel.WEB_SESSION, session_id="ses_x")


def test_tokens_are_prefixed_hashed_and_redactable():
    token = tokens.generate("pat")
    assert tokens.well_formed("pat", token)
    assert not tokens.well_formed("session", token)
    assert tokens.digest("pat", token) != tokens.digest("session", token)
    assert token not in tokens.digest("pat", token)
    assert redact(f"use {token} now") == "use [REDACTED] now"


def test_redaction_of_known_values_headers_and_urls():
    text = "Authorization: Bearer abc.def key=sk-ABCDEFGHIJKLMNOPQRST https://x/?api_key=zz9&a=1"
    out = redact(text, ["my-secret-value"])
    assert "abc.def" not in out and "sk-ABC" not in out and "zz9" not in out
    assert redact("x my-secret-value y", ["my-secret-value"]) == "x [REDACTED] y"


def test_password_policy_and_verify():
    with pytest.raises(MaestroError):
        passwords.hash_password("short")
    stored = passwords.hash_password("long enough password")
    assert stored.startswith("$argon2id$")
    assert passwords.verify(stored, "long enough password")
    assert not passwords.verify(stored, "wrong password!!")
    assert not passwords.verify(None, "long enough password")


def _create_credential(service, value="sk-test-value-123"):
    from maestro.services.principal import Channel

    with service.db.write() as conn:
        conn.execute(
            t.users.insert().values(
                id="usr_x",
                username="admin",
                display_name="a",
                role="admin",
                status="active",
                password_hash="x",
                revision=1,
                created_at="t",
                updated_at="t",
            )
        )
    assert Channel.WEB_SESSION
    return service.credentials.create(ADMIN, "openai-main", "model_api_key", "openai", "", value)


def test_credentials_are_encrypted_bound_and_never_returned(service):
    created = _create_credential(service)
    assert "value" not in created and created["revision"] == 1 and created["key_version"] == 1
    with service.db.read() as conn:
        row = conn.execute(select(t.credentials)).mappings().one()
    assert b"sk-test-value-123" not in row["encrypted_payload"]
    assert service.credentials.resolve_value(created["id"]) == ("sk-test-value-123", 1)
    # Associated data binds id and revision: a bumped revision cannot decrypt old bytes.
    with service.db.write() as conn:
        conn.execute(update(t.credentials).values(revision=2))
    with pytest.raises(MaestroError) as error:
        service.credentials.resolve_value(created["id"])
    assert error.value.code == "CREDENTIAL_UNAVAILABLE"


def test_replace_value_creates_new_revision(service):
    created = _create_credential(service)
    replaced = service.credentials.replace_value(ADMIN, created["id"], "new-value-xyz", 1)
    assert replaced["revision"] == 2
    assert service.credentials.resolve_value(created["id"]) == ("new-value-xyz", 2)
    with pytest.raises(MaestroError) as error:
        service.credentials.replace_value(ADMIN, created["id"], "again", 1)
    assert error.value.code == "REVISION_CONFLICT"


def test_master_key_rotation_reencrypts_and_keeps_old_files(service, settings):
    created = _create_credential(service)
    before = sorted(os.listdir(settings.master_key_dir))
    result = service.credentials.rotate_master(ADMIN, 1)
    assert result["generation"] == 2 and result["reencrypted_credentials"] == 1
    after = sorted(os.listdir(settings.master_key_dir))
    assert set(before) < set(after) and len(after) == 2
    assert service.credentials.resolve_value(created["id"])[0] == "sk-test-value-123"
    with pytest.raises(MaestroError) as error:
        service.credentials.rotate_master(ADMIN, 1)
    assert error.value.code == "MASTER_KEY_GENERATION_CONFLICT"
    with service.db.read() as conn:
        audit = conn.execute(
            select(t.audit_events.c.action).where(t.audit_events.c.action == "master_key.rotate")
        ).all()
    assert len(audit) == 1


def test_failed_rotation_removes_new_key_and_keeps_generation(service, settings, monkeypatch):
    _create_credential(service)
    calls = []

    def broken(*args):
        calls.append(args)
        raise MasterKeyUnavailable("boom")

    monkeypatch.setattr(master_key, "decrypt", broken)
    with pytest.raises(MasterKeyUnavailable):
        service.credentials.rotate_master(ADMIN, 1)
    assert calls
    assert len(os.listdir(settings.master_key_dir)) == 1
    assert service.keys.status()["generation"] == 1


def test_master_key_file_checks(service, settings):
    (name,) = os.listdir(settings.master_key_dir)
    path = settings.master_key_dir / name
    assert service.keys.status()["available"]
    path.chmod(0o644)
    assert not service.keys.status()["available"]
    path.chmod(0o600)
    path.write_bytes(os.urandom(32))  # wrong key: check value mismatch
    assert not service.keys.status()["available"]


def test_master_key_dir_problem_is_named(service, settings):
    settings.master_key_dir.chmod(0o775)
    with pytest.raises(MasterKeyUnavailable) as error:
        service.keys._open_directory()
    assert "0775" in error.value.message
    settings.master_key_dir.chmod(0o700)


def test_master_key_dir_must_be_outside_data(tmp_path):
    from maestro.config import Settings

    with pytest.raises(MaestroError):
        Settings(
            data_dir=tmp_path / "d", cache_dir=tmp_path / "c", master_key_dir=tmp_path / "d" / "k"
        ).check()


def test_initialize_is_idempotent(service, settings):
    assert service.keys.initialize() is False
    assert len(os.listdir(settings.master_key_dir)) == 1
