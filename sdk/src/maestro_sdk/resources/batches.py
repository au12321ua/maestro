"""Runs accepted together, all or none."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from maestro_sdk import models
from maestro_sdk._transport import Transport, _body, _key, _query, _Resource
from maestro_sdk.resources.runs import topic_destination
from maestro_sdk.results import BatchSubmission


class Batches(_Resource):
    """Runs accepted together, all or none; each run costs one unit of PAT quota."""

    def __init__(self, client: Transport, *, topic_id: str | None = None):
        super().__init__(client)
        self._topic_id = topic_id

    def _requests(self, runs, topic_id):
        destination = topic_destination(self._topic_id, topic_id)
        items = [dict(_body(run)) for run in runs]
        if destination is not None:
            for item in items:
                item["topic_id"] = topic_destination(destination, item.get("topic_id"))
        return items

    def preview(
        self, name: str, runs: list[Mapping[str, Any]], *, topic_id: str | None = None
    ) -> models.BatchPreview:
        body = {"name": name, "runs": self._requests(runs, topic_id)}
        return self._c._send("POST", "run-batches/preview", models.BatchPreview, body)

    def submit(
        self,
        name: str,
        runs: list[Mapping[str, Any]],
        *,
        client_request_id: str | None = None,
        topic_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> BatchSubmission:
        body = {
            "name": name,
            "runs": self._requests(runs, topic_id),
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
