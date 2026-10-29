"""Backups of one instance on the same host (12 section 6; drain and other hosts are P1).

A backup set is a directory: a consistent SQLite snapshot taken with the backup API (the
live database is never copied file by file, which would miss its WAL), a copy of the
deployment configuration, optionally the business files, and ``manifest.json`` written
last, so a set without a manifest is incomplete. The master key never goes into a set:
the manifest names the key generation the database needs, and restore refuses when that
key is not in ``master_key_dir`` (credentials could not be decrypted).
"""

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

from maestro import __version__
from maestro.config import Settings
from maestro.core import clock
from maestro.core.errors import MaestroError
from maestro.core.lock import InstanceLock

FORMAT = 1
MANIFEST = "manifest.json"
DATABASE = "app.db"
# Business files under data_dir that a database refers to. ``work`` (Runner scratch) and
# ``exports`` (temporary packages) are left out; cache_dir is rebuilt on demand, and
# prepared images come back from the local image registry's storage under ``registry``.
FILE_DIRS = ("attachments", "registry", "artifacts")
_CHUNK = 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _inventory(path: Path) -> dict:
    count = size = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            with contextlib.suppress(OSError):
                size += os.lstat(os.path.join(root, name)).st_size
                count += 1
    return {"files": count, "bytes": size}


def _snapshot(source: Path, target: Path) -> None:
    """Copy the database through SQLite, then make the copy a single self-contained file."""
    live = sqlite3.connect(source)
    copy = sqlite3.connect(target)
    try:
        live.execute("PRAGMA busy_timeout=5000")
        live.backup(copy)
        copy.execute("PRAGMA journal_mode=DELETE")
        result = copy.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise MaestroError("BACKUP_FAILED", f"snapshot integrity check: {result}", 500)
    finally:
        copy.close()
        live.close()


def _describe(database: Path) -> dict:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        schema = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        instance = connection.execute("SELECT instance_id FROM instance").fetchone()
        key = connection.execute(
            "SELECT generation, basename, kcv FROM master_key_state"
        ).fetchone()
    finally:
        connection.close()
    return {
        "schema_revision": schema[0] if schema else None,
        "instance_id": instance[0] if instance else None,
        "master_key": ({"generation": key[0], "basename": key[1], "kcv": key[2]} if key else None),
    }


def create(
    settings: Settings,
    destination: Path,
    *,
    config_file: Path | None = None,
    include_files: bool = False,
) -> dict:
    """Write a backup set into ``destination``, which must not exist yet. The service may
    keep running: the snapshot is consistent, and files copied afterwards that the
    snapshot does not know are swept as unindexed after a restore."""
    if not settings.database.exists():
        raise MaestroError("BACKUP_FAILED", f"no database at {settings.database}", 500)
    try:
        destination.mkdir(mode=0o700, parents=False)
    except OSError as error:
        raise MaestroError("BACKUP_FAILED", f"{destination}: {error.strerror}", 500) from None
    database = destination / DATABASE
    _snapshot(settings.database, database)
    os.chmod(database, 0o600)
    manifest = {
        "format": FORMAT,
        "created_at": clock.to_text(clock.now()),
        "maestro_version": __version__,
        **_describe(database),
        "database": {
            "file": DATABASE,
            "sha256": _sha256(database),
            "bytes": database.stat().st_size,
        },
        "config": None,
        "files": {},
        "not_included": [
            "master key (keep master_key_dir separately)",
            "cache_dir (rebuilt on demand)",
            "work and exports under data_dir (transient)",
        ],
    }
    if config_file is not None and config_file.is_file():
        shutil.copy2(config_file, destination / "config.toml")
        manifest["config"] = {"file": "config.toml", "sha256": _sha256(destination / "config.toml")}
    for name in FILE_DIRS:
        source = settings.data_dir / name
        if not source.is_dir():
            continue
        entry = {**_inventory(source), "included": include_files}
        if include_files:
            shutil.copytree(source, destination / "files" / name, symlinks=True)
        manifest["files"][name] = entry
    staged = destination / (MANIFEST + ".partial")
    staged.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    os.chmod(staged, 0o600)
    os.replace(staged, destination / MANIFEST)
    return manifest


