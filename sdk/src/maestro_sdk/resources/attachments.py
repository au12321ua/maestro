"""Attachment snapshots: publish a directory or a git commit, fetch it back, compare."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import warnings
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from maestro_sdk import models
from maestro_sdk._transport import _Resource, _save
from maestro_sdk.errors import ContentMismatch


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
        return self._publish(files, lambda f: f["source"].read_bytes(), label, parent)

    def publish_commit(
        self,
        repository: str | os.PathLike[str] = ".",
        rev: str = "HEAD",
        *,
        label: str | None = None,
        parent: models.Attachment | str | None = None,
    ) -> models.Attachment:
        """Publish the files of a git commit: the repository root is the attachment root.

        Only what the commit tracks is published, read from git objects, never from the
        working tree, so untracked and ignored files stay out and the executable bit is
        git's file mode. Links and submodules are refused. Uncommitted changes are left
        out with a warning. Needs ``git`` on PATH.
        """
        git = _Git(Path(repository))
        if git.run("rev-parse", "--show-prefix").strip():
            top = git.run("rev-parse", "--show-toplevel").strip()
            raise ValueError(f"{repository} is inside {top}; the whole repository is published")
        commit = git.run("rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}").strip()
        if git.run("status", "--porcelain").strip():
            warnings.warn(
                f"{repository} has uncommitted changes; publishing commit {commit[:12]} only",
                UserWarning,
                stacklevel=2,
            )
        files = git.tree(commit)
        return self._publish(files, lambda f: git.blob(f["object"]), label, parent)

    def _publish(
        self,
        files: list[dict],
        read: Callable[[dict], bytes],
        label: str | None,
        parent: models.Attachment | str | None,
    ) -> models.Attachment:
        by_sha = {f["sha256"]: f for f in files}
        missing = self._c._call(
            "POST", "attachment-contents/missing", json={"sha256": sorted(by_sha)}, retry=True
        ).json()["data"]["missing"]
        for sha in missing:
            self._c._call(
                "PUT",
                f"attachment-contents/{sha}",
                content=read(by_sha[sha]),
                headers={"Content-Type": "application/octet-stream"},
                retry=True,
            )
        parent_id = parent if isinstance(parent, str) or parent is None else parent.id
        listed = [{k: f[k] for k in ("path", "sha256", "executable")} for f in files]
        body = {"files": listed, "label": label, "parent_id": parent_id}
        # Publishing the same content twice returns the same snapshot: safe to repeat.
        response = self._c._call("POST", "attachments", json=body, retry=True)
        snapshot = self._c._parse(models.Attachment, response.json()["data"])
        if snapshot.content_digest != files_digest(listed):
            raise ContentMismatch(
                f"Snapshot {snapshot.id} has digest {snapshot.content_digest}, "
                f"the published files {files_digest(listed)}"
            )
        return snapshot

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


def files_digest(files: list[dict]) -> str:
    """A snapshot's ``content_digest``: the canonical JSON of each file's path, SHA-256
    and executable bit in path order, as the service computes it (03 section 6)."""
    listed = sorted([f["path"], f["sha256"], f["executable"]] for f in files)
    encoded = json.dumps(listed, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class _Git:
    """Read a repository's commits through the ``git`` command."""

    def __init__(self, repository: Path):
        if not repository.is_dir():
            raise ValueError(f"Not a directory: {repository}")
        self.repository = repository
        self._sha256: dict[str, str] = {}

    def run(self, *args: str) -> str:
        return self._bytes(*args).decode()

    def _bytes(self, *args: str) -> bytes:
        try:
            done = subprocess.run(
                ["git", "-C", str(self.repository), *args], capture_output=True, check=False
            )
        except FileNotFoundError:
            raise ValueError("Publishing a commit needs git on PATH") from None
        if done.returncode:
            message = done.stderr.decode(errors="replace").strip()
            raise ValueError(f"git {args[0]} failed: {message}")
        return done.stdout

    def tree(self, commit: str) -> list[dict]:
        """Every file of the commit in path order; links and submodules are refused."""
        listing = self._bytes("ls-tree", "-r", "-z", "--full-tree", commit)
        files = []
        for entry in filter(None, listing.split(b"\0")):
            meta, _, raw = entry.partition(b"\t")
            mode, _, obj = meta.decode().split(" ")
            path = raw.decode("utf-8", errors="surrogateescape")
            if mode not in ("100644", "100755"):
                kind = {"120000": "link", "160000": "submodule"}.get(mode, f"mode {mode}")
                raise ValueError(f"Attachments cannot hold a {kind}: {path}")
            files.append(
                {
                    "path": path,
                    "sha256": self.sha256(obj),
                    "executable": mode == "100755",
                    "object": obj,
                }
            )
        files.sort(key=lambda f: f["path"])
        return files

    def blob(self, obj: str) -> bytes:
        return self._bytes("cat-file", "blob", obj)

    def sha256(self, obj: str) -> str:
        if obj not in self._sha256:
            digest = hashlib.sha256()
            with subprocess.Popen(
                ["git", "-C", str(self.repository), "cat-file", "blob", obj],
                stdout=subprocess.PIPE,
            ) as reading:
                assert reading.stdout is not None
                while chunk := reading.stdout.read(1 << 20):
                    digest.update(chunk)
            if reading.returncode:
                raise ValueError(f"git cat-file failed for {obj}")
            self._sha256[obj] = digest.hexdigest()
        return self._sha256[obj]


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
