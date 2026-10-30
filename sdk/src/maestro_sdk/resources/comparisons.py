"""Runs side by side."""

from __future__ import annotations

from maestro_sdk import models
from maestro_sdk._transport import _query, _Resource


class Comparisons(_Resource):
    """Runs side by side, computed on demand and never saved."""

    def compare(
        self,
        run_ids: list[str],
        *,
        exclude: list[str] | None = None,
        scope: str | None = None,
        baseline_run_id: str | None = None,
    ) -> models.Comparison:
        """``exclude``: failure groups to leave out (default: the external ones); failures
        of the other groups count as failed. ``scope``: ``common`` or ``all`` tasks."""
        body = {
            "run_ids": run_ids,
            **_query({"exclude": exclude, "scope": scope, "baseline_run_id": baseline_run_id}),
        }
        return self._c._send("POST", "comparisons", models.Comparison, body)
