"""Release tooling: the license rules, the SDK version stamp,
publishing to Gitea Releases and the Python package registry against fake APIs, and the
package index for GitHub Pages; the build an installed service reports. Building a
real package runs in live/test_install.py (opt-in); the license list itself runs in CI."""

import hashlib
import importlib.util
import json
import os
import re
import tarfile
import threading
import urllib.parse
import zipfile
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from support import ROOT


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


licenses = _load("third_party_licenses")
publish = _load("publish_release")
package = _load("publish_package")
build = _load("build_release")
index = _load("package_index")
gate = _load("verify_commit")
release_tool = _load("release")
validate = _load("validate_release")


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
        if path.startswith("/o/r/releases/download/"):
            path = path.removeprefix("/o/r")
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if method == "GET" and (m := re.fullmatch(r"/git/refs?/tags/(.+)", path)):
            sha = s["tags"].get(m[1])
            return (
                self._reply(
                    200, [{"ref": "refs/tags/" + m[1], "object": {"type": "commit", "sha": sha}}]
                )
                if sha
                else self._reply(404, {})
            )
        if method == "POST" and path == "/tags":
            data = json.loads(body)
            s["tags"][data["tag_name"]] = data["target"]
            return self._reply(201, {})
        if method == "GET" and (
            m := re.fullmatch(r"/(?:releases/download/[^/]+/|releases/assets/)(\d+)", path)
        ):
            raw = s["contents"][int(m[1])]
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if method == "GET" and (m := re.fullmatch(r"/releases/(\d+)", path)):
            return self._reply(200, next(r for r in s["releases"] if r["id"] == int(m[1])))
        if method == "PATCH" and (m := re.fullmatch(r"/releases/(\d+)", path)):
            release = next(r for r in s["releases"] if r["id"] == int(m[1]))
            release.update(json.loads(body))
            return self._reply(200, release)
        if method == "GET" and (m := re.fullmatch(r"/releases/tags/(.+)", path)):
            found = [r for r in s["releases"] if r["tag_name"] == m[1]]
            return self._reply(200, found[0]) if found else self._reply(404, {})
        if method == "GET" and (
            m := re.fullmatch(r"/releases\?limit=50&per_page=50&page=(\d+)", path)
        ):
            return self._reply(200, s["releases"] if m[1] == "1" else [])
        if method == "POST" and path == "/releases":
            data = json.loads(body)
            s["created"] += 1
            release = {
                **data,
                "id": s["created"],
                "assets": [],
                "html_url": "https://example/release",
            }
            s["created_drafts"].append(data["draft"])
            release["created_at"] = f"2026-10-28T{s['created']:03d}"
            s["releases"].append(release)
            return self._reply(201, release)
        if method == "POST" and (m := re.fullmatch(r"/releases/(\d+)/assets\?name=(.+)", path)):
            release = next(r for r in s["releases"] if r["id"] == int(m[1]))
            if self.headers["Content-Type"] == "application/octet-stream":
                attachment = body
            else:
                head = f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode()
                form = BytesParser(policy=HTTP).parsebytes(head + body)
                attachment = next(form.iter_parts()).get_content()
            asset_id = len(s["uploads"]) + 1
            if s["fail_name"] == m[2]:
                return self._reply(503, {})
            s["contents"][asset_id] = attachment
            release["assets"].append(
                {
                    "id": asset_id,
                    "name": m[2],
                    "browser_download_url": f"https://public.example/o/r/releases/download/{release['tag_name']}/{asset_id}",
                }
            )
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

    def do_PATCH(self):
        self._route("PATCH")


