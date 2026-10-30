"""Release tooling (N-02, N-16, N-25): the license rules, the SDK version stamp, and
publishing to Gitea Releases and the Python package registry against fake APIs. Building a
real package runs in test_install.py (opt-in); the license list itself runs in CI."""

import hashlib
import importlib.util
import json
import re
import threading
import urllib.parse
import zipfile
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


licenses = _load("third_party_licenses")
publish = _load("publish_release")
package = _load("publish_package")
build = _load("build_release")


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"expression": "Apache-2.0 OR BSD-3-Clause"}, "Apache-2.0 OR BSD-3-Clause"),
        ({"classifiers": ["License :: OSI Approved :: MIT License"]}, "MIT"),
        (
            {"classifiers": ["License :: OSI Approved :: BSD License", "License :: Apache"]},
            "BSD AND Apache",
        ),
        ({"license": "MPL-2.0 AND MIT"}, "MPL-2.0 AND MIT"),
        ({"license": "Apache License, Version 2.0"}, "Apache-2.0"),
        ({"license": "MIT License\n\nCopyright (c) 2026 someone\n..."}, "MIT"),
        ({"license": "Some custom terms\nline two"}, None),
        ({}, None),
    ],
)
def test_license_fields_become_spdx(fields, expected):
    assert licenses.spdx(**fields) == expected


@pytest.mark.parametrize(
    ("expression", "ok"),
    [
        ("MIT", True),
        ("MPL-2.0 AND MIT", True),
        ("GPL-3.0-only OR MIT", True),  # either one may be chosen
        ("GPL-3.0-only", False),
        ("LGPL-2.1-or-later AND MIT", False),
        ("(MIT OR GPL-2.0-only) AND BSD-3-Clause", False),  # nested: every identifier must pass
        ("BSD AND Apache", False),  # an unknown classifier text is refused
        (None, False),
    ],
)
def test_only_allowed_licenses_pass(expression, ok):
    assert licenses.allowed(expression) is ok


def test_only_dev_prereleases_beyond_the_newest_are_stale():
    releases = [
        {"tag_name": f"dev-{n}", "prerelease": True, "created_at": f"2026-10-{n:02d}"}
        for n in range(1, 6)
    ] + [
        {"tag_name": "v0.4.0", "prerelease": False, "created_at": "2026-10-01"},
        {"tag_name": "v0.5.0-rc1", "prerelease": True, "created_at": "2026-10-01"},
    ]
    stale = publish.stale_prereleases(releases, keep=2)
    assert [r["tag_name"] for r in stale] == ["dev-3", "dev-2", "dev-1"]


class FakeGitea(BaseHTTPRequestHandler):
    """Just the release endpoints publish_release.py uses."""

    state: dict

    def log_message(self, *args):
        pass

    def _reply(self, code, body=None):
        raw = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _route(self, method):
        s = self.state
        s["auth"].add(self.headers.get("Authorization"))
        path = self.path.removeprefix("/api/v1/repos/o/r")
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if method == "GET" and (m := re.fullmatch(r"/releases/tags/(.+)", path)):
            found = [r for r in s["releases"] if r["tag_name"] == m[1]]
            return self._reply(200, found[0]) if found else self._reply(404, {})
        if method == "GET" and (m := re.fullmatch(r"/releases\?limit=50&page=(\d+)", path)):
            return self._reply(200, s["releases"] if m[1] == "1" else [])
        if method == "POST" and path == "/releases":
            data = json.loads(body)
            s["created"] += 1
            release = {**data, "id": s["created"], "assets": []}
            release["created_at"] = f"2026-10-28T{s['created']:03d}"
            s["releases"].append(release)
            return self._reply(201, release)
        if method == "POST" and (m := re.fullmatch(r"/releases/(\d+)/assets\?name=(.+)", path)):
            release = next(r for r in s["releases"] if r["id"] == int(m[1]))
            assert b'name="attachment"' in body
            release["assets"].append({"id": len(s["uploads"]) + 1, "name": m[2]})
            s["uploads"].append(m[2])
            return self._reply(201, {})
        if method == "DELETE" and (m := re.fullmatch(r"/releases/(\d+)/assets/(\d+)", path)):
            release = next(r for r in s["releases"] if r["id"] == int(m[1]))
            release["assets"] = [a for a in release["assets"] if a["id"] != int(m[2])]
            return self._reply(204)
        if method == "DELETE" and (m := re.fullmatch(r"/releases/(\d+)", path)):
            s["releases"] = [r for r in s["releases"] if r["id"] != int(m[1])]
            return self._reply(204)
        if method == "DELETE" and (m := re.fullmatch(r"/tags/(.+)", path)):
            s["deleted_tags"].append(m[1])
            return self._reply(204)
        return self._reply(500, {"unexpected": f"{method} {path}"})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")


