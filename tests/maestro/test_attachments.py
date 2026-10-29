"""Attachment snapshots (03 sections 6-7, N-06): published file by file with only the
missing contents uploaded, identified by content, checked on commit, resolved in load
order into run configurations, and kept out of new runs once deleted."""

import hashlib
import io
import json
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from support import (
    create_pat,
    invite_member,
    running,
    setup_admin,
    submit,
    wait_operation,
)
from test_configuration import agent_body, codes, error, resolve
from test_e2e_run import prepare_model
from test_recycle import bin_entry, purge

from maestro.core import clock
from maestro.db import schema as t
from maestro.execution.attachments import AttachmentStore, PackageError, check_manifest

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


def problems(error: PackageError) -> set[tuple[str, str]]:
    return {(path, code) for path, code, _ in error.problems}


# --- snapshot checks -----------------------------------------------------------------


@pytest.fixture
def contents(tmp_path):
    return AttachmentStore(tmp_path / "data", tmp_path / "cache")


def check(store, files: dict, listed=None):
    for entry in entries(files):
        store.blobs.mkdir(parents=True, exist_ok=True)
        (store.blobs / entry["sha256"]).write_bytes(entry["data"])
    listed = listed or [{k: f[k] for k in ("path", "sha256", "executable")} for f in entries(files)]
    return store.check(listed, max_files=20, max_bytes=10_000)


def test_the_digest_covers_paths_contents_and_modes_in_any_order(contents):
    files = {
        "manifest.json": json.dumps(manifest()).encode(),
        "bootstrap.sh": (BOOTSTRAP, 0o755),
        "tools/hello": (b"hello\n", 0o644),
    }
    first = check(contents, files)
    reordered = check(contents, dict(reversed(list(files.items()))))
    assert first.content_digest == reordered.content_digest
    assert first.has_bootstrap and first.manifest["provides"] == {"tools": [], "env": []}
    assert [f["path"] for f in first.files] == ["bootstrap.sh", "manifest.json", "tools/hello"]
    assert [f["size"] for f in first.files][1] == len(files["manifest.json"])
    # The mode is content: the same bytes without the executable bit are another snapshot.
    files["tools/hello"] = (b"hello\n", 0o755)
    assert check(contents, files).content_digest != first.content_digest


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("/etc/cron.d/x", "PATH"),
        ("../escape", "PATH"),
        ("a/../../escape", "PATH"),
        ("has space", "PATH"),
        ("dir/", "PATH"),
    ],
)
def test_unsafe_paths_are_refused(contents, path, code):
    with pytest.raises(PackageError) as raised:
        check(contents, {"manifest.json": json.dumps(manifest()).encode(), path: b"x"})
    assert code in {c for _, c in problems(raised.value)}


def test_duplicates_sizes_counts_and_missing_contents_are_refused(contents):
    base = {"manifest.json": json.dumps(manifest()).encode()}
    listed = [{k: f[k] for k in ("path", "sha256", "executable")} for f in entries(base)]
    with pytest.raises(PackageError) as raised:
        check(contents, base, listed + listed)
    assert ("/files/manifest.json", "DUPLICATE") in problems(raised.value)
    with pytest.raises(PackageError) as raised:
        check(contents, {**base, "a": b"x", "a/b": b"y"})
    assert ("/files/a/b", "DUPLICATE") in problems(raised.value)
    with pytest.raises(PackageError) as raised:
        check(contents, {**base, "big": b"x" * 10_001})
    assert ("/files/", "TOO_LARGE") in problems(raised.value)
    with pytest.raises(PackageError, match="too many"):
        check(contents, {**base, **{f"f{i}": b"" for i in range(21)}})
    absent = listed + [{"path": "gone", "sha256": "0" * 64, "executable": False}]
    with pytest.raises(PackageError) as raised:
        check(contents, base, absent)
    assert ("/files/gone", "MISSING") in problems(raised.value)
    with pytest.raises(PackageError, match="no manifest"):
        check(contents, {"bootstrap.sh": BOOTSTRAP})
    with pytest.raises(PackageError, match="no files"):
        contents.check([], max_files=20, max_bytes=10_000)


