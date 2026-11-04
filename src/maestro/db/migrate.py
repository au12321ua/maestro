"""Versioned schema migrations. Run offline, never implicitly on serve."""

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from maestro.db.engine import Database


def _config() -> Config:
    config = Config()
    config.set_main_option("script_location", "maestro.db:migrations")
    return config


def head() -> str:
    return ScriptDirectory.from_config(_config()).get_current_head()


def revisions() -> set[str]:
    """Every revision this program knows; a database at any of them can be upgraded."""
    return {script.revision for script in ScriptDirectory.from_config(_config()).walk_revisions()}


def current(db: Database) -> str | None:
    with db.engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def upgrade(db: Database, target: str = "head") -> None:
    config = _config()
    with db.engine.connect() as connection:
        # SQLite batch migrations replace tables, including parents still referenced by
        # historical rows. PRAGMA foreign_keys must change before BEGIN, so use the
        # DBAPI cursor directly rather than triggering SQLAlchemy's begin listener.
        cursor = connection.connection.driver_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=OFF")
            config.attributes["connection"] = connection
            command.upgrade(config, target)
            broken = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchmany(5)
            if broken:
                raise RuntimeError(f"migration would leave broken foreign keys: {broken}")
            connection.commit()
        finally:
            # Roll back before restoring enforcement, also on failed DDL or validation.
            connection.rollback()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()


def revision(db: Database, message: str) -> None:
    """Developer helper: autogenerate the next migration against an upgraded database."""
    config = _config()
    with db.engine.connect() as connection:
        config.attributes["connection"] = connection
        command.revision(config, message=message, autogenerate=True)
