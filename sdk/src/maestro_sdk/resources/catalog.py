"""Datasets, versions, tasks and preparations."""

from __future__ import annotations

from collections.abc import Iterator

from maestro_sdk import models
from maestro_sdk._transport import _Resource


class Catalog(_Resource):
    """Datasets added by administrators: versions, tasks, admission, content and cache."""

    def sources(self) -> list[models.DatasetSource]:
        return self._c._get("dataset-sources", list[models.DatasetSource])

    def datasets(self, *, limit: int | None = None) -> Iterator[models.Dataset]:
        return self._c._iterate("datasets", models.Dataset, limit=limit)

    def version(self, version_id: str) -> models.DatasetVersionDetail:
        return self._c._get(f"dataset-versions/{version_id}", models.DatasetVersionDetail)

    def tasks(self, version_id: str) -> list[models.Task]:
        return self._c._get(f"dataset-versions/{version_id}/tasks", list[models.Task])

    def preparation(self, preparation_id: str) -> models.Preparation:
        return self._c._get(f"preparations/{preparation_id}", models.Preparation)
