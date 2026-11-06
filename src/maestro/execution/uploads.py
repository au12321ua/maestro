"""Uploaded task source: original files share the attachment content store.

Validation delegates to the isolated Runner's Harbor parser. No converter, task
script, upstream lookup or credential is involved in inspecting the uploaded tree.
"""

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

from sqlalchemy import select

from maestro.boundaries.dataset_source import (
    MISMATCH,
    NOT_FOUND,
    UNSUPPORTED,
    FetchedTask,
    SourceError,
    SourceTask,
    SourceVersion,
)
from maestro.db import schema as t
from maestro.execution.attachments import describe, files_digest


class UploadSource:
    kind = "upload"

    def __init__(self, db, store, validator):
        self.db = db
        self.store = store
        self.validator = validator

    async def search(self, query, credential):
        raise SourceError(UNSUPPORTED, "Uploaded datasets have no upstream search or updates")

    async def list_versions(self, namespace, name, credential):
        raise SourceError(UNSUPPORTED, "Uploaded datasets have no upstream versions or updates")

    def _files(self, digest):
        with self.db.read() as conn:
            operation = conn.execute(
                select(t.dataset_files.c.operation_id)
                .where(t.dataset_files.c.dataset_digest == digest)
                .limit(1)
            ).scalar()
            return [
                {k: row[k] for k in ("path", "sha256", "size", "executable")}
                for row in conn.execute(
                    select(t.dataset_files)
                    .where(t.dataset_files.c.operation_id == operation)
                    .order_by(t.dataset_files.c.path)
                ).mappings()
            ]

    def _materialize(self, files, destination):
        destination.mkdir(parents=True)
        for entry in files:
            blob = self.store.blob(entry["sha256"])
            if blob is None:
                raise SourceError(NOT_FOUND, f"Uploaded content missing: {entry['path']}")
            path = destination / entry["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(blob, path)
            os.chmod(path, 0o755 if entry["executable"] else 0o644)

    async def describe(self, namespace, name, ref, credential):
        if credential:
            raise SourceError(UNSUPPORTED, "Uploaded datasets do not use Hub credentials")
        files = await asyncio.to_thread(self._files, ref)
        if not files or files_digest(files) != ref:
            raise SourceError(NOT_FOUND, "Uploaded dataset content is missing")
        grouped = {}
        for entry in files:
            key, _, relative = entry["path"].partition("/")
            grouped.setdefault(key, []).append({**entry, "path": relative})
        with tempfile.TemporaryDirectory(prefix="dataset-", dir=self.store.root) as temporary:
            root = Path(temporary) / "tasks"
            await asyncio.to_thread(self._materialize, files, root)
            await self.validator.validate_tasks(root)
        return SourceVersion(
            namespace,
            name,
            ref,
            "digest",
            ref,
            tuple(
                SourceTask(key, files_digest(entries), {"files": entries})
                for key, entries in sorted(grouped.items())
            ),
        )

    async def fetch(self, task, destination, credential):
        files = task.locator["files"]
        if files_digest(files) != task.content_digest:
            raise SourceError(UNSUPPORTED, "Uploaded task file list differs from its digest")
        await asyncio.to_thread(self._materialize, files, destination)
        if (
            await asyncio.to_thread(lambda: files_digest(describe(destination)))
            != task.content_digest
        ):
            raise SourceError(MISMATCH, "Uploaded task contents differ from their digest")
        return FetchedTask(destination, task.content_digest)
