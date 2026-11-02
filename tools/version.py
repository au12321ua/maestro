"""Synchronize derived package versions and generated contracts from pyproject.toml."""

import argparse
import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r'^version = "[^"]+"$', re.MULTILINE)


def read(root=ROOT):
    return tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]


def check(root=ROOT):
    expected = read(root)
    sdk = tomllib.loads((root / "sdk/pyproject.toml").read_text())["project"]["version"]
    if sdk != expected:
        raise SystemExit(f"derived SDK version {sdk} differs from source version {expected}")
    return expected


def set_version(value, root=ROOT):
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:\.dev0)?", value):
        raise SystemExit("version must be X.Y.Z or X.Y.Z.dev0")
    for name in ("pyproject.toml", "sdk/pyproject.toml"):
        path = root / name
        path.write_text(VERSION.sub(f'version = "{value}"', path.read_text(), count=1))
    # Refresh installation metadata before exporting the runtime OpenAPI version.
    if not (root / "frontend/node_modules").exists():
        subprocess.run(
            ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=root / "frontend",
            check=True,
        )
    for command in (
        ["uv", "lock"],
        ["uv", "sync", "--locked", "--group", "dev"],
        ["uv", "run", "python", "tools/generate_clients.py"],
        ["npm", "run", "gen:api", "--prefix", "frontend"],
    ):
        subprocess.run(command, cwd=root, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("value", nargs="?")
    args = parser.parse_args()
    if args.value:
        set_version(args.value)
    print(check())


if __name__ == "__main__":
    main()
