"""``maestro`` command: initialize an instance, migrate, serve, back up and restore,
export OpenAPI.

Installation, systemd and updates belong to ``maestroctl`` (``ops/maestroctl.py``, 12),
which runs these commands as the service account.
"""

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path

from maestro import config
from maestro.core.errors import MaestroError


def _settings(args):
    return config.load(Path(args.config) if args.config else None)


def _private_database(settings) -> None:
    """The database holds password hashes and encrypted credentials: make it owner-only
    before SQLite creates it with the umask's mode (the -wal and -shm files it adds beside
    it take the main file's mode), and tighten one left looser by an earlier run."""
    path = settings.database
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        for suffix in ("-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                os.chmod(path.with_name(path.name + suffix), 0o600)
    except OSError as error:
        raise MaestroError("CONFIG_INVALID", f"database {path}: {error.strerror}", 500) from None


def cmd_init(args) -> int:
    """Create directories, migrate an empty database, create the master key, issue setup."""
    from maestro.application import Maestro
    from maestro.db import migrate

    settings = _settings(args)
    for path, mode in (
        (settings.data_dir, 0o700),
        (settings.cache_dir, 0o700),
        (settings.master_key_dir, 0o700),
    ):
        path.mkdir(parents=True, exist_ok=True, mode=mode)
    settings.artifacts_dir.mkdir(exist_ok=True, mode=0o700)
    _private_database(settings)
    service = Maestro.build(settings)
    migrate.upgrade(service.db)
    created = service.keys.initialize()
    token = service.accounts.issue_setup_token()
    print(f"database: {settings.database} (schema {migrate.current(service.db)})")
    print(f"master key: {'created' if created else 'already present'} in {settings.master_key_dir}")
    if token:
        print("One-time setup token for the first administrator (shown once):")
        print(token)
    else:
        print("An administrator already exists; setup is closed.")
    return 0


def cmd_upgrade(args) -> int:
    from maestro.db import migrate
    from maestro.db.engine import Database

    settings = _settings(args)
    _private_database(settings)
    db = Database.open(settings.database)
    before = migrate.current(db)
    migrate.upgrade(db)
    print(f"schema {before} -> {migrate.current(db)}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from maestro.api.app import create_app
    from maestro.application import Maestro

    settings = _settings(args)
    _private_database(settings)
    for note in settings.advice():
        print(f"WARNING: {note}", file=sys.stderr)
    executor = environment = None
    sources = {}
    if settings.runner_python and not args.demo_executor:
        executor = _harbor_executor(settings)
        environment, sources = _preparation(settings)
    elif not args.demo_executor:
        print("runner_python is not configured: runs will be refused.", file=sys.stderr)
    if args.demo_executor:
        # Development only: scripted fakes so pages can be exercised without Docker,
        # Harbor or the Hub. Never for real experiments.
        from maestro.testing.fakes import FakeDatasetSource, FakeEnvironment, FakeExecutor

        executor = FakeExecutor(step_delay=1.0, executor_id="demo-fake")
        environment = FakeEnvironment()
        source = FakeDatasetSource()
        source.add("demo", "arith", "v1", ["add-01", "add-02", "mul-01"])
        source.add("demo", "arith", "v2", ["add-01", "add-02", "mul-01", "div-01"], revision="2")
        sources = {"fake": source}
        print("WARNING: demo fakes enabled; results are not real.", file=sys.stderr)
    service = Maestro.build(settings, executor=executor, environment=environment, sources=sources)
    state = service.schema_state()
    if not state["compatible"]:
        print(
            f"Schema {state['current']} does not match {state['head']}; run 'maestro db upgrade'.",
            file=sys.stderr,
        )
    from maestro.core.lock import InstanceLock

    with InstanceLock(settings.lock_file):
        uvicorn.run(
            create_app(service),
            host=args.host or settings.listen_host,
            port=args.port or settings.listen_port,
            proxy_headers=True,
            log_level="info",
        )
    return 0


def _harbor_executor(settings):
    from maestro.execution.attachments import AttachmentStore
    from maestro.execution.docker import DockerResources
    from maestro.execution.harbor import HarborExecutor
    from maestro.execution.materials import TaskMaterials

    if not settings.runner_python.is_file():
        raise MaestroError(
            "CONFIG_INVALID", f"runner_python {settings.runner_python} does not exist", 500
        )
    settings.work_dir.mkdir(mode=0o700, exist_ok=True)
    return HarborExecutor(
        settings.runner_python,
        settings.work_dir,
        TaskMaterials(settings.cache_dir),
        DockerResources(timeout=settings.cleanup_timeout_sec),
        term_grace=settings.runner_term_grace_sec,
        silence_limit=settings.runner_silence_sec,
        heartbeat_sec=settings.runner_heartbeat_sec,
        artifact_limits=settings.artifact_limits(),
        attachments=AttachmentStore(settings.data_dir, settings.cache_dir),
    )


def _docker_environment(settings):
    from maestro.execution.preparation import DockerEnvironment

    return DockerEnvironment(
        settings.preparation_dir,
        build_timeout=settings.build_timeout_default_sec,
        build_timeout_max=settings.build_timeout_max_sec,
        pull_timeout=settings.pull_timeout_sec,
        save_timeout=settings.save_timeout_sec,
    )


def _preparation(settings):
    from maestro.execution.hub import HubSource

    hub = HubSource(
        settings.runner_python, settings.work_dir, max_runners=settings.hub_runner_slots
    )
    return _docker_environment(settings), {"hub": hub}


def cmd_local_dataset(args) -> int:
    """Development only: import a directory of Harbor tasks and run its first preparation
    with the real Docker environment, while the service is stopped. Never for benchmarks."""
    import asyncio

    from maestro.application import Maestro
    from maestro.core.lock import InstanceLock
    from maestro.execution.materials import LocalTaskSource
    from maestro.services.instance import instance_id

    settings = _settings(args)
    _private_database(settings)
    source = LocalTaskSource(Path(args.directory).resolve())
    service = Maestro.build(
        settings,
        environment=_docker_environment(settings),
        sources={"local": source},
    )

    async def run():
        service.preparer.start_epoch("ep_dev_local", instance_id(service.db))
        return await service.preparer.import_and_prepare(
            {"source_kind": "local", "namespace": "local", "name": args.name, "ref": "local"},
            None,
        )

    with InstanceLock(settings.lock_file):
        result = asyncio.run(run())
    imported = result["import"]
    if imported["status"] != "succeeded":
        print(f"import failed: {imported['error']}", file=sys.stderr)
        return 1
    prepared = result.get("preparation") or {}
    print(
        f"dataset local/{args.name} version {result['dataset_version_id']}: "
        f"first preparation {prepared.get('status', 'not needed')}"
    )
    return 0 if prepared.get("status") in (None, "succeeded") else 1


def cmd_backup(args) -> int:
    from maestro import backup

    settings = _settings(args)
    config_file = Path(args.config) if args.config else config.DEFAULT_CONFIG
    manifest = backup.create(
        settings,
        Path(args.output).absolute(),
        config_file=config_file,
        include_files=args.include_files,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


def cmd_restore(args) -> int:
    from maestro import backup

    settings = _settings(args)
    report = backup.restore(
        settings,
        Path(args.source).absolute(),
        include_files=args.include_files,
        allow_missing_key=args.allow_missing_key,
    )
    _private_database(settings)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def cmd_activity(args) -> int:
    from maestro import backup

    print(json.dumps(backup.activity(_settings(args)), sort_keys=True))
    return 0


def cmd_openapi(args) -> int:
    from maestro.api.openapi import document

    text = json.dumps(document(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(text)
    else:
        sys.stdout.write(text)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="maestro")
    parser.add_argument("--config", default=os.environ.get("MAESTRO_CONFIG"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="initialize a new instance").set_defaults(func=cmd_init)
    db = sub.add_parser("db", help="database maintenance")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    db_sub.add_parser("upgrade", help="apply migrations").set_defaults(func=cmd_upgrade)
    serve = sub.add_parser("serve", help="run the HTTP service")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--demo-executor", action="store_true", help=argparse.SUPPRESS)
    serve.set_defaults(func=cmd_serve)
    local = sub.add_parser("dev-local-dataset", help=argparse.SUPPRESS)
    local.add_argument("name")
    local.add_argument("directory")
    local.set_defaults(func=cmd_local_dataset)
    backup = sub.add_parser("backup", help="write a backup set (database, config, manifest)")
    backup.add_argument("--output", required=True, help="new directory for the set")
    backup.add_argument("--include-files", action="store_true", help="copy business files too")
    backup.set_defaults(func=cmd_backup)
    restore = sub.add_parser("restore", help="restore a backup set while the service is stopped")
    restore.add_argument("source")
    restore.add_argument("--include-files", action="store_true")
    restore.add_argument("--allow-missing-key", action="store_true")
    restore.set_defaults(func=cmd_restore)
    sub.add_parser("activity", help="work a stop would interrupt, as JSON").set_defaults(
        func=cmd_activity
    )
    openapi = sub.add_parser("openapi", help="print the public OpenAPI document")
    openapi.add_argument("--output")
    openapi.set_defaults(func=cmd_openapi)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except MaestroError as error:
        print(f"{error.code}: {error.message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