@pytest.fixture
def gitea(monkeypatch):
    state = {
        "releases": [],
        "created": 0,
        "uploads": [],
        "deleted_tags": [],
        "auth": set(),
        "tags": {"v0.4.0": "c" * 40},
        "contents": {},
        "created_drafts": [],
        "fail_name": None,
    }
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
    assert gitea["auth"] == {"Bearer t0ken"}

    # Re-running the same job reuses bytes without deleting or duplicating assets.
    uploads = len(gitea["uploads"])
    publish.main(["--dist", str(dist), "--tag", "dev-011", "--commit", "c" * 40, "--prerelease"])
    assert len(gitea["uploads"]) == uploads
    assert all(gitea["created_drafts"])
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


def test_the_github_index_links_every_release_with_its_digest(tmp_path):
    wheels = tmp_path / "wheels"
    for version in ("0.4.0", "0.3.1"):
        (wheels / f"v{version}").mkdir(parents=True)
        _sdk_wheel(wheels / f"v{version}", version, content=version.encode())
        (wheels / f"v{version}" / "SHA256SUMS").write_text("other assets are ignored\n")
    base = "https://github.com/o/maestro/releases/download/"
    assert (
        index.main(["--wheels", str(wheels), "--base-url", base, "--out", str(tmp_path / "site")])
        == 0
    )

    page = (tmp_path / "site" / "simple" / "maestro-sdk" / "index.html").read_text()
    for version in ("0.4.0", "0.3.1"):
        name = f"maestro_sdk-{version}-py3-none-any.whl"
        digest = hashlib.sha256((wheels / f"v{version}" / name).read_bytes()).hexdigest()
        assert (
            f'<a href="{base}v{version}/{name}#sha256={digest}" '
            f'data-requires-python="&gt;=3.10">{name}</a>'
        ) in page
    assert 'href="maestro-sdk/"' in (tmp_path / "site" / "simple" / "index.html").read_text()


def test_the_github_index_refuses_a_release_without_its_own_sdk(tmp_path):
    out = ["--base-url", "https://x/releases/download", "--out", str(tmp_path / "site")]
    (tmp_path / "v0.4.0").mkdir()
    with pytest.raises(SystemExit, match="exactly one SDK wheel"):
        index.main(["--wheels", str(tmp_path), *out])
    _sdk_wheel(tmp_path / "v0.4.0", "0.4.1")
    with pytest.raises(SystemExit, match="holds the SDK 0.4.1"):
        index.main(["--wheels", str(tmp_path), *out])
    assert not (tmp_path / "site").exists()


def test_release_failure_resumes_draft_without_replacing_assets(gitea, tmp_path):
    (tmp_path / "a.whl").write_bytes(b"one")
    (tmp_path / "b.tar.gz").write_bytes(b"two")
    args = ["--dist", str(tmp_path), "--tag", "v0.4.0", "--commit", "c" * 40]
    gitea["fail_name"] = "b.tar.gz"
    with pytest.raises(SystemExit, match="503"):
        publish.main(args)
    assert gitea["releases"][0]["draft"] is True
    assert gitea["uploads"] == ["a.whl"]
    gitea["fail_name"] = None
    publish.main(args)
    assert gitea["uploads"] == ["a.whl", "b.tar.gz"]
    assert gitea["releases"][0]["draft"] is False
    (tmp_path / "a.whl").write_bytes(b"different")
    with pytest.raises(SystemExit, match="different SHA-256"):
        publish.main(args)
    assert gitea["uploads"] == ["a.whl", "b.tar.gz"]
    gitea["tags"]["v0.4.0"] = "d" * 40
    with pytest.raises(SystemExit, match="expected"):
        publish.main(args)
    assert gitea["tags"]["v0.4.0"] == "d" * 40


def test_archive_is_reproducible_despite_mtime_mode_and_destination(tmp_path):
    top = tmp_path / "maestro-test"
    top.mkdir()
    (top / "data").write_text("same bytes")
    (top / "maestroctl").write_text("script")
    first, second = tmp_path / "one.tar.gz", tmp_path / "two.tar.gz"
    build.reproducible_tar(top, first, 1700000000)
    for p in top.iterdir():
        p.chmod(0o777)
        os.utime(p, (1800000000, 1800000000))
    build.reproducible_tar(top, second, 1700000000)
    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first) as archive:
        assert archive.getmember("maestro-test/maestroctl").mode == 0o755
        assert archive.getmember("maestro-test/data").mode == 0o644


