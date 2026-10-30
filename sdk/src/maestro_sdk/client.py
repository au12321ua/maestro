"""Synchronous client for the Maestro ``/api/v1`` contract.

The models come from the OpenAPI document (``models.py``); the SDK adds what a
generator cannot: authentication, idempotent submission, paging, waiting and verified
downloads (``_transport.py``), with one module per group of endpoints in ``resources/``.
It never runs anything locally and needs nothing from the service's code.
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path
from typing import Any

import httpx

from maestro_sdk import models
from maestro_sdk._contract import CONTRACT_DIGEST
from maestro_sdk._transport import Transport
from maestro_sdk.errors import ContractDriftWarning, UnsupportedCapability
from maestro_sdk.resources import (
    Artifacts,
    Attachments,
    Batches,
    Catalog,
    Collections,
    Comparisons,
    Configs,
    Exports,
    Operations,
    Plans,
    Runs,
    Snapshots,
)
from maestro_sdk.results import BatchSubmission, Download, Resolution, Submission

API_VERSION = "v1"

__all__ = [
    "BatchSubmission",
    "ContractDriftWarning",
    "Download",
    "MaestroClient",
    "Resolution",
    "Submission",
]


class MaestroClient(Transport):
    """Client for one Maestro service.

    ``token`` is a personal access token (PAT). By default the client connects directly
    and ignores proxy variables (``trust_env=False``), so a PAT never travels through a
    proxy the caller did not choose; pass ``trust_env=True`` to use them.
    """

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        timeout: float = 30.0,
        retries: int = 3,
        trust_env: bool = False,
        http: httpx.Client | None = None,
    ):
        super().__init__(
            base_url, token, timeout=timeout, retries=retries, trust_env=trust_env, http=http
        )
        self._warned = False
        self.catalog = Catalog(self)
        self.configs = Configs(self)
        self.attachments = Attachments(self)
        self.plans = Plans(self)
        self.comparisons = Comparisons(self)
        self.collections = Collections(self)
        self.snapshots = Snapshots(self)
        self.runs = Runs(self)
        self.batches = Batches(self)
        self.operations = Operations(self)
        self.artifacts = Artifacts(self)
        self.exports = Exports(self)

    @classmethod
    def from_env(cls, **kwargs: Any) -> MaestroClient:
        """``MAESTRO_URL`` and ``MAESTRO_TOKEN`` (or ``MAESTRO_TOKEN_FILE``)."""
        token = os.environ.get("MAESTRO_TOKEN")
        if not token and os.environ.get("MAESTRO_TOKEN_FILE"):
            token = Path(os.environ["MAESTRO_TOKEN_FILE"]).read_text().strip()
        return cls(os.environ["MAESTRO_URL"], token, **kwargs)

    def __repr__(self) -> str:
        return f"MaestroClient({self.base_url!r})"

    def __enter__(self) -> MaestroClient:  # noqa: PYI034 (typing.Self needs 3.11)
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def capabilities(self) -> models.Capabilities:
        """Versions, features, limits and which channel may do what (API-01, API-06)."""
        caps = self._get("capabilities", models.Capabilities)
        if caps.contract_digest != CONTRACT_DIGEST and not self._warned:
            self._warned = True
            warnings.warn(
                "The service's API contract differs from this SDK's; "
                "install the SDK that matches the service",
                ContractDriftWarning,
                stacklevel=2,
            )
        return caps

    def require(self, *features: str) -> models.Capabilities:
        """Fail early with UNSUPPORTED_CAPABILITY instead of guessing at fields."""
        caps = self.capabilities()
        if caps.api_version != API_VERSION:
            raise UnsupportedCapability(
                f"Service speaks API {caps.api_version}; this SDK speaks {API_VERSION}"
            )
        missing = [f for f in features if not caps.features.get(f)]
        if missing:
            raise UnsupportedCapability(f"Service does not offer: {', '.join(missing)}")
        return caps

    def me(self) -> models.Me:
        return self._get("me", models.Me)

    def system_status(self) -> models.Health:
        """Readiness, whether runs are accepted now, storage and the worker."""
        return self._get("system-status", models.Health)
