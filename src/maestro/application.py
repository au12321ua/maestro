"""Service container: one place that wires settings, database, boundaries and services."""

from dataclasses import dataclass

from maestro.boundaries.dataset_source import DatasetSource
from maestro.boundaries.environment import Environment
from maestro.boundaries.executor import Executor
from maestro.boundaries.storage import ArtifactStore, LocalArtifactStore
from maestro.config import Settings
from maestro.db import migrate
from maestro.db.engine import Database
from maestro.execution.attachments import AttachmentStore
from maestro.execution.materials import TaskMaterials
from maestro.security.master_key import MasterKeys
from maestro.services.accounts import Accounts
from maestro.services.artifacts import Artifacts
from maestro.services.attachments import Attachments
from maestro.services.capacity import Capacity, HostCapacity
from maestro.services.catalog import Catalog
from maestro.services.collections import Collections, Comparisons, Snapshots
from maestro.services.configuration import Resolver
from maestro.services.credentials import Credentials
from maestro.services.datasets import Datasets
from maestro.services.disk import DiskWatch
from maestro.services.exports import Exports
from maestro.services.maintenance import Maintenance
from maestro.services.model_configs import ModelConfigs
from maestro.services.operations import Operations
from maestro.services.pats import Pats
from maestro.services.plans import Plans
from maestro.services.policy import Policies
from maestro.services.preparation import Preparer
from maestro.services.recycle import Recycle
from maestro.services.runs import Runs
from maestro.services.setup_network import SetupNetworks
from maestro.services.upstream import UpstreamVersions
from maestro.services.worker import Worker


@dataclass
class Maestro:
    settings: Settings
    db: Database
    keys: MasterKeys
    accounts: Accounts
    pats: Pats
    credentials: Credentials
    operations: Operations
    catalog: Catalog
    datasets: Datasets
    runs: Runs
    policies: Policies
    setup_networks: SetupNetworks
    models: ModelConfigs
    attachments: Attachments
    plans: Plans
    comparisons: Comparisons
    collections: Collections
    snapshots: Snapshots
    artifacts: Artifacts
    exports: Exports
    recycle: Recycle
    maintenance: Maintenance
    disk: DiskWatch
    capacity: Capacity
    store: ArtifactStore
    executor: Executor | None
    worker: Worker | None
    preparer: Preparer | None

    @classmethod
    def build(
        cls,
        settings: Settings,
        *,
        executor: Executor | None = None,
        store: ArtifactStore | None = None,
        db: Database | None = None,
        environment: Environment | None = None,
        sources: dict[str, DatasetSource] | None = None,
    ) -> "Maestro":
        """The worker runs only with an executor; preparation also needs an environment."""
        db = db or Database.open(settings.database)
        store = store or LocalArtifactStore(settings.artifacts_dir)
        keys = MasterKeys(db, settings.master_key_dir, settings.data_dir)
        credentials = Credentials(db, keys)
        sources = sources or {}
        host = HostCapacity.detect(
            settings.worker_slots, settings.execution_cpus, settings.execution_memory_mb
        )
        disk = DiskWatch(settings.data_dir, settings)
        materials = TaskMaterials(settings.cache_dir)
        attachment_store = AttachmentStore(settings.data_dir, settings.cache_dir)
        preparer = (
            Preparer(
                db,
                environment,
                sources,
                materials,
                credentials,
                restore_slots=settings.restore_slots,
                build_timeout_max=settings.build_timeout_max_sec,
            )
            if environment is not None
            else None
        )
        worker = (
            Worker(db, executor, store, host, disk=disk, preparer=preparer, credentials=credentials)
            if executor is not None
            else None
        )
        location = environment.location_id if environment is not None else "local-docker"
        adapters = dict(executor.harness_adapters) if executor is not None else None
        model_params = dict(getattr(executor, "harness_model_parameters", {}) or {})
        resolver = Resolver(
            host,
            adapters_of=lambda: adapters,
            model_params_of=lambda: model_params,
            executor_id=executor.executor_id if executor is not None else None,
            network_of=lambda: executor.network_restriction if executor is not None else None,
        )
        runs = Runs(db, host, disk, resolver)
        setup_networks = SetupNetworks(db)
        artifacts = Artifacts(db, store)
        exports = Exports(
            db, store, settings.exports_dir, runs, disk, max_bytes=settings.export_max_bytes
        )
        attachments = Attachments(db, attachment_store, adapters)
        return cls(
            settings=settings,
            db=db,
            keys=keys,
            accounts=Accounts(db, settings.setup_token_file, settings.session_hours),
            pats=Pats(db),
            credentials=credentials,
            operations=Operations(db),
            catalog=Catalog(db, location),
            datasets=Datasets(db, sources, credentials),
            runs=runs,
            policies=Policies(db),
            setup_networks=setup_networks,
            models=ModelConfigs(
                db,
                adapters,
                dict(getattr(executor, "harness_parameters", {}) or {}),
                model_params,
                dict(getattr(executor, "harness_upstream", {}) or {}),
                UpstreamVersions(setup_networks.current),
            ),
            attachments=attachments,
            plans=Plans(db),
            comparisons=Comparisons(db),
            collections=runs.collections,
            snapshots=Snapshots(db, runs.collections),
            artifacts=artifacts,
            exports=exports,
            recycle=Recycle(db, store, materials, exports, environment, attachment_store),
            maintenance=Maintenance(artifacts, exports, attachments, runs),
            disk=disk,
            capacity=Capacity(db, host),
            store=store,
            executor=executor,
            worker=worker,
            preparer=preparer,
        )

    def schema_state(self) -> dict:
        current = migrate.current(self.db)
        head = migrate.head()
        return {"current": current, "head": head, "compatible": current == head}
