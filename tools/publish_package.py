"""Publish the SDK wheel to the Gitea Python package registry. Standard library only.

    GITEA_URL=<server> GITEA_OWNER=<owner> GITEA_TOKEN=<token> \\
    python3 tools/publish_package.py --dist <dir>

Uploads the one ``maestro_sdk-*.whl`` in ``--dist`` to ``<server>/api/packages/<owner>/pypi``
(the protocol ``twine upload`` speaks). A version is never overwritten: when the job is
re-run and the file is already there, the registry's SHA-256 must equal the wheel's.
Only canonical X.Y.Z versions are uploaded; development wheels travel with local packages.
"""

import argparse
import hashlib
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from email.parser import HeaderParser
from pathlib import Path

PACKAGE = "maestro-sdk"


class Registry:
    def __init__(self, server: str, owner: str, token: str):
        self.server = server.rstrip("/")
        self.owner = urllib.parse.quote(owner, safe="")
        self.token = token

    def call(self, method, path, *, data=None, content_type=None, accept="application/json"):
        """Returns (status, body); HTTP errors other than 404 and 409 end the script."""
        headers = {"Authorization": f"token {self.token}", "Accept": accept}
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(self.server + path, data, headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            body = error.read()
            if error.code in (404, 409):
                return error.code, body
            detail = body.decode(errors="replace")[:500]
            raise SystemExit(f"{method} {path}: HTTP {error.code} {detail}") from None

    def upload(self, wheel: Path, fields: dict[str, str]) -> bool:
        """True when uploaded, False when the same file is already in the registry."""
        boundary = uuid.uuid4().hex
        parts = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
            for k, v in fields.items()
        ]
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="content"; '
            f'filename="{wheel.name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
            + wheel.read_bytes()
            + b"\r\n"
        )
        data = b"".join(parts) + f"--{boundary}--\r\n".encode()
        status, _ = self.call(
            "POST",
            f"/api/packages/{self.owner}/pypi",
            data=data,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        if status == 409:
            published = self.published_digest(wheel.name)
            if published != fields["sha256_digest"]:
                raise SystemExit(
                    f"{wheel.name} is already published, but its SHA-256 in the registry "
                    "differs or cannot be read; a published file is never replaced"
                )
            return False
        if status == 404:
            raise SystemExit(f"no Python package registry for owner {self.owner}")
        return True

    def published_digest(self, filename: str) -> str | None:
        status, body = self.call(
            "GET", f"/api/packages/{self.owner}/pypi/simple/{PACKAGE}", accept="text/html"
        )
        if status != 200:
            return None
        found = re.search(re.escape(filename) + r"#sha256=([0-9a-f]{64})", body.decode())
        return found[1] if found else None


def wheel_fields(wheel: Path) -> dict[str, str]:
    """The upload form for ``wheel``, from its own metadata."""
    with zipfile.ZipFile(wheel) as archive:
        (name,) = [n for n in archive.namelist() if re.fullmatch(r"[^/]+\.dist-info/METADATA", n)]
        meta = HeaderParser().parsestr(archive.read(name).decode())
    if re.sub(r"[-_.]+", "-", meta["Name"]).lower() != PACKAGE:
        raise SystemExit(f"{wheel.name} is not {PACKAGE}")
    return {
        ":action": "file_upload",
        "protocol_version": "1",
        "metadata_version": meta["Metadata-Version"],
        "name": meta["Name"],
        "version": meta["Version"],
        "summary": meta.get("Summary", ""),
        "requires_python": meta.get("Requires-Python", ""),
        "filetype": "bdist_wheel",
        "pyversion": "py3",
        "sha256_digest": hashlib.sha256(wheel.read_bytes()).hexdigest(),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", required=True, type=Path)
    args = parser.parse_args(argv)

    if any("-unreleased" in p.name for p in args.dist.glob("maestro-*.tar.gz")):
        raise SystemExit("unreleased development packages must not be uploaded to the SDK index")
    wheels = sorted(args.dist.glob("maestro_sdk-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"{args.dist} must hold exactly one SDK wheel, found {len(wheels)}")
    (wheel,) = wheels
    fields = wheel_fields(wheel)
    if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", fields["version"]):
        raise SystemExit("only canonical X.Y.Z SDK versions can be published")
    registry = Registry(
        os.environ["GITEA_URL"], os.environ["GITEA_OWNER"], os.environ["GITEA_TOKEN"]
    )
    uploaded = registry.upload(wheel, fields)
    print(f"{'uploaded' if uploaded else 'already published'} {wheel.name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
