"""Backup sets on the same host (P0 subset) and the activity check that
updates use to refuse stopping in-progress work."""

import json
import sqlite3

import pytest

from maestro import backup
from maestro.core.errors import MaestroError
from maestro.core.lock import InstanceLock
from maestro.services import operations
from maestro.services.instance import instance_id


def _users(settings) -> list[str]:
    with sqlite3.connect(settings.database) as connection:
        return [row[0] for row in connection.execute("SELECT username FROM users ORDER BY 1")]


def _add_user(service, name):
    service.accounts.initialize_admin(
        service.accounts.issue_setup_token(), name, None, "pw-" * 4, "test"
    )


def test_backup_and_restore_round_trip(service, settings, tmp_path):
    _add_user(service, "before")
    instance = instance_id(service.db)
    (settings.data_dir / "attachments").mkdir()
    (settings.data_dir / "attachments" / "a.bin").write_bytes(b"x" * 10)
    config = tmp_path / "config.toml"
    config.write_text("# deployment config\n")
    target = tmp_path / "backups" / "one"
    target.parent.mkdir()

    manifest = backup.create(settings, target, config_file=config, include_files=True)

    assert manifest["schema_revision"] and manifest["instance_id"] == instance
    assert manifest["master_key"]["generation"] == 1
    assert manifest["files"]["attachments"] == {"files": 1, "bytes": 10, "included": True}
    assert (target / "config.toml").read_text() == "# deployment config\n"
    # The snapshot is one self-contained file, owner-only, and the key is not in the set.
    assert not list(target.glob("app.db-*"))
    assert all(p.stat().st_mode & 0o077 == 0 for p in (target / "app.db", target / "manifest.json"))
    assert not list(target.rglob("master-*.key"))
    assert json.loads((target / "manifest.json").read_text()) == manifest

    # Later changes are undone by a restore; the files present at restore stay aside.
    service.db.dispose()
    with sqlite3.connect(settings.database) as connection:
        connection.execute("DELETE FROM sessions")
        connection.execute("DELETE FROM users")
    (settings.data_dir / "attachments" / "new.bin").write_bytes(b"later")
    report = backup.restore(settings, target, include_files=True)

    assert _users(settings) == ["before"]
    assert report["master_key_present"] is True
    assert sorted(p.name for p in (settings.data_dir / "attachments").iterdir()) == ["a.bin"]
    (aside,) = report["previous_files_kept_at"].values()
    assert sorted(p.name for p in (settings.data_dir / aside).iterdir()) == ["a.bin", "new.bin"]


def test_restore_refuses_while_the_service_holds_the_lock(service, settings, tmp_path):
    backup.create(settings, tmp_path / "set")
    with InstanceLock(settings.lock_file), pytest.raises(MaestroError) as error:
        backup.restore(settings, tmp_path / "set")
    assert error.value.code == "INSTANCE_LOCKED"


def test_restore_checks_the_set_before_touching_anything(service, settings, tmp_path):
    good = tmp_path / "good"
    backup.create(settings, good)
    before = settings.database.read_bytes()

    incomplete = tmp_path / "incomplete"
    backup.create(settings, incomplete)
    (incomplete / "manifest.json").unlink()
    tampered = tmp_path / "tampered"
    backup.create(settings, tampered)
    with sqlite3.connect(tampered / "app.db") as connection:
        connection.execute("DELETE FROM users")
    unknown = tmp_path / "unknown"
    backup.create(settings, unknown)
    manifest = json.loads((unknown / "manifest.json").read_text())
    (unknown / "manifest.json").write_text(json.dumps(manifest | {"schema_revision": "9999"}))

    for source, code in (
        (incomplete, "BACKUP_INVALID"),
        (tampered, "BACKUP_INVALID"),
        (unknown, "BACKUP_INVALID"),
    ):
        with pytest.raises(MaestroError) as error:
            backup.restore(settings, source)
        assert error.value.code == code
    with pytest.raises(MaestroError) as error:
        backup.restore(settings, good, include_files=True)  # nothing was copied
    assert error.value.code == "BACKUP_INVALID"
    assert settings.database.read_bytes() == before


def test_a_missing_master_key_is_named_not_glossed_over(service, settings, tmp_path):
    backup.create(settings, tmp_path / "set")
    service.db.dispose()
    (key,) = settings.master_key_dir.iterdir()
    key.rename(tmp_path / key.name)
    with pytest.raises(MaestroError) as error:
        backup.restore(settings, tmp_path / "set")
    assert error.value.code == "MASTER_KEY_MISSING"
    assert "generation 1" in error.value.message
    report = backup.restore(settings, tmp_path / "set", allow_missing_key=True)
    assert report["master_key_present"] is False


def test_activity_counts_only_work_a_stop_would_interrupt(service, settings):
    assert backup.activity(settings) == {
        "active_runs": 0,
        "queued_runs": 0,
        "active_operations": {},
        "busy": False,
    }
    with service.db.write() as conn:
        operations.create(conn, "export", owner_id=None)  # rebuilt after a restart
        op = operations.create(conn, "dataset_import", owner_id=None)
    assert backup.activity(settings)["busy"] is True
    assert backup.activity(settings)["active_operations"] == {"dataset_import": 1}
    with service.db.write() as conn:
        conn.exec_driver_sql("UPDATE operations SET status='succeeded' WHERE id=?", (op,))
    assert backup.activity(settings)["busy"] is False
