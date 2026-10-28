"""List the third-party licenses a release depends on (N-16).

    uv run python tools/third_party_licenses.py [--out THIRD_PARTY_LICENSES.md]

Two parts, read from what is installed for the locks: the web pages bundle their npm
runtime dependencies, so those are redistributed and carry their full license texts; the
service and Runner install their Python dependencies from PyPI at install time, so those
are listed by name, version and license only. Any license outside ``ALLOWED`` (GPL family,
unknown or unreadable) fails the run, which turns "re-check the locks before a release"
into a check CI runs on every push. Needs ``uv sync`` for the service and the Runner and
``npm ci`` in ``frontend/``.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# SPDX identifiers, plus "BSD" for packages that only say "BSD License" in a classifier.
ALLOWED = {
    "0BSD",
    "Apache-2.0",
    "BlueOak-1.0.0",
    "BSD",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "CC0-1.0",
    "CNRI-Python",
    "HPND",
    "ISC",
    "MIT",
    "MIT-0",
    "MPL-2.0",
    "PSF-2.0",
    "Python-2.0",
    "Unlicense",
    "Zlib",
}

# Trove classifiers and free-text "License" fields seen in the locks, mapped to SPDX.
NAMES = {
    "apache 2": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "bsd license": "BSD",
    "isc license": "ISC",
    "isc license (iscl)": "ISC",
    "mit license": "MIT",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "python software foundation license": "PSF-2.0",
    "the unlicense (unlicense)": "Unlicense",
}

# Read the installed distributions in the project's own environment, as JSON.
_PROBE = """
import importlib.metadata as m, json, sys
out = {}
for d in m.distributions():
    md = d.metadata
    out[md["Name"].lower().replace("_", "-")] = {
        "name": md["Name"], "version": d.version,
        "expression": md.get("License-Expression"), "license": md.get("License"),
        "classifiers": [c for c in (md.get_all("Classifier") or []) if c.startswith("License ::")],
    }
json.dump(out, sys.stdout)
"""


def sh(*cmd, cwd=ROOT) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout


def spdx(expression=None, license=None, classifiers=()) -> str | None:
    """The package's license as an SPDX-like expression, or None when it cannot be told."""
    if expression:
        return expression.strip()
    if classifiers:
        # An unknown classifier stays as its own text, so the allowed check refuses it.
        names = [c.split("::")[-1].strip() for c in classifiers]
        return " AND ".join(dict.fromkeys(NAMES.get(n.lower(), n) for n in names))
    lines = (license or "").strip().splitlines()
    if not lines:
        return None
    # A one-line field is a name or an expression ("MPL-2.0 AND MIT"); a longer one is
    # the license text itself, recognised by its first line.
    first = lines[0].strip()
    return NAMES.get(first.lower(), first if len(lines) == 1 else None)


def allowed(expression: str | None) -> bool:
    """Every AND part of at least one OR alternative must be allowed."""
    if not expression:
        return False
    if "(" in expression:  # nested expressions: be strict, every identifier must pass
        return all(t in ALLOWED for t in re.split(r"[()\s]+|\bAND\b|\bOR\b", expression) if t)
    return any(
        all(part.strip() in ALLOWED for part in alternative.split(" AND "))
        for alternative in expression.split(" OR ")
    )