class FakeChecks:
    def __init__(self):
        self.run = {
            "id": 2,
            "head_sha": "a" * 40,
            "event": "push",
            "path": "verify.yml@refs/heads/feature/test",
            "html_url": "https://ci/run/2",
            "conclusion": "failure",
        }  # Publication may fail independently.
        self.job = {
            "name": "verify",
            "head_sha": "a" * 40,
            "status": "completed",
            "conclusion": "success",
            "steps": [{"name": n, "conclusion": "success"} for n in gate.REQUIRED_STEPS],
        }

    def call(self, method, path):
        if "/jobs" in path:
            return {"jobs": [self.job]}
        return {"workflow_runs": [] if "page=2" in path else [self.run]}


def test_publication_uses_exact_successful_source_job_not_overall_run():
    client = FakeChecks()
    assert gate.verified(client, "a" * 40) == client.run
    assert gate.verified(client, "b" * 40) is None


@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        ("run", "path", "other.yml@refs/heads/dev"),
        ("run", "path", "verify.yml@refs/tags/v0.4.0"),
        ("run", "event", "pull_request"),
        ("job", "head_sha", "b" * 40),
        ("job", "name", "publish"),
        ("job", "conclusion", "failure"),
        ("job", "status", "running"),
        ("job", "steps", []),
    ],
)
def test_arbitrary_green_results_do_not_authorize_release(target, field, value):
    client = FakeChecks()
    getattr(client, target)[field] = value
    assert gate.verified(client, "a" * 40) is None


def test_skipped_required_check_does_not_authorize_release():
    client = FakeChecks()
    client.job["steps"][0]["conclusion"] = "skipped"
    assert gate.verified(client, "a" * 40) is None


@pytest.fixture
def release_repo(tmp_path):
    repo, remote = tmp_path / "repo", tmp_path / "remote.git"
    repo.mkdir()

    def git(*args, root=repo):
        return release_tool.git(*args, root=root)

    git("init", "--bare", str(remote))
    git("init", "-b", "dev")
    git("config", "user.name", "Release Test")
    git("config", "user.email", "release@example.test")
    (repo / "pyproject.toml").write_text('[project]\nversion = "0.4.1.dev0"\n')
    (repo / "business").write_text("selected")
    git("add", ".")
    git("commit", "-m", "Business change")
    git("branch", "master")
    git("remote", "add", "origin", str(remote))
    git("push", "origin", "dev", "master")
    git("fetch", "origin")
    return repo, git


