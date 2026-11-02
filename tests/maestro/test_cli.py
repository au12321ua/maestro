import json

import pytest

from maestro import cli
from maestro.core.errors import MaestroError
from maestro.core.lock import InstanceLock


def test_init_creates_instance_and_setup_token(tmp_path, capsys):
    config = tmp_path / "config.toml"
    config.write_text(
        f'data_dir = "{tmp_path}/data"\ncache_dir = "{tmp_path}/cache"\n'
        f'master_key_dir = "{tmp_path}/secrets"\n'
    )
    assert cli.main(["--config", str(config), "init"]) == 0
    out = capsys.readouterr().out
    token = out.strip().splitlines()[-1]
    assert token.startswith("mst_set_")
    assert (tmp_path / "data" / "app.db").exists()
    assert (tmp_path / "secrets").stat().st_mode & 0o077 == 0
    assert len(list((tmp_path / "secrets").iterdir())) == 1
    # Re-running keeps the key and issues a fresh setup token while no admin exists.
    assert cli.main(["--config", str(config), "init"]) == 0
    assert "already present" in capsys.readouterr().out
    assert len(list((tmp_path / "secrets").iterdir())) == 1


def test_config_rejects_unknown_and_relative(tmp_path, capsys):
    config = tmp_path / "config.toml"
    config.write_text('data_dir = "relative"\ncache_dir = "/c"\nmaster_key_dir = "/k"\n')
    assert cli.main(["--config", str(config), "init"]) == 2
    config.write_text("surprise = 1\n")
    assert cli.main(["--config", str(config), "init"]) == 2
    assert "CONFIG_INVALID" in capsys.readouterr().err


def test_openapi_export(tmp_path):
    target = tmp_path / "openapi.json"
    assert cli.main(["openapi", "--output", str(target)]) == 0
    spec = json.loads(target.read_text())
    assert spec["info"]["title"] == "Maestro API"
    # The export is deterministic, so CI can diff it against a committed copy.
    again = tmp_path / "again.json"
    cli.main(["openapi", "--output", str(again)])
    assert again.read_text() == target.read_text()


def test_instance_lock_is_exclusive(tmp_path):
    with InstanceLock(tmp_path / "lock"):
        with pytest.raises(MaestroError) as error, InstanceLock(tmp_path / "lock"):
            pass
        assert error.value.code == "INSTANCE_LOCKED"
