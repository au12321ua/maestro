"""Attachment snapshots: publish a directory, fetch it back, compare snapshots."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path, PurePosixPath

from maestro_sdk import models
from maestro_sdk._transport import _Resource, _save


class Attachments(_Resource):
    def list(
        self, *, name: str | None = None, include_disabled: bool = False
    ) -> list[models.Attachment]:
        return self._c._get(
            "attachments", list[models.Attachment], name=name, include_disabled=include_disabled
        )

    def get(self, attachment_id: str) -> models.Attachment:
        return self._c._get(f"attachments/{attachment_id}", models.Attachment)

    def publish(
        self,
        directory: str | os.PathLike[str],
        *,
        label: str | None = None,
        parent: models.Attachment | str | None = None,
        exclude: tuple[str, ...] = (".git",),
    ) -> models.Attachment:
        """Publish a directory as a snapshot, uploading only the files the service lacks.

        The same content published again returns the existing snapshot (its label stays
        as first given). ``label`` names the snapshot within its attachment and never
        moves; ``parent`` records the snapshot this one was made from. Directories named
        in ``exclude`` are left out. Nothing in the directory runs on upload.
        """
        root = Path(directory)
        if not root.is_dir():
            raise ValueError(f"Not a directory: {root}")
        files = _describe(root, exclude)
        by_sha = {f["sha256"]: f["source"] for f in files}
        missing = self._c._call(
            "POST", "attachment-contents/missing", json={"sha256": sorted(by_sha)}, retry=True
        ).json()["data"]["missing"]
        for sha in missing:
            with by_sha[sha].open("rb") as content:
                self._c._call(
                    "PUT",
                    f"attachment-contents/{sha}",
                    content=content.read(),
                    headers={"Content-Type": "application/octet-stream"},
                    retry=True,
                )
        parent_id = parent if isinstance(parent, str) or parent is None else parent.id
        body = {
            "files": [{k: f[k] for k in ("path", "sha256", "executable")} for f in files],
            "label": label,
            "parent_id": parent_id,
        }
        # Publishing the same content twice returns the same snapshot: safe to repeat.
        response = self._c._call("POST", "attachments", json=body, retry=True)
        return self._c._parse(models.Attachment, response.json()["data"])

    def fetch(
        self,
        attachment: models.Attachment | str,
        directory: str | os.PathLike[str],
        *,
        prune: bool = False,
    ) -> Path:
        """Make ``directory`` hold the snapshot's files, downloading only those missing
        or different here. With ``prune``, extra files and file/directory conflicts are
        removed too. Existing links in a destination path are refused."""
        attachment_id = attachment if isinstance(attachment, str) else attachment.id
        snapshot = self.get(attachment_id)
        root = Path(directory)
        for parent in (root.absolute(), *root.absolute().parents):
            if parent.is_symlink():
                raise ValueError(f"Fetch destination cannot contain a link: {parent}")
        root.mkdir(parents=True, exist_ok=True)
        wanted = {f.path for f in snapshot.files or []}
        conflicts: set[Path] = set()
        # Check every destination before modifying anything. The server only publishes
        # relative paths, but an existing local parent can still point outside root.
        for entry in snapshot.files or []:
            parts = PurePosixPath(entry.path).parts
            if not parts or entry.path.startswith("/") or any(p in {".", ".."} for p in parts):
                raise ValueError(f"Unsafe attachment path: {entry.path}")
            target = root
            for index, part in enumerate(parts):
                target = target / part
                if target.is_symlink():
                    raise ValueError(f"Fetch destination cannot contain a link: {target}")
                if target.exists() and target.is_dir() != (index < len(parts) - 1):
                    conflicts.add(target)
                    break
        if conflicts and not prune:
            raise ValueError("File/directory conflicts in fetch destination; use prune=True")
        for target in sorted(conflicts, key=lambda p: len(p.parts)):
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
        for entry in snapshot.files or []:
            target = root.joinpath(*PurePosixPath(entry.path).parts)
            # Replacing a hard-linked file also keeps chmod local to the destination.
            if not (
                target.is_file() and target.stat().st_nlink == 1 and _sha256(target) == entry.sha256
            ):
                _save(self._c, f"attachments/{attachment_id}/files/{entry.path}", target)
            target.chmod(0o755 if entry.executable else 0o644)
        if prune:
            for path in sorted(root.rglob("*"), reverse=True):
                relative = path.relative_to(root).as_posix()
                if path.is_dir() and not path.is_symlink():
                    if not any(path.iterdir()):
                        path.rmdir()
                elif relative not in wanted:
                    path.unlink()
        return root

    def download(
        self, attachment: models.Attachment | str, destination: str | os.PathLike[str]
    ) -> Path:
        """Save the whole snapshot as a zip file (paths and executable bits kept)."""
        attachment_id = attachment if isinstance(attachment, str) else attachment.id
        return _save(self._c, f"attachments/{attachment_id}/archive", Path(destination))

    def diff(
        self, attachment: models.Attachment | str, base: models.Attachment | str | None = None
    ) -> models.AttachmentDiff:
        """Files added, removed and changed since ``base`` (default: the parent)."""
        attachment_id = attachment if isinstance(attachment, str) else attachment.id
        base_id = base if isinstance(base, str) or base is None else base.id
        return self._c._get(
            f"attachments/{attachment_id}/diff", models.AttachmentDiff, base_id=base_id
        )

    def set_enabled(self, attachment_id: str, enabled: bool) -> models.Attachment:
        return self._c._send(
            "POST", f"attachments/{attachment_id}/enabled", models.Attachment, {"enabled": enabled}
        )

    def delete(self, attachment_id: str) -> models.RecycleEntry:
        return self._c._send("DELETE", f"attachments/{attachment_id}", models.RecycleEntry)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _describe(root: Path, exclude: tuple[str, ...]) -> list[dict]:
    """Every regular file below ``root`` in path order; links are refused."""
    files = []
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if any(part in exclude for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"Attachments cannot hold links: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"Not a regular file: {path}")
        files.append(
            {
                "path": relative.as_posix(),
                "sha256": _sha256(path),
                "executable": bool(path.stat().st_mode & 0o100),
                "source": path,
            }
        )
    return files