@pytest.mark.parametrize("failed_version", ["0.4.1", "0.4.2.dev0"])
def test_prepare_pins_scope_and_prepares_next_version_before_publishing(
    release_repo, tmp_path, monkeypatch, failed_version
):
    repo, git = release_repo
    source = git("rev-parse", "HEAD")

    failed = False

    def stamp(value, root):
        nonlocal failed
        (root / "pyproject.toml").write_text(f'[project]\nversion = "{value}"\n')
        if value == failed_version and not failed:
            failed = True
            raise RuntimeError("interrupted generation")

    monkeypatch.setattr(release_tool, "set_version", stamp)
    # The actual generator is tested by contract drift tests. Use a minimal repo here.
    original = release_tool.git

    def minimal_git(*args, root=repo):
        if args[:2] == ("add", "--"):
            return original("add", ".", root=root)
        return original(*args, root=root)

    monkeypatch.setattr(release_tool, "git", minimal_git)
    notes = tmp_path / "notes.md"
    notes.write_text("User changes; migrations: none; validation: unit tests.\n")
    args = SimpleNamespace(
        remote="origin",
        commit=source,
        version="0.4.1",
        next_version=None,
        notes=notes,
        worktree=tmp_path / "candidate",
    )
    with pytest.raises(RuntimeError, match="interrupted"):
        release_tool.prepare(args, repo)
    candidate = release_tool.prepare(args, repo)
    assert release_tool.prepare(args, repo) == candidate  # Preparation can be resumed repeatedly.
    completion = git("rev-parse", "release/v0.4.1-next")
    assert candidate != source and completion != candidate
    assert 'version = "0.4.1"' in git("show", f"{candidate}:pyproject.toml")
    assert 'version = "0.4.2.dev0"' in git("show", f"{completion}:pyproject.toml")
    assert git("rev-parse", "origin/master") == source
    # Concurrent dev work is integrated only in the development completion.
    (repo / "later").write_text("must survive")
    git("add", ".")
    git("commit", "-m", "Later work")
    git("push", "origin", "dev")
    args.finish_worktree = tmp_path / "finish"
    release_tool.finish_dev(candidate, completion, args, repo)
    git("fetch", "origin")
    final = git("rev-parse", "origin/dev")
    assert git("show", f"{final}:later") == "must survive"
    assert git("show", f"{candidate}:business") == "selected"
    assert 'version = "0.4.2.dev0"' in git("show", f"{final}:pyproject.toml")
    release_tool.finish_dev(candidate, completion, args, repo)  # Repeat is harmless.
    assert git("rev-parse", "origin/dev") == final


def test_prepare_refuses_master_divergence_without_creating_worktree(release_repo, tmp_path):
    repo, git = release_repo
    source = git("rev-parse", "HEAD")
    git("switch", "master")
    (repo / "master-only").write_text("must not disappear")
    git("add", ".")
    git("commit", "-m", "New master")
    git("push", "origin", "master")
    args = SimpleNamespace(
        remote="origin", commit=source, version="0.4.1", worktree=tmp_path / "candidate"
    )
    with pytest.raises(SystemExit, match="cannot fast-forward"):
        release_tool.prepare(args, repo)
    assert not args.worktree.exists()


def test_publication_retry_only_restarts_failed_jobs(monkeypatch):
    class Client:
        def __init__(self):
            self.calls = []

        def call(self, *args):
            self.calls.append(args)

    client = Client()
    attempts = iter(
        [
            {"id": 9, "status": "completed", "conclusion": "failure", "html_url": "https://ci/9"},
            {"id": 9, "status": "completed", "conclusion": "success", "html_url": "https://ci/9"},
        ]
    )
    monkeypatch.setattr(release_tool, "latest_run", lambda *a: next(attempts))
    monkeypatch.setattr(release_tool.time, "sleep", lambda *a: None)
    release_tool.wait_publication(
        client, "a" * 40, "release.yml", "v0.4.1", float("inf"), retry=True
    )
    assert client.calls == [("POST", "/actions/runs/9/rerun-failed-jobs")]


def test_github_draft_upload_and_resume_use_the_same_immutable_rules(gitea, tmp_path, monkeypatch):
    client = publish.Gitea(os.environ["GITEA_API"], "o/r", "t0ken", github=True)
    (tmp_path / "a.whl").write_bytes(b"sdk wheel")
    (tmp_path / "b.tar.gz").write_bytes(b"service package")
    gitea["fail_name"] = "b.tar.gz"
    # GitHub can return an HTTPError directly from its binary upload endpoint.
    with pytest.raises(Exception, match="503"):
        publish.publish(client, tmp_path, "v0.4.0", "c" * 40)
    assert gitea["releases"][0]["draft"] is True
    gitea["fail_name"] = None
    publish.publish(client, tmp_path, "v0.4.0", "c" * 40)
    publish.publish(client, tmp_path, "v0.4.0", "c" * 40)
    assert gitea["uploads"] == ["a.whl", "b.tar.gz"]
    assert gitea["created"] == 1
    (tmp_path / "b.tar.gz").write_bytes(b"changed")
    with pytest.raises(SystemExit, match="different SHA-256"):
        publish.publish(client, tmp_path, "v0.4.0", "c" * 40)


