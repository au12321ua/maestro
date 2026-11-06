"""Datasets, versions, tasks and preparations."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from maestro_sdk import models
from maestro_sdk._transport import _Resource
from maestro_sdk.errors import ContentMismatch
from maestro_sdk.resources.attachments import _describe, files_digest


class Catalog(_Resource):
    """Datasets added by administrators: versions, tasks, admission, content and cache."""

    def sources(self) -> list[models.DatasetSource]:
        return self._c._get("dataset-sources", list[models.DatasetSource])

    def upload(
        self, directory: str | os.PathLike[str], *, namespace: str, name: str
    ) -> models.Operation:
        """Upload Harbor task subdirectories as-is and start import and first preparation.

        Needs an administrator PAT with datasets permission. Wait on the returned
        operation; its result identifies the dataset version and preparation operation.
        Repeating the same content resolves to the same version within this dataset.
        Task validation errors are reported per task in the import operation.
        """
        root = Path(directory)
        if root.is_symlink() or not root.is_dir():
            raise ValueError(f"Not a regular directory: {root}")
        files = _describe(root, ())
        if not files or any("/" not in f["path"] for f in files):
            raise ValueError("Upload a directory of Harbor task subdirectories")
        for task in root.iterdir():
            if task.is_dir() and not any(f["path"].startswith(task.name + "/") for f in files):
                raise ValueError(f"Task {task.name} is empty")
        by_sha = {f["sha256"]: f for f in files}
        missing = self._c._call(
            "POST", "attachment-contents/missing", json={"sha256": sorted(by_sha)}, retry=True
        ).json()["data"]["missing"]
        for sha in missing:
            self._c._call(
                "PUT",
                f"attachment-contents/{sha}",
                content=by_sha[sha]["source"].read_bytes(),
                headers={"Content-Type": "application/octet-stream"},
                retry=True,
            )
        listed = [{k: f[k] for k in ("path", "sha256", "executable")} for f in files]
        response = self._c._call(
            "POST",
            "dataset-uploads",
            retry=True,
            json={"namespace": namespace, "name": name, "files": listed},
        )
        operation = self._c._parse(models.Operation, response.json()["data"])
        if operation.progress.request.ref != files_digest(listed):
            raise ContentMismatch("The dataset upload operation has a different content digest")
        return operation

    def datasets(self, *, limit: int | None = None) -> Iterator[models.Dataset]:
        return self._c._iterate("datasets", models.Dataset, limit=limit)

    def version(self, version_id: str) -> models.DatasetVersionDetail:
        return self._c._get(f"dataset-versions/{version_id}", models.DatasetVersionDetail)

    def tasks(self, version_id: str) -> list[models.Task]:
        return self._c._get(f"dataset-versions/{version_id}/tasks", list[models.Task])

    def files(self, version_id: str) -> list[models.DatasetVersionFile]:
        """List an uploaded version's original file metadata without recording a task read."""
        return self._c._get(f"dataset-versions/{version_id}/files", list[models.DatasetVersionFile])

    def preparation(self, preparation_id: str) -> models.Preparation:
        return self._c._get(f"preparations/{preparation_id}", models.Preparation)

    def task(self, task_id: str) -> models.TaskDetail:
        """Read the instruction; the service records this content read."""
        return self._c._get(f"tasks/{task_id}", models.TaskDetail)

    def reads(self, task_ids: list[str]) -> list[models.TaskRead]:
        return self._c._send("POST", "tasks/reads", list[models.TaskRead], {"task_ids": task_ids})
