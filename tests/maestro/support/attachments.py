"""Shared attachments helpers for tests."""

import hashlib
import json

from maestro.execution.attachments import AttachmentStore

BOOTSTRAP = b"#!/bin/bash\nset -e\necho installed\n"


def manifest(name="tools", **extra) -> dict:
    return {
        "schema_version": 1,
        "name": name,
        "effects": ["agent_tools"],
        "compatible_harnesses": ["fake-agent"],
        **extra,
    }


def package(name="tools", bootstrap=True, extra_files=None, **fields) -> dict:
    """path -> bytes, or (bytes, mode)."""
    files = {"manifest.json": json.dumps(manifest(name, **fields)).encode()}
    if bootstrap:
        files["bootstrap.sh"] = (BOOTSTRAP, 0o755)
    files["tools/hello"] = (b"#!/bin/sh\necho hello\n", 0o755)
    files.update(extra_files or {})
    return files


def submission_package(name="deliverables", script=BOOTSTRAP, extra_files=None) -> dict:
    return {
        "manifest.json": json.dumps(
            {"schema_version": 1, "name": name, "kind": "submission"}
        ).encode(),
        "bootstrap.sh": (script, 0o755),
        **(extra_files or {}),
    }


def entries(files: dict) -> list[dict]:
    """The file list a client commits, and each content by its SHA-256."""
    listed = []
    for path, content in files.items():
        data, mode = content if isinstance(content, tuple) else (content, 0o644)
        listed.append(
            {
                "path": path,
                "sha256": hashlib.sha256(data).hexdigest(),
                "executable": bool(mode & 0o100),
                "data": data,
            }
        )
    return listed


def upload(api, data: bytes, sha256: str | None = None):
    return api.client.put(
        f"/api/v1/attachment-contents/{sha256 or hashlib.sha256(data).hexdigest()}",
        content=data,
        headers=api._headers({"Content-Type": "application/octet-stream"}),
    )


def publish(api, files: dict, expect=None, label=None, parent_id=None, uploaded=None):
    """What a client does: ask what is missing, upload that, commit the list. Contents
    uploaded are appended to ``uploaded``."""
    listed = entries(files)
    asked = api.post(
        "/api/v1/attachment-contents/missing",
        json={"sha256": sorted({f["sha256"] for f in listed})},
    )
    if asked.status_code != 200:
        return asked
    contents = {f["sha256"]: f["data"] for f in listed}
    for sha in asked.json()["data"]["missing"]:
        response = upload(api, contents[sha], sha)
        assert response.status_code == 204, response.text
        if uploaded is not None:
            uploaded.append(sha)
    body = {
        "files": [{k: f[k] for k in ("path", "sha256", "executable")} for f in listed],
        "label": label,
        "parent_id": parent_id,
    }
    return api.post("/api/v1/attachments", json=body, expect=expect)


def store_package(store: AttachmentStore, files: dict) -> str:
    """Put a snapshot straight into a store, as a publish would; its digest."""
    store.blobs.mkdir(parents=True, exist_ok=True)
    for entry in entries(files):
        (store.blobs / entry["sha256"]).write_bytes(entry["data"])
    package = store.check(
        [{k: f[k] for k in ("path", "sha256", "executable")} for f in entries(files)],
        max_files=20,
        max_bytes=100_000,
    )
    store.record(package)
    return package.content_digest


def attach(api, label=None, **kw) -> str:
    return publish(api, package(**kw), label=label, expect=201).json()["data"]["id"]