def test_cross_host_download_redirect_does_not_forward_credentials():
    import urllib.request

    request = urllib.request.Request(
        "https://api.example/asset", headers={"Authorization": "Bearer secret"}
    )
    redirect = publish.SafeRedirect().redirect_request(
        request, None, 302, "Found", {}, "https://storage.example/file"
    )
    assert redirect.get_header("Authorization") is None
    redirect = publish.SafeRedirect().redirect_request(
        request, None, 302, "Found", {}, "https://api.example/file"
    )
    assert redirect.get_header("Authorization") == "Bearer secret"


def test_package_validation_rejects_missing_extra_and_changed_assets(tmp_path):
    (tmp_path / "one").write_bytes(b"asset")
    digest = hashlib.sha256(b"asset").hexdigest()
    (tmp_path / "SHA256SUMS").write_text(f"{digest}  one\n")
    (tmp_path / "extra").write_bytes(b"unlisted")
    with pytest.raises(SystemExit, match="SHA256SUMS"):
        list(validate.inspect_assets(tmp_path, "a" * 40))
    (tmp_path / "extra").unlink()
    (tmp_path / "one").write_bytes(b"changed")
    with pytest.raises(SystemExit, match="SHA256SUMS"):
        list(validate.inspect_assets(tmp_path, "a" * 40))
    (tmp_path / "one").unlink()
    with pytest.raises(SystemExit, match="SHA256SUMS"):
        list(validate.inspect_assets(tmp_path, "a" * 40))


@pytest.mark.parametrize("failed_stage", ["gitea", "github", "pages"])
def test_partial_publication_never_reports_complete_or_finishes_dev(
    failed_stage, monkeypatch, tmp_path
):
    sha, completion = "a" * 40, "b" * 40
    clients = [SimpleNamespace(tag_commit=lambda tag: None) for _ in range(2)]
    monkeypatch.setattr(release_tool, "candidate_refs", lambda *a: (sha, completion))
    monkeypatch.setattr(release_tool, "clients", lambda: clients)
    monkeypatch.setattr(release_tool, "verified", lambda *a: {"html_url": "https://ci/source"})
    monkeypatch.setattr(release_tool, "ancestor", lambda *a: True)
    monkeypatch.setattr(release_tool, "maybe_ref", lambda *a: None)
    pushes, finished = [], []

    def git(*args, **kwargs):
        if args[0] == "show":
            if args[1].endswith("pyproject.toml"):
                version = "0.4.2.dev0" if args[1].startswith(completion) else "0.4.1"
                return f'[project]\nversion = "{version}"\n'
            return "Release notes"
        if args[0] == "push":
            pushes.append(args[1:])
        return ""

    monkeypatch.setattr(release_tool, "git", git)

    def wait(client, *args):
        if client is clients[0] and failed_stage == "gitea":
            raise SystemExit("SDK or Gitea release upload failed")
        if client is clients[1]:
            raise SystemExit("index failed" if failed_stage == "pages" else "GitHub release failed")

    monkeypatch.setattr(release_tool, "wait_publication", wait)
    monkeypatch.setattr(release_tool, "release_state", lambda *a: {"draft": False})
    monkeypatch.setattr(release_tool, "finish_dev", lambda *a: finished.append(True))
    args = SimpleNamespace(
        remote="origin", version="0.4.1", timeout=100, github_remote="github", retry=True
    )
    with pytest.raises(SystemExit, match="failed"):
        release_tool.publish(args, tmp_path)
    assert finished == []
    assert any(p[0] == "github" for p in pushes) is (failed_stage != "gitea")


def test_missing_tag_is_not_a_completed_release():
    class Client:
        def tag_commit(self, tag):
            return None

        def call(self, *args, **kwargs):
            pytest.fail("a missing tag cannot qualify as a completed release")

    assert release_tool.release_state(Client(), "v0.4.1", "a" * 40) is None


