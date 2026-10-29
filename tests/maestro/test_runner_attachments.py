"""The Runner's attachment installer (03 section 7) without Docker.

A stand-in environment runs the installer's real shell commands on the host, with the
container paths mapped into a temporary directory, so copying, the file-by-file check,
the time limit inside the "container" and the records are all exercised for real.
"""

import asyncio
import importlib
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from maestro.execution.attachments import AttachmentStore, describe, files_digest

RUNNER_SRC = Path(__file__).resolve().parents[2] / "runner" / "src"


@pytest.fixture
def agents():
    sys.path.insert(0, str(RUNNER_SRC))
    try:
        module = importlib.import_module("maestro_runner.agents")
        yield module
    finally:
        sys.path.remove(str(RUNNER_SRC))
        module.ATTACHMENTS[:] = []
        module.INSTALL.update(timeout_sec=None, report=None, env={})


class Container:
    """Maps the container paths the installer uses below ``root`` and runs commands."""

    def __init__(self, root: Path, agents, corrupt: bool = False):
        self.root = root
        self.prefixes = {agents.TARGET: root / "opt", agents.LOG_DIR: root / "logs"}
        self.corrupt = corrupt
        self.default_user = None
        self.commands = []

    def _map(self, text: str) -> str:
        for prefix, path in self.prefixes.items():
            text = text.replace(prefix, str(path))
        return text

    async def upload_dir(self, source_dir, target_dir):
        target = Path(self._map(target_dir))
        shutil.copytree(source_dir, target, dirs_exist_ok=True)
        if self.corrupt:
            (target / "tools" / "hello").write_text("tampered\n")

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        self.commands.append((command, user))
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            self._map(command),
            cwd=self._map(cwd) if cwd else None,
            env={"PATH": "/usr/bin:/bin", **{k: self._map(v) for k, v in (env or {}).items()}},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout_sec)
        return SimpleNamespace(return_code=proc.returncode, stdout=stdout.decode())


def stored(tmp_path, name: str, script: str | None) -> dict:
    from test_attachments import package, store_package

    store = AttachmentStore(tmp_path / "data")
    files = {"bootstrap.sh": (script.encode(), 0o755)} if script is not None else None
    digest = store_package(store, package(name, bootstrap=False, extra_files=files))
    return {
        "name": name,
        "label": "v1",
        "content_digest": digest,
        "path": str(store.verified(digest)),
        "bootstrap": script is not None,
    }


def prepare(agents, attachments: list[dict], timeout_sec=30):
    reports = []
    loaded = []
    for attachment in attachments:
        files = agents.describe(Path(attachment["path"]))
        # The Runner digests a package exactly as the service did when it was published.
        assert agents.content_digest(files) == attachment["content_digest"]
        assert agents.content_digest(files) == files_digest(describe(Path(attachment["path"])))
        loaded.append({**attachment, "files": files})
    agents.ATTACHMENTS[:] = loaded
    agents.INSTALL.update(
        timeout_sec=timeout_sec, report=lambda name, records: reports.append((name, records))
    )
    return reports


def install(agents, container, trial_dir: Path):
    agent = SimpleNamespace(logs_dir=trial_dir / "agent")
    return asyncio.run(agents.install(agent, container))


MARK = 'echo "installed by $(basename "$MAESTRO_ATTACHMENT_DIR")"; touch "$MAESTRO_ATTACHMENT_DIR/../done-$(basename "$MAESTRO_ATTACHMENT_DIR")"\n'


def test_packages_are_copied_checked_and_installed_in_order(agents, tmp_path):
    first = stored(tmp_path, "sdk", "#!/bin/bash\nset -e\n" + MARK)
    second = stored(tmp_path, "tools", None)
    reports = prepare(agents, [first, second])
    container = Container(tmp_path / "container", agents)
    install(agents, container, tmp_path / "trial-a")
    assert reports[0] == ("trial-a", None)
    assert reports[1][0] == "trial-a"
    records = reports[1][1]
    assert [(r["name"], r["status"]) for r in records] == [("sdk", "ok"), ("tools", "ok")]
    assert records[0]["exit_code"] == 0 and records[0]["target"].endswith("/00-sdk")
    assert (container.root / "opt" / "done-00-sdk").exists()
    log = container.root / "logs" / "00-sdk.log"
    assert log.read_text() == "installed by 00-sdk\n"
    # Platform steps run as root; the script itself runs as the task's agent user.
    script = next(c for c in container.commands if "bootstrap.sh" in c[0])
    assert script[1] is None
    assert all(
        user == "root" for command, user in container.commands if "bootstrap.sh" not in command
    )