def test_the_manifest_is_checked_strictly():
    def fails(**fields):
        body = {k: v for k, v in {**manifest(), **fields}.items() if v is not None}
        with pytest.raises(PackageError) as raised:
            check_manifest(body)
        return problems(raised.value)

    assert fails(version="1.0.0") == {("/manifest/version", "UNKNOWN_FIELD")}
    assert fails(effects=["verifier"]) == {("/manifest/effects", "UNSUPPORTED")}
    assert fails(effects=["telepathy"]) == {("/manifest/effects", "ENUM")}
    assert fails(parameter_schema={}) == {("/manifest/parameter_schema", "UNSUPPORTED")}
    assert fails(surprise=1) == {("/manifest/surprise", "UNKNOWN_FIELD")}
    assert fails(name="../x", compatible_harnesses=[]) == {
        ("/manifest/name", "PATTERN"),
        ("/manifest/compatible_harnesses", "INVALID"),
    }
    assert fails(provides={"tools": ["a", "a"]}) == {("/manifest/provides/tools", "INVALID")}


def test_a_snapshot_directory_is_rebuilt_and_checked(contents):
    digest = store_package(contents, package())
    built = contents.verified(digest)
    assert built is not None and built.parent == contents.trees
    assert (built / "bootstrap.sh").read_bytes() == BOOTSTRAP
    assert (built / "tools" / "hello").stat().st_mode & 0o100
    # A changed copy is rebuilt; a lost content means no directory at all.
    (built / "bootstrap.sh").write_bytes(b"tampered")
    assert (contents.verified(digest) / "bootstrap.sh").read_bytes() == BOOTSTRAP
    sha = hashlib.sha256(BOOTSTRAP).hexdigest()
    (contents.blobs / sha).unlink()
    (built / "bootstrap.sh").write_bytes(b"tampered")
    assert contents.verified(digest) is None


# --- publishing ------------------------------------------------------------------------


def test_members_publish_snapshots_uploading_only_what_is_missing(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "member")
        pat = create_pat(member)
        reader = create_pat(member, scope="read")
        assert publish(reader, package()).status_code == 403
        assert upload(reader, b"x").status_code == 403
        uploaded = []
        created = publish(pat, package(), label="v1", uploaded=uploaded, expect=201)
        created = created.json()["data"]
        assert len(uploaded) == 3
        assert created["owner_username"] == "member" and created["has_bootstrap"]
        assert created["label"] == "v1" and created["parent_id"] is None
        assert created["file_count"] == 3 and created["files"][0]["path"] == "bootstrap.sh"
        # The same content again: the same snapshot, nothing uploaded, the label kept.
        uploaded.clear()
        again = publish(member, package(), label="other", uploaded=uploaded, expect=200)
        assert again.json()["data"]["id"] == created["id"] and uploaded == []
        assert again.json()["data"]["label"] == "v1"
        # One file changed: only it is uploaded, and the result is a new snapshot.
        changed_files = package(extra_files={"README": b"new"})
        child = publish(
            member, changed_files, parent_id=created["id"], uploaded=uploaded, expect=201
        ).json()["data"]
        assert uploaded == [hashlib.sha256(b"new").hexdigest()]
        assert child["parent_id"] == created["id"] and child["label"] is None
        # Snapshots branch: another child of the same parent.
        sibling = publish(
            member, package(extra_files={"README": b"other"}), parent_id=created["id"], expect=201
        ).json()["data"]
        assert sibling["id"] != child["id"]
        taken = publish(member, package(extra_files={"README": b"third"}), label="v1")
        assert error(taken) == (409, "ATTACHMENT_LABEL_TAKEN")
        orphan = publish(member, package(extra_files={"README": b"x"}), parent_id="att_none")
        assert error(orphan) == (422, "VALIDATION_FAILED")
        unknown = publish(member, package("other", compatible_harnesses=["opencode"]))
        assert error(unknown) == (422, "ATTACHMENT_INVALID")
        bad = publish(member, package(effects=["scoring"]))
        assert bad.json()["error"]["field_errors"][0]["path"] == "/manifest/effects"
        listed = reader.get("/api/v1/attachments", expect=200).json()["data"]
        assert [a["id"] for a in listed] == [created["id"], child["id"], sibling["id"]]
        assert listed[0]["files"] is None
        # The owner or an administrator disables; another member cannot.
        other = invite_member(service, admin, "other")
        path = f"/api/v1/attachments/{created['id']}/enabled"
        assert other.post(path, json={"enabled": False}).status_code == 403
        assert not pat.post(path, json={"enabled": False}, expect=200).json()["data"]["enabled"]
        assert admin.post(path, json={"enabled": True}, expect=200).json()["data"]["enabled"]
    blobs = service.settings.data_dir / "attachments" / "blobs"
    assert (blobs / hashlib.sha256(BOOTSTRAP).hexdigest()).read_bytes() == BOOTSTRAP
    for content in (b"new", b"other"):
        assert (blobs / hashlib.sha256(content).hexdigest()).read_bytes() == content