def test_dev_completion_never_pushes_a_formal_version(release_repo, tmp_path):
    repo, git = release_repo
    source = git("rev-parse", "HEAD")
    (repo / "pyproject.toml").write_text('[project]\nversion = "0.4.1"\n')
    git("add", ".")
    git("commit", "-m", "Incorrectly prepared development completion")
    completion = git("rev-parse", "HEAD")
    args = SimpleNamespace(remote="origin", version="0.4.1", finish_worktree=tmp_path / "finish")
    with pytest.raises(SystemExit, match="development version"):
        release_tool.finish_dev(source, completion, args, repo)
    git("fetch", "origin")
    assert git("rev-parse", "origin/dev") == source


def test_download_does_not_follow_an_unrelated_advertised_path():
    client = publish.Gitea("https://internal.example/api/v1", "o/r", "secret")
    with pytest.raises(SystemExit, match="unexpected release download path"):
        client.asset_digest({"browser_download_url": "https://elsewhere.example/private"}, 1)


def test_download_preserves_internal_server_subpath(monkeypatch):
    import io

    client = publish.Gitea("https://internal.example/gitea/api/v1", "o/r", "secret")
    seen = []

    def request(method, url, **kwargs):
        seen.append(url)
        return io.BytesIO(b"asset")

    monkeypatch.setattr(client, "request", request)
    result = client.asset_digest(
        {"browser_download_url": "https://public.example/other/o/r/releases/download/v1/wheel.whl"},
        1,
    )
    assert result == hashlib.sha256(b"asset").hexdigest()
    assert seen == ["https://internal.example/gitea/o/r/releases/download/v1/wheel.whl"]


def test_installed_service_reports_its_package_build(tmp_path):
    """Development builds of one version share the version string; the build read from
    the installed package description tells them apart. A source checkout reports none."""
    from maestro import installed_build

    version_dir = tmp_path / "0.5.0.dev0-921d0e7f95b7"
    (version_dir / "venv").mkdir(parents=True)
    assert installed_build(version_dir / "venv") is None
    info = {"name": "maestro", "version": "0.5.0.dev0", "build": "0.5.0.dev0-921d0e7f95b7"}
    (version_dir / "RELEASE.json").write_text(json.dumps(info))
    assert installed_build(version_dir / "venv") == "0.5.0.dev0-921d0e7f95b7"
    (version_dir / "RELEASE.json").write_text(json.dumps({**info, "name": "other"}))
    assert installed_build(version_dir / "venv") is None
    (version_dir / "RELEASE.json").write_text("not json")
    assert installed_build(version_dir / "venv") is None


class FakeRefs(publish.Gitea):
    """Tag refs as the server reports them: annotated tags point at a tag object."""

    def __init__(self, refs, tags):
        super().__init__("http://unused/api/v1", "o/r", "t")
        self.refs, self.tags = refs, tags

    def call(self, method, path, body=None, **kwargs):
        if path.startswith("/git/refs/tags/"):
            name = path.removeprefix("/git/refs/tags/")
            if name not in self.refs:
                return None
            return [{"ref": f"refs/tags/{name}", "object": self.refs[name]}]
        return {"object": self.tags[path.removeprefix("/git/tags/")]}


def test_release_tag_must_be_annotated_on_the_server():
    """The checkout in CI turns the tag into a plain ref, so the server is asked."""
    client = FakeRefs(
        {
            "v1.0.0": {"type": "tag", "sha": "t" * 40},
            "dev-1": {"type": "commit", "sha": "a" * 40},
        },
        {"t" * 40: {"type": "commit", "sha": "a" * 40}},
    )
    assert client.annotated_tag_commit("v1.0.0") == "a" * 40
    assert client.annotated_tag_commit("dev-1") is None
    assert client.annotated_tag_commit("v9.9.9") is None
