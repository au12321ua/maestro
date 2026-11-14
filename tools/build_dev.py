"""Build and validate a manually installable development package in one command.

    python3 tools/build_dev.py
    python3 tools/build_dev.py --commit <ref-or-SHA> --out /tmp/my-build

Defaults to the freshly fetched origin/dev. Requires Linux, Python 3.12–3.13,
uv, Node and npm. Only committed source is used; the caller's checkout is untouched.
No tags, releases, registry uploads, services or application state are created.
"""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path
from tempfile import mkdtemp

from release import git, resolve
from version import validate_version

ROOT = Path(__file__).resolve().parents[1]


def run(command, *, cwd, env):
    subprocess.run(command, cwd=cwd, env=env, check=True)


def build(args, root=ROOT):
    git("fetch", args.remote, root=root)
    sha = resolve(args.commit or f"{args.remote}/dev", root)
    version = tomllib.loads(git("show", f"{sha}:pyproject.toml", root=root))["project"]["version"]
    validate_version(version)
    out = (args.out or root / "dist" / f"development-{sha[:12]}").resolve()
    if out.exists():
        raise SystemExit(f"output already exists: {out}; choose a new --out directory")
    worktree = Path(mkdtemp(prefix="maestro-build-"))
    scratch = Path(mkdtemp(prefix="maestro-build-env-"))
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        in {
            "PATH",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "UV_OFFLINE",
            "UV_PYTHON",
            "UV_PYTHON_DOWNLOADS",
            "UV_HTTP_TIMEOUT",
            "UV_HTTP_RETRIES",
            "npm_config_registry",
            "npm_config_fetch_timeout",
            "npm_config_fetch_retries",
        }
    }
    env.update(HOME=str(scratch / "home"), UV_CACHE_DIR=str(scratch / "uv-cache"))
    Path(env["HOME"]).mkdir()
    print(f"Building unreleased {version} from {sha}\nWorktree: {worktree}", flush=True)
    try:
        git("worktree", "add", "--detach", str(worktree), sha, root=root)
        commands = [
            (["uv", "sync", "--locked", "--group", "dev"], worktree),
            (["uv", "sync", "--locked"], worktree / "runner"),
            (["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], worktree / "frontend"),
            (["npm", "run", "build"], worktree / "frontend"),
            (["uv", "run", "python", "tools/version.py"], worktree),
            (
                ["uv", "run", "python", "tools/build_release.py", "--assets", "--out", str(out)],
                worktree,
            ),
            (
                [
                    "uv",
                    "run",
                    "python",
                    "tools/validate_release.py",
                    "--dist",
                    str(out),
                    "--commit",
                    sha,
                    "--expect-channel",
                    "development",
                ],
                worktree,
            ),
        ]
        for command, cwd in commands:
            print(f"Running: {shlex.join(command)}", flush=True)
            run(command, cwd=cwd, env=env)
        # Only this tool has used the newly created tree; no service is started in it.
        # Check tracked, untracked and ignored content before removing build dependencies.
        inventory = git("status", "--porcelain", "--ignored", root=worktree).splitlines()
        if any(not line.startswith("!! ") for line in inventory):
            raise SystemExit("build worktree has source changes; preserving it for inspection")
        git("worktree", "remove", str(worktree), root=root)
    except BaseException:
        print(f"Build incomplete. Preserved worktree: {worktree}\nBuild environment: {scratch}")
        raise
    shutil.rmtree(scratch)
    (package,) = out.glob("maestro-*.tar.gz")
    result = {
        "commit": sha,
        "version": version,
        "package": str(package),
        "checksums": str(out / "SHA256SUMS"),
    }
    print(json.dumps(result, indent=2), flush=True)
    print(
        f"Copy the package to the host, then run: sudo maestroctl update {shlex.quote(package.name)}"
    )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", help="committed Git ref or SHA; defaults to <remote>/dev")
    parser.add_argument("--remote", default="origin")
    parser.add_argument(
        "--out", type=Path, help="new output directory; defaults to dist/development-<SHA>"
    )
    return build(parser.parse_args(argv))


if __name__ == "__main__":
    main()