def python_packages(project: Path) -> list[dict]:
    """Runtime dependencies from the lock, with their licenses from the installed environment."""
    pins = sh(
        "uv",
        "export",
        "--frozen",
        "--no-dev",
        "--no-emit-project",
        "--no-emit-workspace",
        "--no-hashes",
        "--format",
        "requirements-txt",
        cwd=project,
    )
    # name -> whether the pin carries an environment marker (e.g. colorama on Windows only)
    pinned = {
        m[1].lower().replace("_", "-"): bool(m[2])
        for m in re.finditer(r"^([A-Za-z0-9._-]+)==\S+( ;.*)?$", pins, re.MULTILINE)
    }
    python = project / ".venv/bin/python"
    if not python.exists():
        raise SystemExit(f"{project.relative_to(ROOT) or '.'}: run 'uv sync' first")
    installed = json.loads(sh(str(python), "-I", "-c", _PROBE, cwd=project))
    packages = []
    for name, conditional in pinned.items():
        info = installed.get(name)
        if info is None and conditional:
            continue  # not for this platform, so not installed anywhere Maestro runs
        if info is None:
            raise SystemExit(f"{name} is in the lock but not installed: run 'uv sync --locked'")
        packages.append(
            {
                "name": info["name"],
                "version": info["version"],
                "license": spdx(info["expression"], info["license"], info["classifiers"]),
            }
        )
    return packages


def npm_packages(frontend: Path) -> list[dict]:
    """Runtime (bundled) npm dependencies with their license texts."""
    if not (frontend / "node_modules").exists():
        raise SystemExit("frontend/node_modules is missing: run 'npm ci' in frontend/")
    paths = sh("npm", "ls", "--omit=dev", "--all", "--parseable", cwd=frontend).split()
    packages = {}
    for path in map(Path, paths):
        if path == frontend:
            continue
        meta = json.loads((path / "package.json").read_text())
        license = meta.get("license")
        if isinstance(license, dict):
            license = license.get("type")
        texts = sorted(
            p
            for p in path.iterdir()
            if re.match(r"(licen[cs]e|copying|notice)", p.name, re.IGNORECASE)
        )
        key = (meta["name"], meta["version"])
        packages[key] = {
            "name": meta["name"],
            "version": meta["version"],
            "license": license,
            "texts": [p.read_text(errors="replace").strip() for p in texts if p.is_file()],
        }
    return [packages[k] for k in sorted(packages)]


def render(web: list[dict], python: dict[str, list[dict]], commit: str) -> str:
    lines = [
        "# Third-party licenses",
        "",
        f"Maestro is licensed under Apache-2.0 (LICENSE, NOTICE). Generated for commit {commit}.",
        "",
        "## Web pages",
        "",
        "These packages are bundled into the web pages and redistributed with them.",
        "",
        "| Package | Version | License |",
        "| --- | --- | --- |",
        *(f"| {p['name']} | {p['version']} | {p['license']} |" for p in web),
    ]
    for part, packages in python.items():
        lines += [
            "",
            f"## Python: {part}",
            "",
            "Installed from PyPI when Maestro is installed; not included in the package.",
            "",
            "| Package | Version | License |",
            "| --- | --- | --- |",
            *(f"| {p['name']} | {p['version']} | {p['license']} |" for p in packages),
        ]
    lines += ["", "## License texts of the web page packages"]
    for p in web:
        lines += ["", f"### {p['name']} {p['version']} ({p['license']})", ""]
        if p["texts"]:
            for text in p["texts"]:
                lines += ["~~~~~~text", text, "~~~~~~"]  # texts may hold ~~~ themselves
        else:
            lines.append(f"The package ships no license file; its license is {p['license']}.")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", help="write the list here (default: only check)")
    args = parser.parse_args(argv)

    web = npm_packages(ROOT / "frontend")
    python = {"service": python_packages(ROOT), "Runner": python_packages(ROOT / "runner")}
    refused = [
        f"{part}: {p['name']} {p['version']} ({p['license'] or 'unknown license'})"
        for part, packages in [("web", web), *python.items()]
        for p in packages
        if not allowed(p["license"])
    ]
    if refused:
        print("licenses outside the allowed list:", *refused, sep="\n  ", file=sys.stderr)
        return 1
    if args.out:
        commit = sh("git", "rev-parse", "HEAD").strip()
        Path(args.out).write_text(render(web, python, commit))
    count = len(web) + sum(map(len, python.values()))
    print(f"{count} third-party packages, all licenses allowed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
