"""Update a Maestro workspace's SDK and skills to match its service."""

import fcntl
import json
import os
import re
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from urllib.request import urlopen

INDEX = "https://au12321ua.github.io/maestro/simple/maestro-sdk/"
RELEASES = "https://github.com/au12321ua/maestro/releases/download"
SCRIPT = Path(__file__).resolve()


def service_version() -> str:
    url = os.environ.get("MAESTRO_URL", "").strip().rstrip("/")
    if not url:
        sys.exit("Set MAESTRO_URL to the address of your Maestro service.")
    try:
        with urlopen(f"{url}/api/v1/capabilities", timeout=10) as response:
            capabilities = json.load(response)["data"]
        version = capabilities["service_version"]
    except (OSError, ValueError, KeyError, TypeError):
        sys.exit("Could not read the Maestro service version. Check MAESTRO_URL and connectivity.")
    if str(capabilities.get("service_build")).endswith("-unreleased"):
        sys.exit("This is an unreleased service. Install the SDK wheel bundled with its build.")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        sys.exit("The Maestro service did not report an X.Y.Z release version.")
    return version


def official_wheel(version: str, release: str) -> str:
    try:
        with urlopen(INDEX, timeout=10) as response:
            index = response.read().decode("utf-8")
    except (OSError, ValueError):
        sys.exit("Could not read the GitHub Pages SDK index.")
    # Use the indexed Release URL and digest so the SDK resolves from its official wheel.
    match = re.search(rf'href="({re.escape(release)}#sha256=[0-9a-f]{{64}})"', index, re.IGNORECASE)
    if match is None:
        sys.exit(f"Maestro SDK {version} is missing from the GitHub Pages index.")
    return match[1]


def sdk_matches(version: str, release: str) -> bool:
    try:
        installed = distribution("maestro-sdk")
    except PackageNotFoundError:
        return False
    if installed.version != version:
        return False
    # Local and editable installs can have the same version as the official release.
    origin = installed.read_text("direct_url.json")
    return origin is None or json.loads(origin)["url"].split("#", 1)[0].lower() == release.lower()


def sync_skills(workspace: Path, version: str, *, refresh: bool = False) -> None:
    from maestro_sdk import skills

    installed_version = skills.version()
    if installed_version != version:
        sys.exit(f"Expected Maestro SDK {version}, found {installed_version}.")
    names = skills.names()
    installed = (workspace / target / name for target in skills.TARGETS for name in names)
    if refresh or any(
        not (path / "SKILL.md").is_file() or skills.installed_version(path) != version
        for path in installed
    ):
        skills.install(workspace)
    print(f"Maestro SDK and skills match service version {version}.")


def update(workspace: Path) -> None:
    environment = workspace / ".venv"
    with (environment / ".maestro-update.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        version = service_version()
        release = f"{RELEASES}/v{version}/maestro_sdk-{version}-py3-none-any.whl"
        # Finish an interrupted update before accepting version markers from the workspace.
        pending = environment / ".maestro-skill-sync-pending"
        if not pending.exists() and sdk_matches(version, release):
            sync_skills(workspace, version)
            return

        wheel = official_wheel(version, release)
        pending.touch()
        try:
            subprocess.run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    sys.executable,
                    "--reinstall-package",
                    "maestro-sdk",
                    f"maestro-sdk @ {wheel}",
                ],
                cwd=workspace,
                check=True,
            )
            # Reload Python's import configuration after replacing the installed SDK.
            subprocess.run(
                [sys.executable, "-I", str(SCRIPT), "--sync-skills", version],
                check=True,
            )
        except FileNotFoundError as error:
            sys.exit(f"Could not run {error.filename}. Install uv and check the workspace .venv.")
        except subprocess.CalledProcessError as error:
            sys.exit(error.returncode)
        pending.unlink(missing_ok=True)


def main() -> None:
    workspace = SCRIPT.parent
    if Path(sys.prefix).resolve() != (workspace / ".venv").resolve():
        sys.exit("Run .venv/bin/python -I update-maestro.py from your Maestro workspace.")

    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "--sync-skills":
        sync_skills(workspace, args[1], refresh=True)
    elif args:
        sys.exit("Usage: .venv/bin/python -I update-maestro.py")
    else:
        update(workspace)


if __name__ == "__main__":
    main()