def test_a_failing_script_stops_this_trial_with_its_exit_code(agents, tmp_path):
    first = stored(tmp_path, "broken", "#!/bin/bash\necho nope >&2\nexit 3\n")
    second = stored(tmp_path, "tools", None)
    reports = prepare(agents, [first, second])
    container = Container(tmp_path / "container", agents)
    with pytest.raises(agents.AttachmentInstallFailed, match="exited with 3"):
        install(agents, container, tmp_path / "trial-a")
    records = reports[-1][1]
    assert [(r["name"], r["status"], r.get("exit_code")) for r in records] == [
        ("broken", "failed", 3)
    ]
    assert (container.root / "logs" / "00-broken.log").read_text() == "nope\n"
    assert not (container.root / "opt" / "01-tools").exists()


def test_the_time_limit_is_enforced_inside_the_container(agents, tmp_path):
    slow = stored(tmp_path, "slow", "#!/bin/bash\nsleep 60\n")
    reports = prepare(agents, [slow], timeout_sec=1)
    started = time.monotonic()
    with pytest.raises(agents.AttachmentInstallTimeout):
        install(agents, Container(tmp_path / "container", agents), tmp_path / "trial-a")
    assert time.monotonic() - started < 10
    assert reports[-1][1][0]["status"] == "timeout" and reports[-1][1][0]["exit_code"] == 124


def test_a_copy_that_differs_never_runs_its_script(agents, tmp_path):
    package = stored(tmp_path, "sdk", "#!/bin/bash\n" + MARK)
    reports = prepare(agents, [package])
    container = Container(tmp_path / "container", agents, corrupt=True)
    with pytest.raises(agents.AttachmentTransferFailed):
        install(agents, container, tmp_path / "trial-a")
    assert reports[-1][1][0]["status"] == "transfer_mismatch"
    assert not (container.root / "opt" / "done-00-sdk").exists()
    assert not any("bootstrap.sh" in c for c, _ in container.commands)


def test_each_trial_installs_into_its_own_container(agents, tmp_path):
    package = stored(
        tmp_path, "sdk", "#!/bin/bash\nset -e\nls ../done-* 2>/dev/null && exit 9\n" + MARK
    )
    reports = prepare(agents, [package])
    for trial in ("trial-a", "trial-b"):
        install(agents, Container(tmp_path / trial, agents), tmp_path / trial)
    assert [(n, r[0]["status"]) for n, r in reports if r] == [("trial-a", "ok"), ("trial-b", "ok")]


def test_the_setup_proxy_reaches_install_scripts_only(agents, tmp_path):
    package = stored(tmp_path, "sdk", '#!/bin/bash\necho "proxy=$HTTP_PROXY"\n')
    reports = prepare(agents, [package])
    agents.INSTALL["env"] = {"HTTP_PROXY": "http://172.17.0.1:7890"}
    container = Container(tmp_path / "container", agents)
    install(agents, container, tmp_path / "trial-a")
    log = container.root / "logs" / "00-sdk.log"
    assert log.read_text() == "proxy=http://172.17.0.1:7890\n"
    assert reports[-1][1][0]["proxy"] is True
    # Copying and checking run without it.
    assert all(c[0].find("PROXY") < 0 for c in container.commands)


def test_setup_time_covers_installing(agents, tmp_path):
    assert agents.setup_timeout(360) == 360
    prepare(agents, [stored(tmp_path, "sdk", "#!/bin/bash\n")], timeout_sec=600)
    assert agents.setup_timeout(360) == 360 + 600 + agents.TRANSFER_SEC
