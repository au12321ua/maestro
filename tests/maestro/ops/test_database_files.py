"""The SQLite database holds password hashes and encrypted credentials, so
the file and the -wal/-shm files SQLite adds beside it are owner-only."""

import os
import stat

import pytest

from maestro import cli
from maestro.config import load
from maestro.db.engine import Database


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        f'data_dir = "{tmp_path}/data"\ncache_dir = "{tmp_path}/cache"\n'
        f'master_key_dir = "{tmp_path}/secrets"\n'
    )
    return path


@pytest.fixture
def umask_022():
    previous = os.umask(0o022)
    yield
    os.umask(previous)


def mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def database_files(directory):
    return sorted(p for p in directory.iterdir() if p.name.startswith("app.db"))


def test_init_creates_the_database_owner_only_with_its_wal_files(config, umask_022, capsys):
    assert cli.main(["--config", str(config), "init"]) == 0
    settings = load(config)
    assert mode(settings.database) == 0o600
    # Open it the way the service does: SQLite creates app.db-wal and app.db-shm now.
    db = Database.open(settings.database)
    with db.write() as conn:
        conn.exec_driver_sql("CREATE TABLE IF NOT EXISTS probe (x INTEGER)")
        names = [p.name for p in database_files(settings.data_dir)]
        assert {"app.db", "app.db-wal", "app.db-shm"} <= set(names)
        for path in database_files(settings.data_dir):
            assert mode(path) == 0o600, path.name
    db.dispose()


def test_an_existing_loose_database_is_tightened_by_the_commands_that_open_it(
    config, umask_022, capsys
):
    cli.main(["--config", str(config), "init"])
    settings = load(config)
    settings.database.chmod(0o644)
    assert cli.main(["--config", str(config), "db", "upgrade"]) == 0
    assert mode(settings.database) == 0o600
    settings.database.chmod(0o664)
    assert cli.main(["--config", str(config), "init"]) == 0
    assert mode(settings.database) == 0o600
