"""Publish release assets to Gitea Releases (N-02). Standard library only.

    GITEA_API=<server>/api/v1 GITEA_REPO=<owner>/<repo> GITEA_TOKEN=<token> \\
    python3 tools/publish_release.py --dist <dir> --tag <tag> --commit <sha> \\
        [--prerelease --keep 10] [--title <title>] [--notes <file>]

Creates the release for ``--tag`` (Gitea creates a missing tag at ``--commit``), or reuses
it when the job is re-run, and uploads every file in ``--dist``, replacing assets of the
same name. With ``--prerelease --keep N`` it then deletes all but the newest N ``dev-*``
prereleases together with their tags; formal releases are never deleted.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

DEV_PREFIX = "dev-"


class Gitea:
    def __init__(self, api: str, repo: str, token: str):
        self.base = f"{api.rstrip('/')}/repos/{repo}"
        self.token = token

    def call(self, method, path, body=None, *, data=None, content_type=None, ok_missing=False):
        headers = {"Authorization": f"token {self.token}", "Accept": "application/json"}
        if body is not None:
            data, content_type = json.dumps(body).encode(), "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(self.base + path, data, headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            if ok_missing and error.code == 404:
                return None
            detail = error.read().decode(errors="replace")[:500]
            raise SystemExit(f"{method} {path}: HTTP {error.code} {detail}") from None
        return json.loads(raw) if raw else {}

    def releases(self) -> list[dict]:
        found, page = [], 1
        while batch := self.call("GET", f"/releases?limit=50&page={page}"):
            found += batch
            page += 1
        return found

    def upload(self, release_id: int, path: Path) -> None:
        boundary = uuid.uuid4().hex
        head = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="attachment"; '
            f'filename="{path.name}"\r\nContent-Type: application/octet-stream\r\n\r\n'
        )
        data = head.encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        name = urllib.parse.quote(path.name)
        self.call(
            "POST",
            f"/releases/{release_id}/assets?name={name}",
            data=data,
            content_type=f"multipart/form-data; boundary={boundary}",
        )


def stale_prereleases(releases: list[dict], keep: int) -> list[dict]:
    """``dev-*`` prereleases beyond the newest ``keep``; formal releases never qualify."""
    dev = [r for r in releases if r["prerelease"] and r["tag_name"].startswith(DEV_PREFIX)]
    dev.sort(key=lambda r: r["created_at"], reverse=True)
    return dev[keep:]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", required=True, type=Path)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--title")
    parser.add_argument("--notes", type=Path)
    parser.add_argument("--prerelease", action="store_true")
    parser.add_argument("--keep", type=int, default=10)
    args = parser.parse_args(argv)

    gitea = Gitea(os.environ["GITEA_API"], os.environ["GITEA_REPO"], os.environ["GITEA_TOKEN"])
    assets = sorted(p for p in args.dist.iterdir() if p.is_file())
    if not assets:
        raise SystemExit(f"{args.dist} has nothing to publish")
    tag = urllib.parse.quote(args.tag, safe="")
    release = gitea.call("GET", f"/releases/tags/{tag}", ok_missing=True)
    if release is None:
        release = gitea.call(
            "POST",
            "/releases",
            {
                "tag_name": args.tag,
                "target_commitish": args.commit,
                "name": args.title or args.tag,
                "body": args.notes.read_text() if args.notes else "",
                "prerelease": args.prerelease,
                "draft": False,
            },
        )
    names = {a.name for a in assets}
    for old in release.get("assets") or []:  # a re-run replaces what it uploaded before
        if old["name"] in names:
            gitea.call("DELETE", f"/releases/{release['id']}/assets/{old['id']}")
    for path in assets:
        gitea.upload(release["id"], path)
        print(f"uploaded {path.name}")
    print(f"release {args.tag}: {release.get('html_url', '')}")

    if args.prerelease:
        for old in stale_prereleases(gitea.releases(), args.keep):
            gitea.call("DELETE", f"/releases/{old['id']}")
            gitea.call("DELETE", f"/tags/{urllib.parse.quote(old['tag_name'], safe='')}")
            print(f"deleted prerelease {old['tag_name']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