def test_only_the_uploader_may_name_an_unpublished_content(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "member")
        listed = entries(package())
        for entry in listed:
            assert upload(member, entry["data"]).status_code == 204
        body = {"files": [{k: f[k] for k in ("path", "sha256", "executable")} for f in listed]}
        shas = sorted({f["sha256"] for f in listed})
        missing = admin.post("/api/v1/attachment-contents/missing", json={"sha256": shas})
        assert sorted(missing.json()["data"]["missing"]) == shas
        refused = admin.post("/api/v1/attachments", json=body)
        assert error(refused) == (409, "ATTACHMENT_CONTENT_MISSING")
        assert member.post("/api/v1/attachments", json=body).status_code == 201
        # Published, the contents are there for everyone.
        missing = admin.post("/api/v1/attachment-contents/missing", json={"sha256": shas})
        assert missing.json()["data"]["missing"] == []


def test_uploads_are_checked_limited_and_expire(service, monkeypatch):
    from maestro.domain import limits

    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        assert error(upload(admin, b"one", "0" * 64)) == (422, "ATTACHMENT_DIGEST_MISMATCH")
        assert error(upload(admin, b"one", "XYZ")) == (422, "VALIDATION_FAILED")
        monkeypatch.setattr(limits, "ATTACHMENT_FILE_MAX_BYTES", 100)
        assert error(upload(admin, b"x" * 101)) == (413, "ATTACHMENT_TOO_LARGE")
        streamed = admin.client.put(
            f"/api/v1/attachment-contents/{hashlib.sha256(b'x' * 120).hexdigest()}",
            content=iter([b"x" * 60, b"x" * 60]),  # no Content-Length: counted while read
            headers=admin._headers({"Content-Type": "application/octet-stream"}),
        )
        assert error(streamed) == (413, "ATTACHMENT_TOO_LARGE")
        monkeypatch.setattr(limits, "ATTACHMENT_FILE_MAX_BYTES", 1 << 20)
        monkeypatch.setattr(limits, "ATTACHMENT_STAGED_MAX_BYTES", 10)
        assert upload(admin, b"12345678").status_code == 204
        assert upload(admin, b"12345678").status_code == 204  # same SHA, no extra quota
        streamed = admin.client.put(
            f"/api/v1/attachment-contents/{hashlib.sha256(b'abcdefgh').hexdigest()}",
            content=iter([b"abcd", b"efgh"]),
            headers=admin._headers({"Content-Type": "application/octet-stream"}),
        )
        assert error(streamed) == (413, "ATTACHMENT_TOO_LARGE")
        assert error(upload(admin, b"abcdefgh")) == (413, "ATTACHMENT_TOO_LARGE")
        bad = admin.post("/api/v1/attachment-contents/missing", json={"sha256": ["nope"]})
        assert error(bad) == (422, "VALIDATION_FAILED")
    root = service.settings.data_dir / "attachments"
    assert [p.name for p in root.iterdir()] == ["blobs"]
    kept = hashlib.sha256(b"12345678").hexdigest()
    assert [p.name for p in (root / "blobs").iterdir()] == [kept]
    # Unpublished, the upload expires and its content goes.
    later = clock.now() + timedelta(hours=25)
    monkeypatch.setattr(clock, "now", lambda: later)
    assert service.attachments.sweep_uploads() == 1
    assert list((root / "blobs").iterdir()) == []


