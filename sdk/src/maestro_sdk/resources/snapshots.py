"""Frozen comparisons with notes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from maestro_sdk import models
from maestro_sdk._transport import _query, _Resource


class Snapshots(_Resource):
    """Frozen comparisons of ended runs, with notes written by people or agents."""

    def list(self, *, collection_id: str | None = None) -> list[models.SnapshotSummary]:
        return self._c._get(
            "analysis-snapshots", list[models.SnapshotSummary], collection_id=collection_id
        )

    def get(self, snapshot_id: str) -> models.Snapshot:
        return self._c._get(f"analysis-snapshots/{snapshot_id}", models.Snapshot)

    def create(
        self,
        name: str,
        *,
        collection_id: str | None = None,
        run_ids: list[str] | None = None,
        settings: Mapping[str, Any] | None = None,
        note: str | None = None,
    ) -> models.Snapshot:
        body = {
            "name": name,
            **_query(
                {
                    "collection_id": collection_id,
                    "run_ids": run_ids,
                    "settings": settings,
                    "note": note,
                }
            ),
        }
        return self._c._send("POST", "analysis-snapshots", models.Snapshot, body)

    def delete(self, snapshot_id: str) -> models.RecycleEntry:
        return self._c._send("DELETE", f"analysis-snapshots/{snapshot_id}", models.RecycleEntry)

    def add_note(self, snapshot_id: str, body: str) -> models.Snapshot:
        return self._c._send(
            "POST", f"analysis-snapshots/{snapshot_id}/notes", models.Snapshot, {"body": body}
        )

    def update_note(self, snapshot_id: str, note_id: str, body: str) -> models.Snapshot:
        """Only the note's author may change it."""
        return self._c._send(
            "PUT",
            f"analysis-snapshots/{snapshot_id}/notes/{note_id}",
            models.Snapshot,
            {"body": body},
        )
