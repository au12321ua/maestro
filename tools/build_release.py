"""Build a release package for maestroctl (12 sections 1-2).

    uv run python tools/build_release.py [--out dist] [--allow-dirty] [--assets]
                                         [--expect-version 0.4.0]

The package ``maestro-<build>.tar.gz`` holds one directory with ``maestroctl``, the
service, Runner and SDK wheels, the locked requirements (with hashes) each environment
installs from, ``INSTALL.md``, ``LICENSE``, ``NOTICE``, ``THIRD_PARTY_LICENSES.md`` and
``RELEASE.json`` (versions, commit, schema head, the Runner revision and the SHA-256 of
every file). The web pages ship inside the service wheel, so the installing host needs
neither git nor Node; building the license list does need ``npm ci`` in ``frontend/``.

``--assets`` also puts what a release publishes next to the package (N-02): the SDK
wheel, the license list and ``SHA256SUMS`` over all of them. ``--expect-version`` refuses
to build when the code's version differs, so a ``v0.4.0`` tag cannot publish 0.4.1.
"""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sh(*cmd, cwd=ROOT) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wheel(project: Path, out: Path) -> Path:
    before = set(out.glob("*.whl"))
    sh("uv", "build", "--wheel", "--quiet", "--out-dir", str(out), cwd=project)
    (out / ".gitignore").unlink(missing_ok=True)  # uv marks its output directories
    (built,) = set(out.glob("*.whl")) - before
    return built


def requirements(project: Path, target: Path) -> None:
    text = sh(
        "uv",
        "export",
        "--frozen",
        "--no-dev",
        "--no-emit-project",
        "--no-header",
        "--format",
        "requirements-txt",
        cwd=project,
    )
    target.write_text(text)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "dist"))
    parser.add_argument("--allow-dirty", action="store_true", help="for tests only")
    parser.add_argument("--label", default="", help="appended to the build name (tests, rebuilds)")
    parser.add_argument("--assets", action="store_true", help="also write the release assets")
    parser.add_argument("--expect-version", help="fail unless the code has this version")
    args = parser.parse_args(argv)

    dirty = bool(sh("git", "status", "--porcelain").strip())
    if dirty and not args.allow_dirty:
        print("the working tree has uncommitted changes; commit them first", file=sys.stderr)
        return 1
    if not (ROOT / "src/maestro/static/index.html").exists():
        print("src/maestro/static is empty: run 'npm run build' in frontend/", file=sys.stderr)
        return 1
    commit = sh("git", "rev-parse", "HEAD").strip()
    version = re.search(r'__version__ = "([^"]+)"', (ROOT / "src/maestro/__init__.py").read_text())[
        1
    ]
    if args.expect_version and args.expect_version != version:
        print(f"the code is version {version}, not {args.expect_version}", file=sys.stderr)
        return 1
    harbor = re.search(r'"harbor==([^"]+)"', (ROOT / "runner/pyproject.toml").read_text())[1]
    build = f"{version}-{commit[:12]}" + ("-dirty" if dirty else "")
    if args.label:
        if not re.fullmatch(r"[a-z0-9.]+", args.label):
            print("--label takes lower-case letters, digits and dots", file=sys.stderr)
            return 1
        build += f"-{args.label}"
    head = sh(
        "uv",
        "run",
        "--quiet",
        "python",
        "-c",
        "from maestro.db import migrate; print(migrate.head())",
    ).strip()

    out = Path(args.out).absolute()
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        top = Path(scratch) / f"maestro-{build}"
        for part in ("server", "runner", "sdk"):
            (top / part).mkdir(parents=True)
        wheel(ROOT, top / "server")
        requirements(ROOT, top / "server/requirements.txt")
        runner_wheel = wheel(ROOT / "runner", top / "runner")
        requirements(ROOT / "runner", top / "runner/requirements.txt")
        sdk_wheel = wheel(ROOT / "sdk", top / "sdk")
        shutil.copy2(ROOT / "ops/maestroctl.py", top / "maestroctl")
        (top / "maestroctl").chmod(0o755)
        shutil.copy2(ROOT / "docs/guides/deployment.md", top / "INSTALL.md")
        for name in ("LICENSE", "NOTICE"):
            shutil.copy2(ROOT / name, top / name)
        licenses = top / "THIRD_PARTY_LICENSES.md"
        subprocess.run(
            [sys.executable, str(ROOT / "tools/third_party_licenses.py"), "--out", str(licenses)],
            cwd=ROOT, check=True, stdout=subprocess.DEVNULL,
        )  # fmt: skip
        # Same Harbor lock and Runner code give the same revision, so an update that does
        # not touch them reuses the installed Runner environment.
        runner_revision = hashlib.sha256(
            sha256(runner_wheel).encode() + sha256(top / "runner/requirements.txt").encode()
        ).hexdigest()[:12]
        files = {str(p.relative_to(top)): sha256(p) for p in sorted(top.rglob("*")) if p.is_file()}
        info = {
            "name": "maestro",
            "version": version,
            "build": build,
            "commit": commit,
            "dirty": dirty,
            "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "schema_head": head,
            "harbor_version": harbor,
            "runner_revision": runner_revision,
            "python": ">=3.12,<3.14",
            "files": files,
        }
        (top / "RELEASE.json").write_text(json.dumps(info, indent=2, sort_keys=True) + "\n")
        package = out / f"maestro-{build}.tar.gz"
        with tarfile.open(package, "w:gz") as archive:
            archive.add(top, arcname=top.name)
        if args.assets:
            shutil.copy2(sdk_wheel, out / sdk_wheel.name)
            shutil.copy2(licenses, out / licenses.name)
            assets = [package, out / sdk_wheel.name, out / licenses.name]
            sums = "".join(f"{sha256(a)}  {a.name}\n" for a in assets)
            (out / "SHA256SUMS").write_text(sums)
    print(package)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
