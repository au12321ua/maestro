"""Attachment snapshots (03 section 6): file contents by SHA-256, snapshots as file lists.

A snapshot is a list of files (path, SHA-256, executable bit) holding ``manifest.json`` at
its root; ``bootstrap.sh`` at the root is the install script each trial runs in its own
container (03 section 7). Clients upload only the contents the store lacks, then commit
the list: an unchanged file is never sent twice.

Each content lives once under ``blobs/<hex>``, whichever snapshots share it, and each
snapshot's list under ``snapshots/<hex>.json``; both are original content (not a cache).
The Runner copies a snapshot from a directory rebuilt from them on demand under the cache
directory. Paths are checked part by part and only regular files exist, so links,
devices, absolute paths and ``..`` cannot be expressed. Nothing here runs the script.

The content digest covers each file's path, SHA-256 and executable bit, so the Runner can
compare a container's copy file by file with ``sha256sum``.
"""

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from maestro.core.digests import digest_json

MANIFEST = "manifest.json"
BOOTSTRAP = "bootstrap.sh"
MANIFEST_MAX_BYTES = 64 * 1024
EFFECTS = ("model_call", "agent_tools", "task_environment", "verifier", "scoring")
# Packages that change verification or scoring form a different scoring contract
# (03 section 6); P0 has no such contract, so they are refused at upload.
SUPPORTED_EFFECTS = ("model_call", "agent_tools", "task_environment")
# Manifest fields of the design that P0 does not implement; refused rather than ignored.
UNSUPPORTED_FIELDS = ("parameter_schema", "entrypoints", "load_order_requirements")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}")
PROVIDED = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}")
SHA256 = re.compile(r"[0-9a-f]{64}")
# Path parts the Runner can pass through ``sha256sum`` output and shell commands unquoted.
PATH_PART = re.compile(r"[A-Za-z0-9._+@-]{1,255}")
PATH_MAX = 1024
DEPTH_MAX = 32


class PackageError(Exception):
    """``problems`` are (JSON pointer, code, message)."""

    def __init__(self, message: str, problems: list[tuple[str, str, str]]):
        super().__init__(message)
        self.problems = problems


@dataclass
class Package:
    manifest: dict
    files: list[dict]
    content_digest: str
    size_bytes: int
    has_bootstrap: bool


def display(label: str | None, content_digest: str) -> str:
    """How a snapshot is named in messages: its label, or the start of its digest."""
    return label or content_digest.removeprefix("sha256:")[:12]


def files_digest(files: list[dict]) -> str:
    return digest_json([[f["path"], f["sha256"], f["executable"]] for f in files])


def _check_path(name: str) -> str | None:
    """A relative path made of safe parts, or None. Directory entries end with '/'."""
    name = name.removesuffix("/").removeprefix("./")
    if not name or len(name) > PATH_MAX or name.startswith("/"):
        return None
    parts = name.split("/")
    if len(parts) > DEPTH_MAX:
        return None
    if any(p in {".", ".."} or not PATH_PART.fullmatch(p) for p in parts):
        return None
    return name


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def describe(root: Path) -> list[dict]:
    """The files below ``root`` in path order; links and special files are refused."""
    files = []
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"not a regular file: {relative}")
        files.append(
            {
                "path": relative,
                "size": info.st_size,
                "sha256": _sha256(path),
                "executable": bool(info.st_mode & 0o100),
            }
        )
    return files


def _read_manifest(path: Path) -> dict:
    if path.stat().st_size > MANIFEST_MAX_BYTES:
        raise PackageError(
            "manifest.json is too large", [("/manifest", "TOO_LARGE", str(MANIFEST_MAX_BYTES))]
        )
    try:
        manifest = json.loads(path.read_text("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError) as error:
        raise PackageError(f"manifest.json is not JSON: {error}", []) from None
    if not isinstance(manifest, dict):
        raise PackageError("manifest.json must be an object", [("/manifest", "TYPE", "")])
    return check_manifest(manifest)


def _names(value, pattern: re.Pattern) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(x, str) and pattern.fullmatch(x) for x in value)
        and len(set(value)) == len(value)
    )


