"""One module per group of endpoints; ``MaestroClient`` exposes each as an attribute."""

from maestro_sdk.resources.artifacts import Artifacts
from maestro_sdk.resources.attachments import Attachments
from maestro_sdk.resources.batches import Batches
from maestro_sdk.resources.catalog import Catalog
from maestro_sdk.resources.collections import Collections
from maestro_sdk.resources.comparisons import Comparisons
from maestro_sdk.resources.configs import Configs
from maestro_sdk.resources.exports import Exports
from maestro_sdk.resources.operations import Operations
from maestro_sdk.resources.plans import Plans
from maestro_sdk.resources.runs import Runs
from maestro_sdk.resources.snapshots import Snapshots

__all__ = [
    "Artifacts",
    "Attachments",
    "Batches",
    "Catalog",
    "Collections",
    "Comparisons",
    "Configs",
    "Exports",
    "Operations",
    "Plans",
    "Runs",
    "Snapshots",
]