def test_reading_files_archives_and_differences(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        reader = create_pat(admin, scope="read")
        first = attach(admin, label="v1")
        second = publish(
            admin,
            package(extra_files={"README": b"new", "tools/hello": (b"changed\n", 0o755)}),
            parent_id=first,
            expect=201,
        ).json()["data"]["id"]
        hello = reader.get(f"/api/v1/attachments/{first}/files/tools/hello", expect=200)
        assert hello.content == b"#!/bin/sh\necho hello\n"
        assert hello.headers["X-Executable"] == "true"
        assert hello.headers["X-Content-Digest"].startswith("sha256:")
        assert reader.get(f"/api/v1/attachments/{first}/files/nope").status_code == 404
        archive = reader.get(f"/api/v1/attachments/{first}/archive", expect=200)
        with zipfile.ZipFile(io.BytesIO(archive.content)) as opened:
            names = {i.filename: i.external_attr >> 16 for i in opened.infolist()}
            assert opened.read("tools-v1/bootstrap.sh") == BOOTSTRAP
        assert names["tools-v1/bootstrap.sh"] & 0o100
        assert not names["tools-v1/manifest.json"] & 0o100
        diff = reader.get(f"/api/v1/attachments/{second}/diff", expect=200).json()["data"]
        assert diff["base_id"] == first
        assert [f["path"] for f in diff["added"]] == ["README"]
        assert [c["path"] for c in diff["changed"]] == ["tools/hello"] and diff["removed"] == []
        back = reader.get(f"/api/v1/attachments/{first}/diff?base_id={second}").json()["data"]
        assert [f["path"] for f in back["removed"]] == ["README"]
        alone = reader.get(f"/api/v1/attachments/{first}/diff").json()["data"]
        assert alone["base_id"] is None and len(alone["added"]) == 3
    assert sorted(p.name for p in (service.settings.data_dir / "attachments").iterdir()) == [
        "blobs",
        "snapshots",
    ]


# --- resolution ------------------------------------------------------------------------


def attach(api, label=None, **kw) -> str:
    return publish(api, package(**kw), label=label, expect=201).json()["data"]["id"]


def test_attachments_resolve_in_order_with_sources_and_limits(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        model_id, harness_id = prepare_model(admin)
        member = invite_member(service, admin, "member")
        first = attach(member, name="sdk", provides={"env": ["SDK_HOME"]})
        second = attach(member, name="tools", bootstrap=False, provides={"tools": ["hello"]})
        body = agent_body(model_id, harness_id, attachments=[second, first])
        resolution = resolve(member, version_id, **body)
        assert resolution["blockers"] == []
        effective = resolution["effective_config"]
        assert [a["name"] for a in effective["attachments"]] == ["tools", "sdk"]
        assert effective["attachments"][1]["provides"] == {"env": ["SDK_HOME"], "tools": []}
        assert effective["bootstrap_timeout_sec"] == 600
        assert resolution["field_sources"]["/attachments"] == {"layer": "override"}
        assert resolution["field_sources"]["/timeouts/bootstrap_sec"]["layer"] == "team"
        # The order is part of the configuration.
        swapped = resolve(
            member, version_id, **agent_body(model_id, harness_id, attachments=[first, second])
        )
        assert swapped["config_digest"] != resolution["config_digest"]
        over = resolve(member, version_id, **body, timeouts={"bootstrap_sec": 4000})
        assert codes(over) == ["LIMIT_EXCEEDED"] and over["blockers"][0]["limit"] == 3600
        quiet = resolve(
            member, version_id, **agent_body(model_id, harness_id, attachments=[second])
        )
        assert quiet["effective_config"]["bootstrap_timeout_sec"] is None
        oracle = member.post(
            "/api/v1/config-resolutions",
            json={
                "dataset_version_id": version_id,
                "execution_mode": "oracle",
                "attachments": [first],
            },
        )
        assert oracle.json()["error"]["field_errors"][0]["code"] == "ORACLE_HAS_NO_ATTACHMENTS"


def test_attachment_blockers(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        model_id, harness_id = prepare_model(admin)
        service.attachments.adapters = None  # publish for a harness this executor lacks
        tools = attach(admin, name="tools", provides={"tools": ["hello"]})
        clash = attach(admin, name="more-tools", provides={"tools": ["hello"]})
        newer = attach(admin, name="tools", label="v2", extra_files={"NEWS": b"two"})
        elsewhere = attach(admin, name="elsewhere", compatible_harnesses=["opencode"])
        anywhere = attach(admin, name="anywhere", compatible_harnesses=["*"])
        off = attach(admin, name="off")
        admin.post(f"/api/v1/attachments/{off}/enabled", json={"enabled": False}, expect=200)

        def blockers(*ids):
            found = resolve(
                admin, version_id, **agent_body(model_id, harness_id, attachments=list(ids))
            )
            return [(b["code"], b["path"]) for b in found["blockers"]]

        assert blockers(tools, anywhere) == []
        assert blockers(tools, clash) == [("ATTACHMENT_CONFLICT", "/attachments/1")]
        assert blockers(tools, newer) == [("ATTACHMENT_CONFLICT", "/attachments/1")]
        assert blockers(elsewhere) == [("ATTACHMENT_INCOMPATIBLE", "/attachments/0")]
        assert blockers(off) == [("DISABLED", "/attachments/0")]
        assert blockers("att_missing") == [("NOT_FOUND", "/attachments/0")]
        admin.put(
            "/api/v1/admin/team-policy",
            json={"values": {"run_max_attachments": 1}, "expected_revision": 0},
            expect=200,
        )
        assert blockers(tools, anywhere)[0] == ("LIMIT_EXCEEDED", "/attachments")


def test_runs_record_their_attachments_and_disabling_before_acceptance_refuses(service, version_id):
    with running(service) as client:
        admin = setup_admin(service, client)
        model_id, harness_id = prepare_model(admin)
        member = invite_member(service, admin, "member")
        tools = attach(member)
        body = agent_body(model_id, harness_id, attachments=[tools], timeouts={"bootstrap_sec": 90})
        accepted = submit(member, version_id, **body).json()["data"]
        wait_operation(member, accepted["operation_id"])
        run = member.get(f"/api/v1/runs/{accepted['run_id']}").json()["data"]
        effective = run["snapshot"]["effective"]
        assert effective["bootstrap_timeout_sec"] == 90
        assert effective["attachments"][0]["version_id"] == tools
        assert run["snapshot"]["provenance"]["reference_revisions"]["attachments"] == [tools]
        found = member.get(f"/api/v1/runs?attachment_version_id={tools}").json()["data"]
        assert [r["id"] for r in found] == [accepted["run_id"]]

        resolution = resolve(member, version_id, **body)
        member.post(f"/api/v1/attachments/{tools}/enabled", json={"enabled": False}, expect=200)
        refused = member.post(
            "/api/v1/runs",
            json={"name": "late", "resolution_id": resolution["resolution_id"]},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert error(refused) == (422, "CONFIG_UNAVAILABLE")
        assert refused.json()["error"]["field_errors"][0]["path"] == "/attachments/0"


def test_disabling_during_acceptance_is_caught_in_the_transaction(service, version_id, monkeypatch):
    from maestro.services.configuration import Resolver

    original = Resolver.resolve

    def resolve_then_disable(self, conn, request):
        resolved = original(self, conn, request)
        with service.db.write() as other:
            other.execute(t.attachment_versions.update().values(enabled=False))
        return resolved

    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        model_id, harness_id = prepare_model(admin)
        tools = attach(admin)
        monkeypatch.setattr(Resolver, "resolve", resolve_then_disable)
        refused = submit(admin, version_id, **agent_body(model_id, harness_id, attachments=[tools]))
        assert error(refused) == (409, "CONFIG_CHANGED")
        assert refused.json()["error"]["field_errors"][0]["path"] == "/attachments/0"


def test_deleting_and_purging_an_attachment(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        model_id, harness_id = prepare_model(admin)
        member = invite_member(service, admin, "member")
        other = invite_member(service, admin, "other")
        tools = attach(member)
        body = agent_body(model_id, harness_id, attachments=[tools])
        accepted = submit(member, version_id, **body).json()["data"]
        assert error(other.delete(f"/api/v1/attachments/{tools}")) == (403, "FORBIDDEN")
        busy = member.delete(f"/api/v1/attachments/{tools}")
        assert error(busy) == (409, "ATTACHMENT_IN_USE")
        member.post(f"/api/v1/runs/{accepted['run_id']}/cancel", expect=200)
        member.delete(f"/api/v1/attachments/{tools}", expect=200)
        assert member.get("/api/v1/attachments").json()["data"] == []
        assert codes(resolve(member, version_id, **body)) == ["DELETED"]
        # In the recycle bin, the same content cannot be published around it.
        assert error(publish(member, package())) == (409, "ATTACHMENT_DELETED")
        assert purge(member, {"id": "x", "object_id": tools}).status_code == 403
        purge(admin, bin_entry(admin, tools)).raise_for_status()
        assert admin.get(f"/api/v1/attachments/{tools}").status_code == 410
        assert admin.get(f"/api/v1/attachments/{tools}/files/bootstrap.sh").status_code == 410
        # The run keeps what it used; the contents are gone.
        run = admin.get(f"/api/v1/runs/{accepted['run_id']}").json()["data"]
        assert run["snapshot"]["effective"]["attachments"][0]["name"] == "tools"
    root = service.settings.data_dir / "attachments"
    assert list((root / "blobs").iterdir()) == [] and list((root / "snapshots").iterdir()) == []


def test_a_purge_keeps_contents_another_snapshot_still_holds(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        first = attach(admin)
        second = attach(admin, extra_files={"README": b"more"})
        admin.delete(f"/api/v1/attachments/{first}", expect=200)
        purge(admin, bin_entry(admin, first)).raise_for_status()
        blobs = service.settings.data_dir / "attachments" / "blobs"
        assert len(list(blobs.iterdir())) == 4  # the second still holds the first's three
        # A purged snapshot's files are gone for readers, though the contents remain.
        assert admin.get(f"/api/v1/attachments/{first}/files/bootstrap.sh").status_code == 410
        assert admin.get(f"/api/v1/attachments/{second}/files/bootstrap.sh").status_code == 200
        # Republishing the purged content is allowed and needs no upload.
        uploaded = []
        again = publish(admin, package(), uploaded=uploaded, expect=201).json()["data"]
        assert again["id"] != first and uploaded == []
        assert service.attachments.store.verified(again["content_digest"]) is not None


def test_leftovers_of_a_stopped_process_are_removed_at_start(service):
    root = service.settings.data_dir / "attachments"
    (root / ".remove-x").mkdir(parents=True)
    (root / ".receive-y").write_bytes(b"half an upload")
    (root / ".archive-z.zip").write_bytes(b"half an archive")
    (root / ("a" * 64)).mkdir()  # a package directory from before snapshots
    (root / "blobs").mkdir()
    (root / "blobs" / ("b" * 64)).write_bytes(b"kept")
    trees = service.attachments.store.trees
    (trees / ".build-q").mkdir(parents=True)
    assert service.maintenance.start()["attachment_staging_removed"] == 5
    assert [p.name for p in root.iterdir()] == ["blobs"]
    assert list(trees.iterdir()) == []


def test_simultaneous_uploads_recheck_remaining_quota_atomically(service, monkeypatch):
    from maestro.domain import limits

    monkeypatch.setattr(limits, "ATTACHMENT_STAGED_MAX_BYTES", 10)
    # Both requests observe the same empty staging area before either finishes reading.
    barrier = threading.Barrier(2)
    allowance = service.attachments.upload_allowance

    def together(*args):
        allowed = allowance(*args)
        barrier.wait(timeout=10)
        return allowed

    monkeypatch.setattr(service.attachments, "upload_allowance", together)
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda data: upload(admin, data), [b"12345678", b"abcdefgh"]))
        assert sorted(r.status_code for r in responses) == [204, 413]
        assert len(list(service.attachments.store.blobs.iterdir())) == 1


def test_concurrent_cold_cache_readers_share_one_stable_directory(contents, monkeypatch):
    import maestro.execution.attachments as module

    digest = store_package(contents, package())
    ready = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()
    builds = []
    mkdtemp = module.tempfile.mkdtemp

    def pause_first_build(*args, **kwargs):
        staging = mkdtemp(*args, **kwargs)
        builds.append(staging)
        if len(builds) == 1:
            ready.set()
            assert release.wait(timeout=10)
        return staging

    def second_reader():
        second_entered.set()
        return contents.verified(digest)

    monkeypatch.setattr(module.tempfile, "mkdtemp", pause_first_build)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(contents.verified, digest)
        assert ready.wait(timeout=10)
        second = pool.submit(second_reader)
        assert second_entered.wait(timeout=10)
        try:
            with pytest.raises(TimeoutError):
                second.result(timeout=0.1)
        finally:
            release.set()
        first_path = first.result(timeout=10)
        inode = first_path.stat().st_ino
        second_path = second.result(timeout=10)
    assert first_path == second_path and second_path.stat().st_ino == inode
    assert len(builds) == 1
    assert (first_path / "bootstrap.sh").read_bytes() == BOOTSTRAP
