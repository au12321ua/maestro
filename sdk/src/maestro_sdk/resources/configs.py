"""Models, harnesses, team policy, setup network and configuration resolution."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from maestro_sdk import models
from maestro_sdk._transport import _Resource
from maestro_sdk.results import Resolution


class Configs(_Resource):
    """Models, harnesses, credential names, team policy, setup network, and configuration
    resolution."""

    def models(self, *, include_disabled: bool = False) -> list[models.ModelConfig]:
        return self._c._get(
            "model-configs", list[models.ModelConfig], include_disabled=include_disabled
        )

    def model(self, config_id: str) -> models.ModelConfig:
        return self._c._get(f"model-configs/{config_id}", models.ModelConfig)

    def harnesses(self) -> list[models.Harness]:
        return self._c._get("harness-versions", list[models.Harness])

    def harness(self, harness_id: str) -> models.Harness:
        return self._c._get(f"harness-versions/{harness_id}", models.Harness)

    def credential_names(self) -> list[models.CredentialName]:
        """Names only, for choosing a credential for a secret author parameter."""
        return self._c._get("credentials", list[models.CredentialName])

    def team_policy(self) -> models.TeamPolicy:
        return self._c._get("team-policy", models.TeamPolicy)

    def setup_network(self) -> models.SetupNetwork:
        """How setup work reaches the network on this deployment: proxy, package mirrors
        and the harness setup time limit in force."""
        return self._c._get("setup-network", models.SetupNetwork)

    def setup_network_revisions(self) -> list[models.SetupNetwork]:
        """Every revision, newest first; an attempt's ``setup_network_revision`` names one."""
        return self._c._get("setup-network/revisions", list[models.SetupNetwork])

    def resolve(self, config: models.ConfigRequestInput | Mapping[str, Any]) -> Resolution:
        """Effective values, their sources, blockers and ``config_digest``; reserves nothing."""
        return self._c._send("POST", "config-resolutions", Resolution, config)

    def resolution(self, resolution_id: str) -> Resolution:
        return self._c._get(f"config-resolutions/{resolution_id}", Resolution)
