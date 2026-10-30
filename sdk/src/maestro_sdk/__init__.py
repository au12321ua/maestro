"""Python client for Maestro: submit, watch, compare and rerun team experiments.

from maestro_sdk import MaestroClient

client = MaestroClient("https://maestro.example", token=pat)
client.require("runs", "execution")
resolution = client.configs.resolve(config).raise_for_blockers()
submitted = client.runs.submit(name="baseline", resolution_id=resolution.resolution_id)
client.operations.wait(submitted.operation_id)
"""

from maestro_sdk import models
from maestro_sdk.client import MaestroClient
from maestro_sdk.errors import (
    ApiError,
    BlockersPresent,
    ContentMismatch,
    ContractDriftWarning,
    MaestroError,
    TransportError,
    UnsupportedCapability,
    WaitTimeout,
)
from maestro_sdk.results import BatchSubmission, Download, Resolution, Submission

__all__ = [
    "ApiError",
    "BatchSubmission",
    "BlockersPresent",
    "ContentMismatch",
    "ContractDriftWarning",
    "Download",
    "MaestroClient",
    "MaestroError",
    "Resolution",
    "Submission",
    "TransportError",
    "UnsupportedCapability",
    "WaitTimeout",
    "models",
]