@pytest.fixture
def gitea(monkeypatch):
    state = {"releases": [], "created": 0, "uploads": [], "deleted_tags": [], "auth": set()}
    handler = type("Handler", (FakeGitea,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("GITEA_API", f"http://127.0.0.1:{server.server_port}/api/v1")
    monkeypatch.setenv("GITEA_REPO", "o/r")
    monkeypatch.setenv("GITEA_TOKEN", "t0ken")
    yield state
    server.shutdown()


def test_prereleases_upload_rerun_and_prune(gitea, tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "maestro-0.4.0.dev0-abc.tar.gz").write_bytes(b"package")
    (dist / "SHA256SUMS").write_text("sums\n")
    for n in range(12):
        args = ["--dist", str(dist), "--tag", f"dev-{n:03d}", "--commit", "c" * 40]
        assert publish.main([*args, "--prerelease", "--keep", "10"]) == 0
    assert [r["tag_name"] for r in gitea["releases"]] == [f"dev-{n:03d}" for n in range(2, 12)]
    assert gitea["deleted_tags"] == ["dev-000", "dev-001"]
    assert gitea["auth"] == {"token t0ken"}

    # Re-running the same job replaces the assets instead of failing or duplicating them.
    publish.main(["--dist", str(dist), "--tag", "dev-011", "--commit", "c" * 40, "--prerelease"])
    latest = gitea["releases"][-1]
    assert sorted(a["name"] for a in latest["assets"]) == [
        "SHA256SUMS",
        "maestro-0.4.0.dev0-abc.tar.gz",
    ]


def test_formal_release_is_never_pruned(gitea, tmp_path):
    (tmp_path / "pkg.tar.gz").write_bytes(b"package")
    notes = tmp_path / "notes.md"
    notes.write_text("- change one\n")
    args = ["--dist", str(tmp_path), "--tag", "v0.4.0", "--commit", "c" * 40, "--notes", str(notes)]
    assert publish.main(args) == 0
    (release,) = gitea["releases"]
    assert release["prerelease"] is False
    assert release["body"] == "- change one\n"
    assert release["target_commitish"] == "c" * 40


def test_the_sdk_wheel_is_stamped_without_changing_the_checkout(monkeypatch, tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "maestro-sdk"\nversion = "0.4.1.dev0"\n')
    monkeypatch.setattr(build, "SDK_PYPROJECT", pyproject)
    seen = []

    def fake_wheel(project, out):
        seen.append(build.SDK_VERSION.search(pyproject.read_text())[1])
        raise RuntimeError("build failed")

    monkeypatch.setattr(build, "wheel", fake_wheel)
    with pytest.raises(RuntimeError):
        build.sdk_wheel_as("0.4.1.dev57", tmp_path)
    assert seen == ["0.4.1.dev57"]
    assert 'version = "0.4.1.dev0"' in pyproject.read_text()  # restored even on failure


def _sdk_wheel(directory: Path, version: str, content: bytes = b"") -> Path:
    path = directory / f"maestro_sdk-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("maestro_sdk/__init__.py", content)
        archive.writestr(
            f"maestro_sdk-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: maestro-sdk\nVersion: {version}\n"
            "Summary: Python client\nRequires-Python: >=3.10\n",
        )
    return path


class FakeRegistry(BaseHTTPRequestHandler):
    """Just the package endpoints publish_package.py uses, as Gitea answers them."""

    state: dict

    def log_message(self, *args):
        pass

    def _reply(self, code, body=b"", content_type="application/json"):
        raw = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _route(self, method):
        s = self.state
        s["auth"].add(self.headers.get("Authorization"))
        url = urllib.parse.urlsplit(self.path)
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if method == "POST" and url.path == "/api/packages/o/pypi":
            head = f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode()
            form = BytesParser(policy=HTTP).parsebytes(head + body)
            fields = {
                p.get_param("name", header="content-disposition"): p for p in form.iter_parts()
            }
            content = fields.pop("content")
            data = {k: v.get_content() for k, v in fields.items()}
            assert data[":action"] == "file_upload" and data["requires_python"] == ">=3.10"
            raw = content.get_content()
            if data["sha256_digest"] != hashlib.sha256(raw).hexdigest():
                return self._reply(400, {"message": "hash mismatch"})
            files = s["files"].setdefault(data["version"], {})
            if content.get_filename() in files:
                return self._reply(409, {"message": "package file already exists"})
            files[content.get_filename()] = data["sha256_digest"]
            s["created"] += 1
            s["created_at"][data["version"]] = f"2026-10-30T{s['created']:03d}"
            return self._reply(201)
        if method == "GET" and url.path == "/api/packages/o/pypi/simple/maestro-sdk":
            links = "".join(
                f'<a href="/files/{name}#sha256={digest}">{name}</a>'
                for files in s["files"].values()
                for name, digest in files.items()
            )
            return self._reply(200, links.encode(), "text/html")
        if method == "GET" and url.path == "/api/v1/packages/o":
            query = urllib.parse.parse_qs(url.query)
            assert query["type"] == ["pypi"] and query["q"] == ["maestro-sdk"]
            versions = [
                {"name": "maestro-sdk", "version": v, "created_at": s["created_at"][v]}
                for v in s["files"]
            ] + [{"name": "maestro-sdk-extra", "version": "0.1.0.dev1", "created_at": "2026"}]
            return self._reply(200, versions if query["page"] == ["1"] else [])
        if method == "DELETE" and (
            m := re.fullmatch(r"/api/v1/packages/o/pypi/maestro-sdk/(.+)", url.path)
        ):
            del s["files"][urllib.parse.unquote(m[1])]
            return self._reply(204)
        return self._reply(500, {"unexpected": f"{method} {self.path}"})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")


@pytest.fixture
def registry(monkeypatch):
    state = {"files": {}, "created": 0, "created_at": {}, "auth": set()}
    handler = type("Handler", (FakeRegistry,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("GITEA_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("GITEA_OWNER", "o")
    monkeypatch.setenv("GITEA_TOKEN", "t0ken")
    yield state
    server.shutdown()


def test_dev_versions_upload_rerun_and_prune(registry, tmp_path, capsys):
    release = tmp_path / "release"
    release.mkdir()
    _sdk_wheel(release, "0.4.0")
    assert package.main(["--dist", str(release)]) == 0  # a formal version, never pruned
    for n in range(1, 13):
        dist = tmp_path / f"dev{n}"
        dist.mkdir()
        wheel = _sdk_wheel(dist, f"0.4.1.dev{n}", content=str(n).encode())
        (dist / "SHA256SUMS").write_text("other assets are ignored\n")
        assert package.main(["--dist", str(dist), "--keep", "10"]) == 0
    assert set(registry["files"]) == {"0.4.0"} | {f"0.4.1.dev{n}" for n in range(3, 13)}
    assert registry["auth"] == {"token t0ken"}

    # Re-running the job finds the same file and succeeds; other content is refused.
    capsys.readouterr()
    assert package.main(["--dist", str(wheel.parent)]) == 0
    assert "already published" in capsys.readouterr().out
    _sdk_wheel(wheel.parent, "0.4.1.dev12", content=b"changed")
    with pytest.raises(SystemExit, match="already published"):
        package.main(["--dist", str(wheel.parent)])


def test_only_one_sdk_wheel_is_published(registry, tmp_path):
    with pytest.raises(SystemExit, match="exactly one SDK wheel"):
        package.main(["--dist", str(tmp_path)])
    _sdk_wheel(tmp_path, "0.4.0")
    _sdk_wheel(tmp_path, "0.4.1.dev1")
    with pytest.raises(SystemExit, match="exactly one SDK wheel"):
        package.main(["--dist", str(tmp_path)])
    assert registry["files"] == {}
