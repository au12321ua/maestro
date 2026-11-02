"""Check actual release assets and install their wheels outside the source checkout.

Uses locked service dependencies and a separate SDK environment. No service, Docker,
state or external model request is started. Each invocation uses a fresh temporary HOME.
"""

import argparse
import configparser
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from email.parser import BytesParser
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def metadata(wheel):
    with zipfile.ZipFile(wheel) as archive:
        (name,) = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
        return BytesParser().parsebytes(archive.read(name))


def inspect_assets(dist, commit, version=None):
    sums = {}
    for line in (dist / "SHA256SUMS").read_text().splitlines():
        value, name = line.split("  ", 1)
        if Path(name).name != name or name in sums:
            raise SystemExit("invalid SHA256SUMS entry")
        sums[name] = value
    assets = {p.name for p in dist.iterdir() if p.is_file()} - {"SHA256SUMS"}
    if assets != sums.keys() or any(digest(dist / n) != d for n, d in sums.items()):
        raise SystemExit("release assets do not match SHA256SUMS")
    (package,) = dist.glob("maestro-*.tar.gz")
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(package) as archive:
            archive.extractall(tmp, filter="data")
        (top,) = Path(tmp).iterdir()
        manifest = json.loads((top / "RELEASE.json").read_text())
        if manifest["commit"] != commit or manifest["dirty"]:
            raise SystemExit("release manifest does not describe the clean expected commit")
        if version and manifest["version"] != version:
            raise SystemExit("release manifest version differs from tag")
        files = {
            str(p.relative_to(top)): digest(p)
            for p in top.rglob("*")
            if p.is_file() and p.name != "RELEASE.json"
        }
        if files != manifest["files"]:
            raise SystemExit("package files do not match RELEASE.json")
        (service,) = (top / "server").glob("*.whl")
        (sdk,) = (top / "sdk").glob("*.whl")
        (runner,) = (top / "runner").glob("*.whl")
        for path, name, expected in (
            (service, "maestro", manifest["version"]),
            (sdk, "maestro-sdk", manifest["sdk_version"]),
            (runner, "maestro-runner", "1"),
        ):
            info = metadata(path)
            if (info["Name"], info["Version"]) != (name, expected):
                raise SystemExit(f"{path.name}: unexpected wheel metadata")
        if version and manifest["sdk_version"] != version:
            raise SystemExit("SDK version differs from tag")
        if digest(sdk) != digest(dist / sdk.name):
            raise SystemExit("SDK attachment differs from the SDK inside the package")
        with zipfile.ZipFile(service) as archive:
            if not archive.read("maestro/static/index.html"):
                raise SystemExit("service wheel has no web page")
            (entry,) = [n for n in archive.namelist() if n.endswith(".dist-info/entry_points.txt")]
            config = configparser.ConfigParser()
            config.read_string(archive.read(entry).decode())
            if config["console_scripts"]["maestro"] != "maestro.cli:main":
                raise SystemExit("service entry point differs")
        for name in (
            "LICENSE",
            "NOTICE",
            "THIRD_PARTY_LICENSES.md",
            "INSTALL.md",
            "maestroctl",
            "server/requirements.txt",
            "runner/requirements.txt",
        ):
            if not (top / name).stat().st_size:
                raise SystemExit(f"missing or empty package file: {name}")
        yield top, service, sdk, manifest


def validate(dist, commit, version=None):
    for top, service, sdk, manifest in inspect_assets(dist, commit, version):
        with tempfile.TemporaryDirectory(prefix="maestro-wheel-check-") as tmp:
            scratch = Path(tmp)
            env = {
                k: v
                for k, v in os.environ.items()
                if k
                in {
                    "PATH",
                    "HTTPS_PROXY",
                    "HTTP_PROXY",
                    "NO_PROXY",
                    "UV_OFFLINE",
                    "UV_CACHE_DIR",
                    "UV_PYTHON",
                    "UV_PYTHON_DOWNLOADS",
                    "UV_HTTP_TIMEOUT",
                    "UV_HTTP_RETRIES",
                }
            }
            env["HOME"] = str(scratch / "home")
            Path(env["HOME"]).mkdir()

            def run(*cmd, scratch=scratch, env=env):
                subprocess.run(cmd, cwd=scratch, env=env, check=True)

            for part, wheel, module, expected in (
                ("service", service, "maestro", manifest["version"]),
                ("sdk", sdk, "maestro_sdk", manifest["sdk_version"]),
            ):
                venv = scratch / part
                run("uv", "venv", "--python", sys.executable, str(venv))
                python = str(venv / "bin/python")
                if part == "service":
                    run(
                        "uv",
                        "pip",
                        "install",
                        "--python",
                        python,
                        "--require-hashes",
                        "-r",
                        str(top / "server/requirements.txt"),
                    )
                    run("uv", "pip", "install", "--python", python, "--no-deps", str(wheel))
                else:
                    # SDK uses only dependencies already fixed by the service lock, but
                    # has its own environment and never installs the service itself.
                    run(
                        "uv",
                        "pip",
                        "install",
                        "--python",
                        python,
                        "--require-hashes",
                        "-r",
                        str(top / "server/requirements.txt"),
                    )
                    run("uv", "pip", "install", "--python", python, "--no-deps", str(wheel))
                probe = (
                    "import importlib, importlib.metadata as m, pathlib, sys; "
                    f"p=importlib.import_module({module!r}); "
                    f"assert m.version({'maestro' if part == 'service' else 'maestro-sdk'!r}) == {expected!r}; "
                    "assert pathlib.Path(p.__file__).is_relative_to(sys.prefix); "
                )
                if part == "service":
                    probe += (
                        f"assert p.__version__ == {expected!r}; "
                        "assert pathlib.Path(p.__file__).with_name('static').joinpath('index.html').is_file()"
                    )
                else:
                    probe += "assert importlib.util.find_spec('maestro') is None; from maestro_sdk import MaestroClient"
                run(python, "-I", "-c", probe)
                if part == "service":
                    run(str(venv / "bin/maestro"), "--help")
        print(f"validated wheels and package for {manifest['commit']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", required=True, type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--expect-version")
    args = parser.parse_args(argv)
    validate(args.dist.resolve(), args.commit, args.expect_version)


if __name__ == "__main__":
    main()
