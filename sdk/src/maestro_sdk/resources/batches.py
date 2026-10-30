"""Runs accepted together, all or none."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from maestro_sdk import models
from maestro_sdk._transport import _body, _key, _query, _Resource
from maestro_sdk.results import BatchSubmission


class Batches(_Resource):
    """Runs accepted together, all or none; each run costs one unit of PAT quota."""

    def preview(self, name: str, runs: list[Mapping[str, Any]]) -> models.BatchPreview:
        body = {"name": name, "runs": [_body(r) for r in runs]}
        return self._c._send("POST", "run-batches/preview", models.BatchPreview, body)

    def submit(
        self,
        name: str,
        runs: list[Mapping[str, Any]],
        *,
        client_request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> BatchSubmission:
        body = {
            "name": name,
            "runs": [_body(r) for r in runs],
            **_query({"client_request_id": client_request_id}),
        }
        key = _key(idempotency_key)
        response = self._c._call("POST", "run-batches", json=body, idempotency_key=key)
        return BatchSubmission(
            accepted=self._c._parse(models.BatchAccepted, response.json()["data"]),
            idempotency_key=key,
            replayed=response.headers.get("Idempotent-Replayed") == "true",
        )

    def get(self, batch_id: str) -> models.Batch:
        return self._c._get(f"run-batches/{batch_id}", models.Batch)

    def list(
        self, *, owner_id: str | None = None, limit: int | None = None
    ) -> Iterator[models.Batch]:
        return self._c._iterate("run-batches", models.Batch, owner_id=owner_id, limit=limit)

    def cancel(self, batch_id: str) -> models.Batch:
        return self._c._send("POST", f"run-batches/{batch_id}/cancel", models.Batch)