def check_manifest(manifest: dict) -> dict:
    problems: list[tuple[str, str, str]] = []

    def bad(key: str, code: str, message: str = "") -> None:
        problems.append((f"/manifest/{key}", code, message))

    known = {
        "schema_version",
        "name",
        "description",
        "effects",
        "compatible_harnesses",
        "provides",
    }
    for key in manifest:
        if key == "version":
            bad(key, "UNKNOWN_FIELD", "snapshots have no version; give a label when publishing")
        elif key in UNSUPPORTED_FIELDS:
            bad(key, "UNSUPPORTED", "not supported by this version of the platform")
        elif key not in known:
            bad(key, "UNKNOWN_FIELD")
    if manifest.get("schema_version") != 1:
        bad("schema_version", "ENUM", "1")
    if not isinstance(manifest.get("name"), str) or not NAME.fullmatch(manifest["name"]):
        bad("name", "PATTERN", "letters, digits and ._- (at most 64)")
    description = manifest.get("description", "")
    if not isinstance(description, str) or len(description) > 512:
        bad("description", "LENGTH", "at most 512 characters")
    effects = manifest.get("effects")
    if not _names(effects, re.compile("|".join(EFFECTS))) or not effects:
        bad("effects", "ENUM", ", ".join(EFFECTS))
    else:
        if set(effects) - set(SUPPORTED_EFFECTS):
            bad(
                "effects",
                "UNSUPPORTED",
                "packages that change verification or scoring are not supported yet",
            )
    harnesses = manifest.get("compatible_harnesses")
    if not _names(harnesses, re.compile(r"\*|[a-z0-9][a-z0-9_.-]{0,63}")) or not harnesses:
        bad("compatible_harnesses", "INVALID", 'harness agent names, or ["*"] for any')
    provides = manifest.get("provides", {})
    if not isinstance(provides, dict) or set(provides) - {"tools", "env"}:
        bad("provides", "INVALID", '{"tools": [...], "env": [...]}')
    else:
        for kind in ("tools", "env"):
            if not _names(provides.get(kind, []), PROVIDED):
                bad(f"provides/{kind}", "INVALID", "a list of distinct names")
    if problems:
        raise PackageError("manifest.json is not valid", problems)
    return {
        "schema_version": 1,
        "name": manifest["name"],
        "description": description,
        "effects": sorted(effects),
        "compatible_harnesses": sorted(harnesses),
        "provides": {k: sorted(provides.get(k, [])) for k in ("tools", "env")},
    }


def _hex(content_digest: str) -> str | None:
    algorithm, _, value = content_digest.partition(":")
    return value if algorithm == "sha256" and SHA256.fullmatch(value) else None


