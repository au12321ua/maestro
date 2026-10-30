"""Single artifact files."""

from __future__ import annotations

import os
from pathlib import Path

from maestro_sdk import models
from maestro_sdk._transport import _Resource, _save


class Artifacts(_Resource):
    def read(self, artifact_id: str, *, offset: int = 0, length: int | None = None) -> bytes:
        """A slice (at most 1 MiB with ``length``), e.g. for following a log."""
        response = self._c._call(
            "GET",
            f"artifacts/{artifact_id}/download",
            params={"offset": offset, "length": length},
            headers={"Accept": "application/octet-stream"},
        )
        return response.content

    def download(
        self, artifact: models.Artifact | str, destination: str | os.PathLike[str]
    ) -> Path:
        """Save one file to ``destination`` (a file path) and check its digest."""
        artifact_id = artifact if isinstance(artifact, str) else artifact.id
        return _save(self._c, f"artifacts/{artifact_id}/download", Path(destination))

    def delete(self, artifact_id: str) -> models.RecycleEntry:
        """Administrators only (for example a confirmed leak)."""
        return self._c._send("DELETE", f"artifacts/{artifact_id}", models.RecycleEntry)
