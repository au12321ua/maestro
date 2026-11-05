"""Deployment limits in config.toml: defaults, ranges, relations and effective values."""

import json

import pytest

from maestro.config import TUNING, Settings, load
from maestro.core.errors import MaestroError


def write(tmp_path, **extra) -> object:
    data, cache, keys = tmp_path / "data", tmp_path / "cache", tmp_path / "keys"
    lines = [f'data_dir = "{data}"', f'cache_dir = "{cache}"', f'master_key_dir = "{keys}"']
    # JSON scalars (true, "10", 10.5) are valid TOML too.
    lines += [f"{name} = {json.dumps(value)}" for name, value in extra.items()]
    path = tmp_path / "config.toml"
    path.write_text("\n".join(lines) + "\n")
    return path


def test_the_defaults_are_the_documented_values_and_pass_the_checks(tmp_path):
    settings = load(write(tmp_path))
    assert settings.effective_limits() == {
        "export_max_bytes": 10 * 1024**3,
        "artifact_max_files": 500,
        "artifact_file_max_bytes": 64 * 1024**2,
        "artifact_attempt_max_bytes": 1024**3,
        "build_timeout_default_sec": 600,
        "build_timeout_max_sec": 3600,
        "pull_timeout_sec": 1800,
        "push_timeout_sec": 1800,
        "restore_slots": 2,
        "hub_runner_slots": 4,
    }
    assert settings.advice() == []


def test_values_from_the_file_take_effect(tmp_path):
    settings = load(
        write(
            tmp_path,
            artifact_max_files=50,
            artifact_file_max_mb=8,
            artifact_attempt_max_mb=100,
            export_max_gb=2,
            restore_slots=3,
            runner_heartbeat_sec=5,
            runner_silence_sec=60,
        )
    )
    assert settings.artifact_limits() == {
        "max_files": 50,
        "max_file_bytes": 8 * 1024**2,
        "max_total_bytes": 100 * 1024**2,
    }
    assert settings.export_max_bytes == 2 * 1024**3
    assert settings.restore_slots == 3


@pytest.mark.parametrize("name", sorted(TUNING))
def test_each_limit_rejects_values_outside_its_range(tmp_path, name):
    low, high = TUNING[name]
    for bad in (low - 1, high + 1, 0, -5):
        with pytest.raises(MaestroError) as raised:
            load(write(tmp_path, **{name: bad}))
        assert raised.value.code == "CONFIG_INVALID" and name in raised.value.message


@pytest.mark.parametrize("bad", ["10", 10.5, True])
def test_limits_must_be_integers(tmp_path, bad):
    with pytest.raises(MaestroError) as raised:
        load(write(tmp_path, restore_slots=bad))
    assert "restore_slots" in raised.value.message


def test_settings_built_in_code_are_checked_the_same_way(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "d", cache_dir=tmp_path / "c", master_key_dir=tmp_path / "k"
    )
    with pytest.raises(MaestroError):
        Settings(**{**settings.__dict__, "restore_slots": None}).check()


def test_a_single_file_cannot_be_allowed_more_than_the_whole_attempt(tmp_path):
    with pytest.raises(MaestroError) as raised:
        load(write(tmp_path, artifact_file_max_mb=200, artifact_attempt_max_mb=100))
    assert "artifact_file_max_mb" in raised.value.message


def test_the_default_build_time_cannot_exceed_the_maximum(tmp_path):
    with pytest.raises(MaestroError) as raised:
        load(write(tmp_path, build_timeout_default_sec=900, build_timeout_max_sec=600))
    assert "build_timeout_default_sec" in raised.value.message


def test_the_runner_must_be_allowed_to_miss_several_heartbeats(tmp_path):
    with pytest.raises(MaestroError) as raised:
        load(write(tmp_path, runner_heartbeat_sec=30, runner_silence_sec=60))
    assert "runner_silence_sec" in raised.value.message


def test_unknown_keys_are_still_refused(tmp_path):
    with pytest.raises(MaestroError) as raised:
        load(write(tmp_path, artifact_max_file=10))  # a typo must not be silently ignored
    assert "artifact_max_file" in raised.value.message


def test_an_export_larger_than_the_dispatch_watermark_is_advised_against(tmp_path):
    settings = load(write(tmp_path, export_max_gb=50, storage_dispatch_min_free_gb=10))
    assert any("export_max_gb" in line for line in settings.advice())
    quiet = load(write(tmp_path, export_max_gb=5, storage_dispatch_min_free_gb=10))
    assert quiet.advice() == []
