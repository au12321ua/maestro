"""Saved partial configurations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from maestro_sdk import models
from maestro_sdk._transport import _body, _query, _Resource


class Plans(_Resource):
    """Saved partial configurations; a run fixes the revision it used."""

    def list(self, *, include_archived: bool = False) -> list[models.Plan]:
        return self._c._get("plans", list[models.Plan], include_archived=include_archived)

    def get(self, plan_id: str) -> models.Plan:
        return self._c._get(f"plans/{plan_id}", models.Plan)

    def create(
        self, name: str, template: Mapping[str, Any], *, description: str = ""
    ) -> models.Plan:
        body = {"name": name, "template": _body(template), "description": description}
        return self._c._send("POST", "plans", models.Plan, body)

    def update(self, plan_id: str, expected_revision: int, **changes: Any) -> models.Plan:
        """``name``, ``description`` and/or ``template``; only the creator may change it."""
        body = {"expected_revision": expected_revision, **_query(changes)}
        return self._c._send("PUT", f"plans/{plan_id}", models.Plan, body)

    def copy(self, plan_id: str, *, name: str | None = None) -> models.Plan:
        return self._c._send("POST", f"plans/{plan_id}/copy", models.Plan, {"name": name})

    def archive(self, plan_id: str, archived: bool = True) -> models.Plan:
        return self._c._send(
            "POST", f"plans/{plan_id}/archive", models.Plan, {"archived": archived}
        )

    def delete(self, plan_id: str) -> models.RecycleEntry:
        return self._c._send("DELETE", f"plans/{plan_id}", models.RecycleEntry)
