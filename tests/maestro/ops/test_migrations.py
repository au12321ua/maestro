import json

import pytest
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
    """0003: the version OpenCode installs leaves the parameters and becomes the
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
    """0005: old attachment versions and their recycle bin entries go; the
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
    """Single-image preparations are invalidated instead of converted to manifests."""
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


def test_existing_tokens_keep_what_their_scope_allowed(tmp_path):
    """Existing token identities and historical references survive the permissions upgrade."""
    from sqlalchemy import select, text

    from maestro.db import schema as t

    db = Database.open(tmp_path / "app.db")
    migrate.upgrade(db, "0006")
    with db.write() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, username, display_name, role, status, password_hash, "
                "revision, created_at, updated_at) "
                "VALUES ('usr_1', 'a', 'a', 'admin', 'active', 'x', 1, 'x', 'x')"
            )
        )
        for pat_id, scope in (("pat_r", "read"), ("pat_e", "experiment")):
            conn.execute(
                text(
                    "INSERT INTO pats (id, owner_id, name, token_hash, token_prefix, scope, "
                    "expires_at, run_limit, accepted_runs, revision, created_at) VALUES "
                    "(:id, 'usr_1', :id, :id, 'p', :scope, 'x', 1, 0, 1, 'x')"
                ),
                {"id": pat_id, "scope": scope},
            )
            conn.execute(
                t.operations.insert().values(
                    id=f"op_{pat_id}",
                    kind="run",
                    owner_id="usr_1",
                    pat_id=pat_id,
                    status="succeeded",
                    progress_json={},
                    revision=1,
                    created_at="x",
                    updated_at="x",
                )
            )
            conn.execute(
                t.config_resolutions.insert().values(
                    id=f"res_{pat_id}",
                    owner_id="usr_1",
                    pat_id=pat_id,
                    request_json={},
                    result_json={},
                    created_at="x",
                    expires_at="x",
                )
            )
        references = {
            table.name: conn.execute(select(table).order_by(table.c.id)).mappings().all()
            for table in (t.operations, t.config_resolutions)
        }
    migrate.upgrade(db)
    with db.read() as conn:
        rows = dict(conn.execute(select(t.pats.c.id, t.pats.c.permissions_json)).all())
        for table in (t.operations, t.config_resolutions):
            assert (
                conn.execute(select(table).order_by(table.c.id)).mappings().all()
                == references[table.name]
            )
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        identities = conn.exec_driver_sql(
            "SELECT id, token_hash, token_prefix, accepted_runs FROM pats ORDER BY id"
        ).all()
    assert rows == {"pat_r": [], "pat_e": ["experiment"]}
    assert identities == [("pat_e", "pat_e", "p", 0), ("pat_r", "pat_r", "p", 0)]


@pytest.mark.parametrize("failure", ["exception", "broken_reference"])
def test_failed_upgrade_rolls_back_and_restores_foreign_keys(tmp_path, monkeypatch, failure):
    """Neither failed DDL nor orphaned rows may survive a migration attempt."""
    from maestro.db import schema as t

    db = Database.open(tmp_path / "app.db")
    migrate.upgrade(db, "0006")

    def fail(config, target):
        conn = config.attributes["connection"]
        conn.exec_driver_sql("CREATE TABLE migration_canary (id INTEGER PRIMARY KEY)")
        if failure == "exception":
            raise RuntimeError("migration failed")
        conn.execute(
            t.operations.insert().values(
                id="op_orphan",
                kind="run",
                pat_id="pat_missing",
                status="succeeded",
                progress_json={},
                revision=1,
                created_at="x",
                updated_at="x",
            )
        )

    monkeypatch.setattr(migrate.command, "upgrade", fail)
    with pytest.raises(RuntimeError, match="migration failed|foreign key"):
        migrate.upgrade(db)
    assert migrate.current(db) == "0006"
    with db.read() as conn:
        assert "migration_canary" not in inspect(conn).get_table_names()
        assert conn.exec_driver_sql("SELECT count(*) FROM operations").scalar() == 0
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_workflow_upgrade_keeps_existing_collections_and_events(tmp_path):
    from sqlalchemy import select, text

    from maestro.db import schema as t

    db = Database.open(tmp_path / "app.db")
    migrate.upgrade(db, "0007")
    with db.write() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, username, display_name, role, status, password_hash, "
                "revision, created_at, updated_at) VALUES ('usr_1', 'a', 'a', 'member', 'active', 'x', 1, 'x', 'x')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO collections (id, name, description, owner_id, scenario, variable_json, "
                "fixed_json, template_json, settings_json, created_at, updated_at) "
                "VALUES ('col_1', 'existing', '', 'usr_1', 'attachments', '[\"attachments\"]', '{}', '{}', '{}', 'x', 'x')"
            )
        )
        conn.execute(
            t.collection_events.insert().values(
                id="cev_1",
                collection_id="col_1",
                action="created",
                actor_id="usr_1",
                at="x",
                detail_json={},
            )
        )
    migrate.upgrade(db)
    with db.read() as conn:
        collection = conn.execute(select(t.collections)).mappings().one()
        assert collection["name"] == "existing" and collection["variable_json"] == ["attachments"]
        assert (
            collection["current_best_snapshot_id"] is None
            and collection["current_best_json"] is None
        )
        assert conn.execute(select(t.collection_events.c.id)).scalar_one() == "cev_1"
        assert conn.execute(select(t.workflow_records)).first() is None
        assert conn.execute(select(t.workflow_record_revisions)).first() is None
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    db.dispose()
