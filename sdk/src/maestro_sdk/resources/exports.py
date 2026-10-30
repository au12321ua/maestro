"""Packages of runs for analysis."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from maestro_sdk import models
from maestro_sdk._transport import _query, _Resource, _save


class Exports(_Resource):
    def create(
        self,
        run_ids: list[str],
        *,
        include_artifacts: bool = True,
        artifact_kinds: list[str] | None = None,
        task_keys: list[str] | None = None,
    ) -> models.Export:
        """An asynchronous package; wait on its ``operation_id`` and then download it."""
        body = {
            "run_ids": run_ids,
            "include_artifacts": include_artifacts,
            **_query({"artifact_kinds": artifact_kinds, "task_keys": task_keys}),
        }
        return self._c._send("POST", "exports", models.Export, body)

    def get(self, export_id: str) -> models.Export:
        return self._c._get(f"exports/{export_id}", models.Export)

    def list(self, *, limit: int | None = None) -> Iterator[models.Export]:
        return self._c._iterate("exports", models.Export, limit=limit)

    def download(self, export_id: str, destination: str | os.PathLike[str]) -> Path:
        """Save the zip package and check its digest."""
        return _save(self._c, f"exports/{export_id}/download", Path(destination))
