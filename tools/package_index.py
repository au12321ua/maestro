"""Build the static Python package index of the SDK for GitHub Pages (U-62). Standard
library only.

    python3 tools/package_index.py --wheels <dir> --base-url <url> --out <site>

GitHub has no Python package registry, so the GitHub release workflow downloads the SDK
wheel of every formal release into ``<dir>/<tag>/`` and this script writes a PEP 503
simple index (``<site>/simple/maestro-sdk/``) whose links point at
``<base-url>/<tag>/<wheel>`` with the wheel's SHA-256 and Requires-Python. pip and uv read
it like the Gitea registry; the files themselves stay in the releases.
"""

import argparse
import hashlib
import html
import re
import sys
import zipfile
from email.parser import HeaderParser
from pathlib import Path

PACKAGE = "maestro-sdk"
PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title></head>
<body>
{body}
</body></html>
"""


def wheel_metadata(wheel: Path) -> dict[str, str]:
    with zipfile.ZipFile(wheel) as archive:
        (name,) = [n for n in archive.namelist() if re.fullmatch(r"[^/]+\.dist-info/METADATA", n)]
        meta = HeaderParser().parsestr(archive.read(name).decode())
    if re.sub(r"[-_.]+", "-", meta["Name"]).lower() != PACKAGE:
        raise SystemExit(f"{wheel.name} is not {PACKAGE}")
    return {"version": meta["Version"], "requires_python": meta.get("Requires-Python", "")}


def links(wheels: Path, base_url: str) -> list[str]:
    """One link per release; each release must hold the SDK of its own version."""
    found = []
    for release in sorted(p for p in wheels.iterdir() if p.is_dir()):
        tag = release.name
        files = sorted(release.glob("maestro_sdk-*.whl"))
        if len(files) != 1:
            raise SystemExit(f"release {tag} must hold exactly one SDK wheel, found {len(files)}")
        (wheel,) = files
        meta = wheel_metadata(wheel)
        if f"v{meta['version']}" != tag:
            raise SystemExit(f"release {tag} holds the SDK {meta['version']}")
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        href = html.escape(f"{base_url.rstrip('/')}/{tag}/{wheel.name}#sha256={digest}")
        requires = html.escape(meta["requires_python"])
        attribute = f' data-requires-python="{requires}"' if requires else ""
        found.append(f'<a href="{href}"{attribute}>{html.escape(wheel.name)}</a><br>')
    if not found:
        raise SystemExit(f"no release with an SDK wheel in {wheels}")
    return found


def write(out: Path, entries: list[str]) -> None:
    project = out / "simple" / PACKAGE
    project.mkdir(parents=True, exist_ok=True)
    (out / "simple" / "index.html").write_text(
        PAGE.format(title="Simple index", body=f'<a href="{PACKAGE}/">{PACKAGE}</a>')
    )
    (project / "index.html").write_text(
        PAGE.format(title=f"Links for {PACKAGE}", body="\n".join(entries))
    )
    (out / "index.html").write_text(
        PAGE.format(
            title=f"{PACKAGE} package index",
            body=f"<p>Python package index for <code>{PACKAGE}</code>: use the "
            '<a href="simple/">simple/</a> address of this page as an index.</p>',
        )
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheels", required=True, type=Path, help="one directory per tag")
    parser.add_argument("--base-url", required=True, help="the releases' download address")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    entries = links(args.wheels, args.base_url)
    write(args.out, entries)
    print(f"indexed {len(entries)} SDK release(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