def read_manifest(source: Path) -> dict:
    try:
        manifest = json.loads((source / MANIFEST).read_text())
    except (OSError, ValueError):
        raise MaestroError(
            "BACKUP_INVALID", f"{source} has no readable {MANIFEST}: not a complete backup", 400
        ) from None
    if manifest.get("format") != FORMAT:
        raise MaestroError("BACKUP_INVALID", f"unsupported backup format in {source}", 400)
    return manifest


def restore(
    settings: Settings,
    source: Path,
    *,
    include_files: bool = False,
    allow_missing_key: bool = False,
) -> dict:
    """Put a backup set back while the service is stopped (the instance lock proves it).
    The database is replaced; with ``include_files`` the business files too, the
    current ones being kept beside them until an administrator removes them."""
    from maestro.db import migrate

    manifest = read_manifest(source)
    database = source / manifest["database"]["file"]
    if _sha256(database) != manifest["database"]["sha256"]:
        raise MaestroError("BACKUP_INVALID", f"{database} does not match its manifest", 400)
    revision = manifest.get("schema_revision")
    if revision not in migrate.revisions():
        raise MaestroError(
            "BACKUP_INVALID",
            f"backup schema {revision} is unknown to this Maestro {__version__}; "
            "restore it with the version that made it",
            400,
        )
    key = manifest.get("master_key") or {}
    key_present = bool(key) and (settings.master_key_dir / key["basename"]).is_file()
    if key and not key_present and not allow_missing_key:
        raise MaestroError(
            "MASTER_KEY_MISSING",
            f"master key generation {key['generation']} ({key['basename']}) is not in "
            f"{settings.master_key_dir}: stored credentials could not be decrypted. Put the "
            "key back, or restore anyway and re-enter every credential",
            409,
        )
    if include_files:
        missing = [
            name
            for name, entry in manifest["files"].items()
            if entry.get("included") and not (source / "files" / name).is_dir()
        ]
        if missing or not any(e.get("included") for e in manifest["files"].values()):
            raise MaestroError(
                "BACKUP_INVALID", f"this backup does not contain the files {missing or ''}", 400
            )
    with InstanceLock(settings.lock_file):
        staged = settings.data_dir / (DATABASE + ".restoring")
        shutil.copyfile(database, staged)
        os.chmod(staged, 0o600)
        for suffix in ("-wal", "-shm", "-journal"):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(settings.database.with_name(settings.database.name + suffix))
        os.replace(staged, settings.database)
        moved = {}
        if include_files:
            stamp = clock.now().strftime("%Y%m%dT%H%M%SZ")
            for name, entry in manifest["files"].items():
                if not entry.get("included"):
                    continue
                target = settings.data_dir / name
                if target.exists():
                    aside = settings.data_dir / f"{name}.before-restore-{stamp}"
                    os.rename(target, aside)
                    moved[name] = str(aside)
                shutil.copytree(source / "files" / name, target, symlinks=True)
    return {
        "restored_from": str(source),
        "schema_revision": revision,
        "instance_id": manifest.get("instance_id"),
        "master_key_present": key_present,
        "files_restored": include_files,
        "previous_files_kept_at": moved,
    }


def activity(settings: Settings) -> dict:
    """Work that a stop would interrupt, read straight from the database (the service may
    be running). Updates refuse while any of it is in progress unless told otherwise.
    Queued runs wait and exports are rebuilt after a restart, so neither counts as busy;
    runs and batches have their own operations, which the run states already cover."""
    connection = sqlite3.connect(f"file:{settings.database}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        runs = dict(
            connection.execute(
                "SELECT state, count(*) FROM runs WHERE state IN "
                "('queued','preparing','running','finalizing') GROUP BY state"
            ).fetchall()
        )
        operations = dict(
            connection.execute(
                "SELECT kind, count(*) FROM operations WHERE status IN ('accepted','running') "
                "AND kind NOT IN ('run','batch','export') GROUP BY kind"
            ).fetchall()
        )
    finally:
        connection.close()
    queued = runs.pop("queued", 0)
    return {
        "active_runs": sum(runs.values()),
        "queued_runs": queued,
        "active_operations": operations,
        "busy": bool(runs) or bool(operations),
    }
