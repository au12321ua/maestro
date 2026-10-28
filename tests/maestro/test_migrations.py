import json

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect

from maestro.db import migrate
from maestro.db.engine import Database
from maestro.db.schema import metadata


def test_empty_instance_migrates_to_head_and_matches_schema(tmp_path):
    db = Database.open(tmp_path / "app.db")
    assert migrate.current(db) is None
    migrate.upgrade(db)
    assert migrate.current(db) == migrate.head()
    with db.engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), metadata)
        assert diff == [], diff
        names = set(inspect(connection).get_table_names())
    assert {"users", "pats", "runs", "trials", "operations", "audit_events"} <= names
    migrate.upgrade(db)  # idempotent
    assert migrate.current(db) == migrate.head()


def test_pragmas_wal_and_foreign_keys(tmp_path):
    db = Database.open(tmp_path / "app.db")
    migrate.upgrade(db)
    with db.read() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000


def test_foreign_keys_enforced(tmp_path):
    import pytest
    from sqlalchemy import insert
    from sqlalchemy.exc import IntegrityError

    from maestro.db import schema as t

    db = Database.open(tmp_path / "app.db")
    migrate.upgrade(db)
    with pytest.raises(IntegrityError), db.write() as conn:
        conn.execute(
            insert(t.sessions).values(
                id="ses_x",
                token_hash="h",
                user_id="usr_missing",
                created_at="x",
                expires_at="x",
                last_seen_at="x",
            )
        )


def test_opencode_versions_move_into_the_harness_version(tmp_path):
    """0003 (N-09): the version OpenCode installs leaves the parameters and becomes the
    harness version's upstream version; what each version installed does not change."""
    from sqlalchemy import select, text

    from maestro.db import schema as t
    from maestro.services.model_configs import harness_digest

    db = Database.open(tmp_path / "app.db")
    migrate.upgrade(db, "0002")
    rows = {
        "hrn_pinned": ("opencode", {"version": {"type": "string", "default": "1.9.0"}}),
        "hrn_default": ("opencode", {"turns": {"type": "integer"}}),
        "hrn_terminus": ("terminus-2", {"max_turns": {"type": "integer"}}),
    }
    with db.write() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, username, display_name, role, status, password_hash, "
                "revision, created_at, updated_at) "
                "VALUES ('usr_1', 'a', 'a', 'admin', 'active', 'x', 1, 'x', 'x')"
            )
        )
        for harness_id, (agent, parameters) in rows.items():
            conn.execute(
                text(
                    "INSERT INTO harness_versions (id, name, version, agent, description, "
                    "protocols_json, schema_json, content_digest, enabled, created_by, "
                    "created_at) VALUES (:id, :id, '1', :agent, '', '[\"anthropic\"]', "
                    ":schema, 'sha256:old', 1, 'usr_1', 'x')"
                ),
                {
                    "id": harness_id,
                    "agent": agent,
                    "schema": json.dumps({"parameters": parameters}),
                },
            )
    migrate.upgrade(db)
    with db.read() as conn:
        found = {r["id"]: r for r in conn.execute(select(t.harness_versions)).mappings()}
    pinned, default, terminus = found["hrn_pinned"], found["hrn_default"], found["hrn_terminus"]
    assert pinned["upstream_version"] == "1.9.0" and pinned["schema_json"] == {"parameters": {}}
    assert default["upstream_version"] == "1.18.35"
    assert default["schema_json"] == {"parameters": {"turns": {"type": "integer"}}}
    assert pinned["content_digest"] == harness_digest(
        "opencode", "hrn_pinned", "1", ["anthropic"], {"parameters": {}}, "1.9.0"
    )
    assert terminus["upstream_version"] is None and terminus["content_digest"] == "sha256:old"