class AttachmentStore:
    """``<data_dir>/attachments``: ``blobs/<hex>`` file contents, ``snapshots/<hex>.json``
    file lists, and ``.receive-*`` uploads and ``.archive-*`` downloads in progress. Rebuilt snapshot directories live
    under ``<cache_dir>/attachments``. A purge removes contents no snapshot still holds;
    nothing else does."""

    def __init__(self, data_dir: Path, cache_dir: Path | None = None):
        self.root = data_dir / "attachments"
        self.blobs = self.root / "blobs"
        self.snapshots = self.root / "snapshots"
        self.trees = (cache_dir or data_dir / "cache") / "attachments"

    # --- contents ----------------------------------------------------------------------

    def receive(self):
        """A temporary file on the store's file system for an upload in progress."""
        self.root.mkdir(parents=True, exist_ok=True)
        return tempfile.NamedTemporaryFile(prefix=".receive-", dir=self.root, delete=False)

    def blob(self, sha256: str) -> Path | None:
        path = self.blobs / sha256 if SHA256.fullmatch(sha256) else None
        return path if path is not None and path.is_file() else None

    def add_blob(self, source: Path, sha256: str) -> None:
        """Move an uploaded file in under its SHA-256, which the caller has checked; an
        existing copy is kept. The caller holds the database write lock."""
        self.blobs.mkdir(parents=True, exist_ok=True)
        if self.blob(sha256):
            source.unlink(missing_ok=True)
            return
        os.chmod(source, 0o644)
        os.rename(source, self.blobs / sha256)

    def retire_blob(self, sha256: str) -> Path | None:
        """Move a content out of reach in one rename; the caller deletes what it returns."""
        path = self.blob(sha256)
        if path is None:
            return None
        retired = Path(tempfile.mkdtemp(prefix=".remove-", dir=self.root))
        os.rename(path, retired / sha256)
        return retired

    # --- snapshots ---------------------------------------------------------------------

    def check(self, entries, max_files: int, max_bytes: int) -> Package:
        """The snapshot a client's file list describes, with every content present.
        ``entries`` are {path, sha256, executable}; sizes come from the stored contents."""
        problems: list[tuple[str, str, str]] = []

        def problem(path: str, code: str, message: str = "") -> None:
            if len(problems) < 50:
                problems.append((f"/files/{path}", code, message))

        if not isinstance(entries, list) or not entries:
            raise PackageError("The snapshot lists no files", [("/files", "EMPTY", "")])
        if len(entries) > max_files:
            raise PackageError(
                "Attachment has too many files", [("/files", "TOO_MANY_FILES", str(max_files))]
            )
        files: dict[str, dict] = {}
        total = 0
        for index, entry in enumerate(entries):
            raw = entry.get("path") if isinstance(entry, dict) else None
            path = _check_path(raw) if isinstance(raw, str) and not raw.endswith("/") else None
            if path is None:
                problem(str(index), "PATH", "only relative names of letters, digits and ._+@-")
                continue
            sha = entry.get("sha256")
            if not isinstance(sha, str) or not SHA256.fullmatch(sha):
                problem(path, "SHA256", "64 lowercase hexadecimal digits")
                continue
            if not isinstance(entry.get("executable"), bool):
                problem(path, "EXECUTABLE", "true or false")
                continue
            if path in files:
                problem(path, "DUPLICATE", "the list holds this path twice")
                continue
            blob = self.blob(sha)
            if blob is None:
                problem(path, "MISSING", "upload this content first")
                continue
            size = blob.stat().st_size
            total += size
            files[path] = {
                "path": path,
                "size": size,
                "sha256": sha,
                "executable": entry["executable"],
            }
        names = set(files)
        for path in names:
            if any(str(parent) in names for parent in Path(path).parents):
                problem(path, "DUPLICATE", "a file and a directory share this name")
        if total > max_bytes:
            problem("", "TOO_LARGE", f"at most {max_bytes} bytes")
        if problems:
            raise PackageError("The snapshot holds entries that are not allowed", problems)
        if MANIFEST not in files:
            raise PackageError("The snapshot has no manifest.json", [("/files", "MANIFEST", "")])
        manifest = _read_manifest(self.blobs / files[MANIFEST]["sha256"])
        ordered = [files[p] for p in sorted(files)]
        return Package(
            manifest=manifest,
            files=ordered,
            content_digest=files_digest(ordered),
            size_bytes=total,
            has_bootstrap=BOOTSTRAP in files,
        )

    def record(self, package: Package) -> None:
        """Keep the snapshot's file list, so its directory can be rebuilt without the
        database. Under the database write lock, with the contents in place."""
        self.snapshots.mkdir(parents=True, exist_ok=True)
        target = self.snapshots / f"{_hex(package.content_digest)}.json"
        partial = target.with_name(f".{target.name}.partial")
        partial.write_text(json.dumps(package.files, sort_keys=True))
        os.rename(partial, target)

    def forget(self, content_digest: str) -> None:
        """Drop a snapshot's file list and its rebuilt directory."""
        value = _hex(content_digest)
        if value is None:
            return
        (self.snapshots / f"{value}.json").unlink(missing_ok=True)
        shutil.rmtree(self.trees / value, ignore_errors=True)

    def files(self, content_digest: str) -> list[dict] | None:
        value = _hex(content_digest)
        try:
            return json.loads((self.snapshots / f"{value}.json").read_text()) if value else None
        except (OSError, ValueError):
            return None

    def path_for(self, content_digest: str) -> Path | None:
        """The rebuilt directory, if there is one; :meth:`verified` builds it."""
        value = _hex(content_digest)
        path = self.trees / value if value else None
        return path if path is not None and path.is_dir() else None

    def verified(self, content_digest: str) -> Path | None:
        """The snapshot's directory, rebuilt from the stored contents when missing, and
        only if its files match the digest."""
        value = _hex(content_digest)
        if value is None:
            return None
        self.trees.mkdir(parents=True, exist_ok=True)
        # Re-check after locking: another cold-cache caller may have already returned
        # this directory to a Runner. Never remove that freshly built directory.
        with (self.trees / f".{value}.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            return self._verified(content_digest)

    def _verified(self, content_digest: str) -> Path | None:
        path = self.path_for(content_digest)
        try:
            if path is not None and files_digest(describe(path)) == content_digest:
                return path
        except (OSError, ValueError):
            pass
        files = self.files(content_digest)
        if not files or files_digest(files) != content_digest:
            return None
        self.trees.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".build-", dir=self.trees))
        try:
            for entry in files:
                blob = self.blob(entry["sha256"])
                if blob is None:
                    return None
                target = staging / entry["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(blob, target)
                os.chmod(target, 0o755 if entry["executable"] else 0o644)
            if files_digest(describe(staging)) != content_digest:
                return None
            target = self.trees / _hex(content_digest)
            shutil.rmtree(target, ignore_errors=True)
            os.rename(staging, target)
            return self.path_for(content_digest)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def discard_partial(self) -> int:
        """At start, before any upload: uploads and removals left by a process that
        stopped mid-way, half-built directories, and package directories from before
        snapshots (N-06), whose rows the 0005 migration removed."""
        removed = 0
        for entry in self.root.iterdir() if self.root.is_dir() else []:
            stale = entry.name.startswith((".upload-", ".receive-", ".remove-", ".archive-"))
            legacy = entry.is_dir() and SHA256.fullmatch(entry.name)
            if stale or legacy:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
                removed += 1
        for entry in self.trees.iterdir() if self.trees.is_dir() else []:
            if entry.name.startswith(".build-"):
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        return removed
