"""Opaque workflow records and their attributed revision history."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from maestro_sdk import models
from maestro_sdk._transport import _body, _query, _Resource


class Records(_Resource):
    def create(
        self,
        target: models.RecordTargetInput | Mapping[str, Any],
        kind: str,
        title: str,
        body: Mapping[str, Any],
        *,
        references: list[models.RecordReference | Mapping[str, Any]] | None = None,
        excerpts: list[models.RecordExcerptInput | Mapping[str, Any]] | None = None,
    ) -> models.WorkflowRecord:
        """Target uses type/id and optional snapshot_id; attachment ids are names.
        Excerpts are redacted on the service and retained after artifact expiry."""
        return self._c._send(
            "POST",
            "records",
            models.WorkflowRecord,
            {
                "target": _body(target),
                "kind": kind,
                "title": title,
                "body": dict(body),
                "references": [_body(r) for r in references or []],
                "excerpts": [_body(e) for e in excerpts or []],
            },
        )

    def get(self, record_id: str) -> models.WorkflowRecord:
        return self._c._get(f"records/{record_id}", models.WorkflowRecord)

    def list(
        self,
        target_type: str,
        target_id: str,
        *,
        snapshot_id: str | None = None,
        kind: str | None = None,
    ) -> list[models.WorkflowRecord]:
        """An attachment name includes pinned records; snapshot_id selects pinned ones only."""
        return self._c._get(
            "records",
            list[models.WorkflowRecord],
            **_query(
                {
                    "target_type": target_type,
                    "target_id": target_id,
                    "snapshot_id": snapshot_id,
                    "kind": kind,
                }
            ),
        )

    def update(
        self,
        record_id: str,
        *,
        expected_revision: int,
        **changes: Any,
    ) -> models.WorkflowRecord:
        """Change kind/title/body/references/excerpts; the target and author stay fixed."""
        for field in ("references", "excerpts"):
            if field in changes and changes[field] is not None:
                changes[field] = [_body(value) for value in changes[field]]
        return self._c._send(
            "PUT",
            f"records/{record_id}",
            models.WorkflowRecord,
            {
                "expected_revision": expected_revision,
                **_body(changes),
            },
        )

    def history(self, record_id: str) -> list[models.RecordRevision]:
        """Includes the initial and current versions, in revision order."""
        return self._c._get(f"records/{record_id}/history", list[models.RecordRevision])

    def delete(self, record_id: str) -> models.RecycleEntry:
        return self._c._send("DELETE", f"records/{record_id}", models.RecycleEntry)
