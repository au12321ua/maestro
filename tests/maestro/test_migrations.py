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


def test_attachments_from_before_snapshots_are_removed(tmp_path):
    """0005 (N-06, U-50): old attachment versions and their recycle bin entries go; the
    rest of the recycle bin stays, and the new tables start empty."""
    from sqlalchemy import func, select, text

    from maestro.db import schema as t

    db = Database.open(tmp_path / "app.db")
    migrate.upgrade(db, "0004")
    with db.write() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, username, display_name, role, status, password_hash, "
                "revision, created_at, updated_at) "
                "VALUES ('usr_1', 'a', 'a', 'admin', 'active', 'x', 1, 'x', 'x')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO attachment_versions (id, owner_id, name, version, description, "
                "content_digest, manifest_json, files_json, file_count, size_bytes, "
                "has_bootstrap, enabled, created_at) VALUES ('att_1', 'usr_1', 'tools', "
                "'1.0.0', '', 'sha256:old', '{}', '[]', 0, 0, 0, 1, 'x')"
            )
        )
        for object_type, object_id in (
            ("attachment_version", "att_1"),
            ("experiment_plan", "pln_1"),
        ):
            conn.execute(
                text(
                    "INSERT INTO deletions (id, object_type, object_id, label, state, "
                    "deleted_by, deleted_at, detail_json) VALUES (:id, :type, :object, '', "
                    "'recycled', 'usr_1', 'x', '{}')"
                ),
                {"id": f"del_{object_id}", "type": object_type, "object": object_id},
            )
    migrate.upgrade(db)
    with db.read() as conn:
        assert conn.execute(select(func.count()).select_from(t.attachment_versions)).scalar() == 0
        kinds = conn.execute(select(t.deletions.c.object_type)).scalars().all()
    assert kinds == ["experiment_plan"]


def test_the_last_admission_reset_names_the_current_preparation_method():
    """A migration that resets admissions writes the method digest of its release as a
    value. A later change of the method needs a migration of its own: without one,
    existing admissions would name a method the code no longer has."""
    import importlib.util
    from pathlib import Path

    from maestro.execution.materials import PREPARATION_METHOD_DIGEST

    versions = Path(migrate.__file__).with_name("migrations") / "versions"
    methods = []
    for path in sorted(versions.glob("[0-9]*.py")):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if hasattr(module, "METHOD"):
            methods.append(module.METHOD)
    assert methods and methods[-1] == PREPARATION_METHOD_DIGEST


def test_upgrading_resets_admissions_and_drops_old_preparations(tmp_path):
    """U-53: nothing from single-image preparations is converted."""
    from maestro.execution.materials import PREPARATION_METHOD_DIGEST

    db = Database.open(tmp_path / "app.db")
    migrate.upgrade(db, "0005")
    at = "2026-10-28T00:00:00Z"
    rows = {
        "users": [
            {
                "id": "usr_1",
                "username": "a",
                "display_name": "a",
                "role": "admin",
                "status": "active",
                "password_hash": "x",
                "revision": 1,
                "created_at": at,
                "updated_at": at,
            }
        ],
        "datasets": [
            {
                "id": "ds_1",
                "source_kind": "local",
                "namespace": "n",
                "name": "d",
                "description": "",
                "enabled": True,
                "created_at": at,
            }
        ],
        "dataset_versions": [
            {
                "id": "dv_1",
                "dataset_id": "ds_1",
                "upstream_ref": "v1",
                "ref_kind": "tag",
                "resolved_digest": "sha256:a",
                "manifest_json": {},
                "status": "ready",
                "created_at": at,
            }
        ],
        "tasks": [
            {
                "id": key,
                "dataset_version_id": "dv_1",
                "task_key": key,
                "definition_json": {},
                "content_digest": "sha256:c",
                "material_digest": "sha256:d",
                "spec_digest": "sha256:e",
            }
            for key in ("task_1", "task_2")
        ],
        "task_admissions": [
            {
                "id": f"adm_{key}",
                "task_id": key,
                "preparation_method_digest": "sha256:old",
                "state": state,
                "revision": 3,
                "evidence_json": {"output": {"image_id": "sha256:f"}},
            }
            for key, state in (("task_1", "admitted"), ("task_2", "revoked"))
        ],
        "preparations": [
            {
                "id": "prep_1",
                "task_id": "task_1",
                "spec_digest": "sha256:e",
                "location_id": "local-docker",
                "cache_state": "ready",
                "generation": 1,
                "output_json": {"image_id": "sha256:f"},
            }
        ],
    }
    with db.engine.begin() as conn:
        for table, values in rows.items():
            conn.execute(metadata.tables[table].insert(), values)
    migrate.upgrade(db)
    with db.engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT task_id, preparation_method_digest, state, revision, evidence_json "
            "FROM task_admissions ORDER BY task_id"
        ).all()
        assert [(r[0], r[1], r[2], r[3], json.loads(r[4])) for r in rows] == [
            ("task_1", PREPARATION_METHOD_DIGEST, "pending", 4, {}),
            ("task_2", PREPARATION_METHOD_DIGEST, "revoked", 4, {}),
        ]
        assert conn.exec_driver_sql("SELECT count(*) FROM preparations").scalar() == 0
