"""Publish immutable assets through draft Gitea or GitHub Releases; safe to resume.

Credentials: GITEA_API/GITEA_REPO/GITEA_TOKEN, or GH_TOKEN/GITHUB_REPOSITORY.
An existing tag must resolve to --commit. Only development tags may be created here.
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

DEV_PREFIX = "dev-"


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, *args, **kwargs):
        redirected = super().redirect_request(request, *args, **kwargs)
        if (
            redirected
            and urllib.parse.urlsplit(redirected.full_url).netloc
            != urllib.parse.urlsplit(request.full_url).netloc
        ):
            redirected.remove_header("Authorization")
        return redirected


class Gitea:
    def __init__(self, api: str, repo: str, token: str, *, github=False):
        self.base = f"{api.rstrip('/')}/repos/{repo}"
        self.token = token
        self.github = github
        self.repo = repo

    def request(self, method, url, data=None, content_type=None, accept="application/json"):
        headers = {"Authorization": f"Bearer {self.token}", "Accept": accept}
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data, headers, method=method)
        return urllib.request.build_opener(SafeRedirect()).open(request, timeout=300)

    def call(self, method, path, body=None, *, data=None, content_type=None, ok_missing=False):
        if body is not None:
            data, content_type = json.dumps(body).encode(), "application/json"
        try:
            with self.request(method, self.base + path, data, content_type) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            if ok_missing and error.code == 404:
                return None
            detail = error.read().decode(errors="replace")[:500]
            raise SystemExit(f"{method} {path}: HTTP {error.code} {detail}") from None
        return json.loads(raw) if raw else {}

    def releases(self) -> list[dict]:
        found, page = [], 1
        while batch := self.call("GET", f"/releases?limit=50&per_page=50&page={page}"):
            found += batch
            page += 1
        return found

    def tag_commit(self, tag):
        prefix = "/git/ref/tags/" if self.github else "/git/refs/tags/"
        ref = self.call("GET", prefix + urllib.parse.quote(tag, safe=""), ok_missing=True)
        if not ref:
            return None
        if isinstance(ref, list):
            ref = next((r for r in ref if r["ref"] == f"refs/tags/{tag}"), None)
            if ref is None:
                return None
        obj = ref["object"]
        for _ in range(8):
            if obj["type"] == "commit":
                return obj["sha"]
            if obj["type"] != "tag":
                break
            obj = self.call("GET", f"/git/tags/{obj['sha']}")["object"]
        raise SystemExit(f"tag {tag} does not resolve to a commit")

    def annotated_tag_commit(self, tag):
        """Commit of ``tag`` when it is an annotated tag, else None. Asked from the server:
        a CI checkout may have replaced the tag locally with a plain ref to the commit."""
        prefix = "/git/ref/tags/" if self.github else "/git/refs/tags/"
        ref = self.call("GET", prefix + urllib.parse.quote(tag, safe=""), ok_missing=True)
        if isinstance(ref, list):
            ref = next((r for r in ref if r["ref"] == f"refs/tags/{tag}"), None)
        if not ref or ref["object"]["type"] != "tag":
            return None
        return self.tag_commit(tag)

    def asset_digest(self, asset, release_id):
        # Read bytes even if the provider advertises a digest: this also verifies downloads.
        if self.github:
            url = self.base + f"/releases/assets/{asset['id']}"
        else:
            # Runners may use an internal API address while Gitea advertises its
            # public ROOT_URL. Keep downloads on the configured server, preserving
            # its own subpath, and accept only this repository's release route.
            advertised = urllib.parse.urlsplit(asset["browser_download_url"])
            prefix = f"/{urllib.parse.quote(self.repo, safe='/')}/releases/download/"
            start = advertised.path.find(prefix)
            if start < 0:
                raise SystemExit("unexpected release download path")
            server = self.base.split("/api/v1/repos/", 1)[0]
            url = server + advertised.path[start:]
        # Only send credentials to the configured API's host. Redirects strip them too.
        if urllib.parse.urlsplit(url).netloc != urllib.parse.urlsplit(self.base).netloc:
            raise SystemExit("release download points outside the configured server")
        digest = hashlib.sha256()
        with self.request("GET", url, accept="application/octet-stream") as response:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def upload(self, release_id: int, path: Path) -> None:
        name = urllib.parse.quote(path.name)
        if self.github:
            url = self.base.replace("api.github.com", "uploads.github.com")
            with self.request(
                "POST",
                f"{url}/releases/{release_id}/assets?name={name}",
                path.read_bytes(),
                "application/octet-stream",
            ) as response:
                response.read()
            return
        boundary = uuid.uuid4().hex
        head = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="attachment"; '
            f'filename="{path.name}"\r\nContent-Type: application/octet-stream\r\n\r\n'
        )
        data = head.encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        self.call(
            "POST",
            f"/releases/{release_id}/assets?name={name}",
            data=data,
            content_type=f"multipart/form-data; boundary={boundary}",
        )


def stale_prereleases(releases: list[dict], keep: int) -> list[dict]:
    dev = [r for r in releases if r["prerelease"] and r["tag_name"].startswith(DEV_PREFIX)]
    dev.sort(key=lambda r: r["created_at"], reverse=True)
    return dev[keep:]


def publish(client, dist, tag, commit, *, title=None, notes="", prerelease=False, keep=10):
    assets = sorted(p for p in dist.iterdir() if p.is_file())
    if not assets:
        raise SystemExit(f"{dist} has nothing to publish")
    actual = client.tag_commit(tag)
    if actual is None and prerelease and tag.startswith(DEV_PREFIX):
        client.call("POST", "/tags", {"tag_name": tag, "target": commit})
        actual = client.tag_commit(tag)
    if actual != commit:
        raise SystemExit(f"tag {tag}: expected {commit}, found {actual}; refusing to publish")
    quoted = urllib.parse.quote(tag, safe="")
    release = client.call("GET", f"/releases/tags/{quoted}", ok_missing=True)
    if release is None:
        # GitHub's tag lookup may not return a draft. Reuse it instead of duplicating it.
        release = next((r for r in client.releases() if r["tag_name"] == tag), None)
    if release is None:
        release = client.call(
            "POST",
            "/releases",
            {
                "tag_name": tag,
                "target_commitish": commit,
                "name": title or tag,
                "body": notes,
                "prerelease": prerelease,
                "draft": True,
            },
        )
    if release.get("body", "") != notes or release["prerelease"] != prerelease:
        raise SystemExit(
            f"release {tag}: description or release type differs; refusing to change it"
        )
    existing = {a["name"]: a for a in release.get("assets") or []}
    # Check all conflicts before writing anything. Never delete or replace an attachment.
    for path in assets:
        if path.name in existing:
            digest = client.asset_digest(existing[path.name], release["id"])
            if digest != hashlib.sha256(path.read_bytes()).hexdigest():
                raise SystemExit(f"{tag}/{path.name}: already published with different SHA-256")
    for path in assets:
        if path.name in existing:
            print(f"already published: {path.name}")
        else:
            client.upload(release["id"], path)
            print(f"uploaded {path.name}")
    uploaded = client.call("GET", f"/releases/{release['id']}")
    actual_assets = {asset["name"]: asset for asset in uploaded.get("assets") or []}
    for path in assets:
        if (
            path.name not in actual_assets
            or client.asset_digest(actual_assets[path.name], release["id"])
            != hashlib.sha256(path.read_bytes()).hexdigest()
        ):
            raise SystemExit(
                f"{tag}/{path.name}: uploaded asset is missing or has the wrong SHA-256"
            )
    if release["draft"]:
        client.call("PATCH", f"/releases/{release['id']}", {"draft": False})
    print(f"release {tag}: {release.get('html_url', '')}")
    if prerelease:
        for old in stale_prereleases(client.releases(), keep):
            client.call("DELETE", f"/releases/{old['id']}")
            client.call("DELETE", f"/tags/{urllib.parse.quote(old['tag_name'], safe='')}")
            print(f"deleted prerelease {old['tag_name']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", required=True, type=Path)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--title")
    parser.add_argument("--notes", type=Path)
    parser.add_argument("--prerelease", action="store_true")
    parser.add_argument("--keep", type=int, default=10)
    parser.add_argument("--github", action="store_true")
    args = parser.parse_args(argv)
    if args.keep < 1:
        parser.error("--keep must be positive")
    if args.github:
        client = Gitea(
            "https://api.github.com",
            os.environ["GITHUB_REPOSITORY"],
            os.environ["GH_TOKEN"],
            github=True,
        )
    else:
        client = Gitea(os.environ["GITEA_API"], os.environ["GITEA_REPO"], os.environ["GITEA_TOKEN"])
    publish(
        client,
        args.dist,
        args.tag,
        args.commit,
        title=args.title,
        notes=args.notes.read_text() if args.notes else "",
        prerelease=args.prerelease,
        keep=args.keep,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
