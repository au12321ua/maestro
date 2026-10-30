"""Collections of comparable runs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from maestro_sdk import models
from maestro_sdk._transport import _body, _query, _Resource


class Collections(_Resource):
    """Run lists the team keeps up. The specification is fixed at creation: which
    dimensions may vary, and the values of the others; only matching runs get in."""

    def list(self) -> list[models.Collection]:
        return self._c._get("collections", list[models.Collection])

    def get(self, collection_id: str) -> models.Collection:
        return self._c._get(f"collections/{collection_id}", models.Collection)

    def create(
        self,
        name: str,
        scenario: str,
        *,
        base_run_id: str | None = None,
        base_request: Mapping[str, Any] | None = None,
        variable_dimensions: list[str] | None = None,
        run_ids: list[str] | None = None,
        description: str = "",
        settings: Mapping[str, Any] | None = None,
    ) -> models.Collection:
        """Fixed values come from ``base_run_id`` or a full ``base_request``. Scenarios:
        models, harnesses, model_harness, attachments, repeats, custom (with
        ``variable_dimensions``)."""
        base = {"run_id": base_run_id} if base_run_id else {"request": _body(base_request)}
        body = {
            "name": name,
            "scenario": scenario,
            "base": base,
            "description": description,
            "run_ids": run_ids or [],
            **_query({"variable_dimensions": variable_dimensions, "settings": settings}),
        }
        return self._c._send("POST", "collections", models.Collection, body)

    def update(self, collection_id: str, **changes: Any) -> models.Collection:
        """``name``, ``description`` and/or ``settings``."""
        return self._c._send(
            "PUT", f"collections/{collection_id}", models.Collection, _query(changes)
        )

    def copy(self, collection_id: str, **changes: Any) -> models.Collection:
        """A new collection: ``name``, ``scenario``, ``variable_dimensions``, ``base``
        (``{"run_id": ...}`` or ``{"request": ...}``); ``not_carried`` lists runs left out."""
        return self._c._send(
            "POST", f"collections/{collection_id}/copy", models.Collection, _query(changes)
        )

    def delete(self, collection_id: str) -> models.RecycleEntry:
        return self._c._send("DELETE", f"collections/{collection_id}", models.RecycleEntry)

    def compare(self, collection_id: str, **settings: Any) -> models.Comparison:
        """With the saved settings, or these for this call only."""
        return self._c._send(
            "POST",
            f"collections/{collection_id}/comparison",
            models.Comparison,
            _query(settings) or None,
        )

    def candidates(self, collection_id: str) -> list[models.CollectionCandidate]:
        """Recent runs not in the collection; ``mismatches`` is empty when one can join."""
        return self._c._get(
            f"collections/{collection_id}/candidates", list[models.CollectionCandidate]
        )

    def add_runs(
        self, collection_id: str, run_ids: list[str], *, labels: Mapping[str, str] | None = None
    ) -> models.Collection:
        runs = [_query({"run_id": x, "label": (labels or {}).get(x)}) for x in run_ids]
        return self._c._send(
            "POST", f"collections/{collection_id}/runs", models.Collection, {"runs": runs}
        )

    def label_run(self, collection_id: str, run_id: str, label: str | None) -> models.Collection:
        return self._c._send(
            "PUT", f"collections/{collection_id}/runs/{run_id}", models.Collection, {"label": label}
        )

    def remove_run(self, collection_id: str, run_id: str) -> models.Collection:
        return self._c._send(
            "DELETE", f"collections/{collection_id}/runs/{run_id}", models.Collection
        )

    def events(self, collection_id: str) -> list[models.CollectionEvent]:
        return self._c._get(f"collections/{collection_id}/events", list[models.CollectionEvent])
