"""Experiment topics, including a destination bound to an individual handle."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any, Literal
from urllib.parse import urlsplit

from maestro_sdk import models
from maestro_sdk._transport import _body, _Resource
from maestro_sdk.resources.batches import Batches
from maestro_sdk.resources.runs import Runs

_TOPIC_ID = re.compile(r"top_[0-9a-hjkmnp-tv-z]{26}")
ReferenceKind = Literal["run", "collection", "dataset", "attachment_snapshot"]
MaterialKind = Literal["dataset_version", "attachment_snapshot"]


class Topics(_Resource):
    def list(self, *, q: str | None = None, limit: int | None = None) -> Iterator[models.Topic]:
        """Search by name or ID, yielding candidates and their purposes across pages."""
        return self._c._iterate("topics", models.Topic, q=q, limit=limit)

    def create(
        self, name: str, purpose: str = "", *, settings: Mapping[str, Any] | None = None
    ) -> models.Topic:
        body = {"name": name, "purpose": purpose}
        if settings is not None:
            body["settings"] = dict(settings)
        return self._c._send("POST", "topics", models.Topic, body)

    def _id(self, value: str) -> str:
        if _TOPIC_ID.fullmatch(value):
            return value
        base = urlsplit(self._c.base_url)
        parts = urlsplit(value)
        prefix = base.path + "/topics/"
        identity = parts.path.removeprefix(prefix)
        if (
            parts.path.startswith(prefix)
            and _TOPIC_ID.fullmatch(identity)
            and not parts.query
            and not parts.fragment
            and (
                (not parts.scheme and not parts.netloc)
                or (parts.scheme == base.scheme and parts.netloc.lower() == base.netloc.lower())
            )
        ):
            return identity
        raise ValueError("Expected a topic ID or a topic link on this Maestro service")

    def open(self, url_or_id: str) -> TopicHandle:
        """Bind reads and submissions to this topic. Opening a handle makes no request."""
        return TopicHandle(self, self._id(url_or_id))

    def get(self, url_or_id: str) -> models.Topic:
        return self._c._get(f"topics/{self._id(url_or_id)}", models.Topic)

    def update(
        self,
        url_or_id: str,
        *,
        name: str | None = None,
        purpose: str | None = None,
        settings: Mapping[str, Any] | None = None,
    ) -> models.Topic:
        body = {
            k: v
            for k, v in {
                "name": name,
                "purpose": purpose,
                "settings": dict(settings) if settings is not None else None,
            }.items()
            if v is not None
        }
        return self._c._send("PUT", f"topics/{self._id(url_or_id)}", models.Topic, body)

    def delete(self, url_or_id: str) -> models.RecycleEntry:
        return self._c._send("DELETE", f"topics/{self._id(url_or_id)}", models.RecycleEntry)

    def overview(self, url_or_id: str) -> models.TopicOverview:
        return self._c._get(f"topics/{self._id(url_or_id)}/overview", models.TopicOverview)

    def references(
        self, url_or_id: str, kind: ReferenceKind, *, limit: int | None = None
    ) -> Iterator[models.TopicReference]:
        return self._c._iterate(
            f"topics/{self._id(url_or_id)}/references",
            models.TopicReference,
            kind=kind,
            limit=limit,
        )

    def materials(
        self, url_or_id: str, kind: MaterialKind, *, limit: int | None = None
    ) -> Iterator[models.TopicMaterial]:
        return self._c._iterate(
            f"topics/{self._id(url_or_id)}/materials", models.TopicMaterial, kind=kind, limit=limit
        )

    def change_references(
        self,
        url_or_id: str,
        *,
        add: list[models.TopicObject | Mapping[str, Any]] | None = None,
        remove: list[models.TopicObject | Mapping[str, Any]] | None = None,
    ) -> models.TopicReferenceResult:
        """Add or remove multiple object references in one transaction."""
        return self._c._send(
            "POST",
            f"topics/{self._id(url_or_id)}/references",
            models.TopicReferenceResult,
            {
                "add": [_body(item) for item in add or []],
                "remove": [_body(item) for item in remove or []],
            },
        )


class TopicHandle:
    """One explicit topic. The client can hold many handles without a default destination."""

    def __init__(self, topics: Topics, topic_id: str):
        self._topics = topics
        self._id = topic_id
        self.runs = Runs(topics._c, topic_id=topic_id)
        self.batches = Batches(topics._c, topic_id=topic_id)

    @property
    def id(self) -> str:
        return self._id

    @property
    def url(self) -> str:
        return f"{self._topics._c.base_url}/topics/{self.id}"

    def overview(self) -> models.TopicOverview:
        return self._topics.overview(self.id)

    def references(
        self, kind: ReferenceKind, *, limit: int | None = None
    ) -> Iterator[models.TopicReference]:
        return self._topics.references(self.id, kind, limit=limit)

    def materials(
        self, kind: MaterialKind, *, limit: int | None = None
    ) -> Iterator[models.TopicMaterial]:
        return self._topics.materials(self.id, kind, limit=limit)

    def handoffs(self, *, limit: int = 50) -> Iterator[models.WorkflowRecord]:
        return self._topics._c._iterate(
            "records",
            models.WorkflowRecord,
            target_type="topic",
            target_id=self.id,
            kind="handoff",
            limit=limit,
        )
