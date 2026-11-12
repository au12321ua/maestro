"""The Runner's attachment installer without Docker.

A stand-in environment runs the installer's real shell commands on the host, with the
container paths mapped into a temporary directory, so copying, the file-by-file check,
the time limit inside the "container" and the records are all exercised for real.
"""

import asyncio
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from maestro.execution.attachments import AttachmentStore, describe, files_digest


class Container:
    """Maps the container paths the installer uses below ``root`` and runs commands."""

    def __init__(self, root: Path, agents, corrupt: bool = False):
        self.root = root
        self.environment_dir = root / "environment"
        agents.TASK_NAMES[str(self.environment_dir.resolve())] = "author-task"
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
    from support.attachments import package, store_package

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


WRAPPER_DRIVER = r"""
import asyncio, contextlib, json, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from maestro_runner import agents
from harbor.agents.oracle import OracleAgent
from harbor.models.agent.context import AgentContext
from harbor.models.task.task import Task
from harbor.models.trial.paths import TrialPaths
from harbor.trial.trial import Trial
from harbor.trial.single_step import SingleStepTrial

root, task_dir, mode, exit_code, corrupt = Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], int(sys.argv[5]), sys.argv[6] == "True"
package = root / "package"
package.mkdir()
(package / "bootstrap.sh").write_text("#!/bin/bash\n")
files = agents.describe(package)
agents.ATTACHMENTS[:] = [{"name": "files", "label": "v1", "kind": "submission" if mode == "submission" else "attachment", "content_digest": agents.content_digest(files), "path": str(package), "bootstrap": True, "files": files}]
records, steps, scopes = [], [], []
agents.INSTALL.update(timeout_sec=10, env={"HTTP_PROXY": "http://setup.test:80", "npm_config_registry": "https://setup.test"}, report=lambda n,r: records.append(r))

class Environment:
    default_user = "worker"
    os = "linux"
    environment_dir = task_dir / "environment"
    capabilities = SimpleNamespace(mounted=True)
    overlay = {}
    @contextlib.contextmanager
    def scoped_exec_env(self, env):
        before = self.overlay
        self.overlay = {**before, **env}
        try: yield
        finally: self.overlay = before
    async def upload_dir(self, source_dir, target_dir):
        if str(target_dir) == "/solution": steps.append("upload_solution")
    async def exec(self, command, **kwargs):
        env = {**self.overlay, **(kwargs.get("env") or {})}
        if "sha256sum" in command:
            output = "" if corrupt else "".join(f"{sha}  ./{path}\n" for path,sha,_ in files) + "--maestro--\n" + "".join(f"./{path}\n" for path,_,x in files if x)
            return SimpleNamespace(return_code=0, stdout=output)
        if "bash bootstrap.sh" in command:
            steps.append("install")
            assert env["MAESTRO_TASK_NAME"] == "author task"
            assert env["MAESTRO_ATTACHMENT_DIR"].endswith("00-files")
            assert env["HTTP_PROXY"] == "http://setup.test:80"
            return SimpleNamespace(return_code=exit_code)
        if "/solution/solve.sh" in command and "chmod" not in command:
            steps.append("solve")
            assert "HTTP_PROXY" not in env and "npm_config_registry" not in env
        return SimpleNamespace(return_code=0)

env = Environment()
agents.TASK_NAMES[str(env.environment_dir.resolve())] = "author task"
paths = TrialPaths(trial_dir=root / "trial")
paths.agent_dir.mkdir(parents=True)
if mode == "oracle":
    cls = agents.MaestroOracle
    assert issubclass(cls, OracleAgent)
    agent = cls(logs_dir=paths.agent_dir, task_dir=task_dir, trial_paths=paths)
else:
    agent = agents.MaestroSubmission(logs_dir=paths.agent_dir)

class Harness:
    config = SimpleNamespace(install_only=False, trial_name="trial")
    result = SimpleNamespace(exception_info=None)
    task = Task(task_dir)
    logger = SimpleNamespace(debug=lambda *a, **kw: None)
    async def _prepare(self): await agent.setup(env)
    async def _run(self): await SingleStepTrial._run(self)
    async def _run_agent(self): await agent.run("", env, AgentContext())
    async def _upload_agent_logs(self): pass
    async def _collect_artifacts(self, **kw): pass
    async def _stop_agent_environment(self): pass
    async def _run_verifier(self):
        assert not env.overlay
        steps.append("verify")
    async def _emit(self, *a): pass
    async def _close_bridge(self): pass
    async def _recover_outputs(self): pass
    async def _finalize(self): pass
    def _init_result(self): pass
    def _record_exception(self, exc): self.result.exception_info = type(exc).__name__
    def _scrub_jobs_dir(self): pass
    def _close_logger_handler(self): pass

asyncio.run(Trial.run(Harness()))
print(json.dumps({"steps": steps, "records": records[-1], "error": Harness.result.exception_info, "overlay": env.overlay}))
"""


@pytest.mark.parametrize(
    "mode, exit_code, corrupt, expected, error",
    [
        ("oracle", 0, False, ["install", "upload_solution", "solve", "verify"], None),
        ("submission", 0, False, ["install", "verify"], None),
        ("oracle", 3, False, ["install"], "AttachmentInstallFailed"),
        ("oracle", 124, False, ["install"], "AttachmentInstallTimeout"),
        ("submission", 3, False, ["install"], "AttachmentInstallFailed"),
        ("submission", 124, False, ["install"], "AttachmentInstallTimeout"),
        # A copy that differs from the package is a transfer fault, as for attachments.
        ("submission", 0, True, [], "AttachmentTransferFailed"),
    ],
)
def test_no_harness_wrappers_follow_harbor_lifecycle_and_scope_setup_env(
    tmp_path, mode, exit_code, corrupt, expected, error
):
    import json
    import subprocess

    from support import FIXTURES
    from support.runner import RUNNER, RUNNER_SRC

    if not RUNNER.exists():
        pytest.skip("Runner environment not installed")
    result = subprocess.run(
        [
            str(RUNNER),
            "-I",
            "-c",
            WRAPPER_DRIVER,
            str(RUNNER_SRC),
            str(tmp_path),
            str(FIXTURES / "harbor-tasks/oracle-pass"),
            mode,
            str(exit_code),
            str(corrupt),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        },
    )
    assert result.returncode == 0, result.stderr
    found = json.loads(result.stdout)
    assert found["steps"] == expected
    assert found["error"] == error and found["overlay"] == {}
    assert found["records"][0]["status"] == (
        "ok"
        if error is None
        else "transfer_mismatch"
        if corrupt
        else "timeout"
        if exit_code == 124
        else "failed"
    )


@pytest.mark.parametrize(
    "mode, with_package", [("oracle", False), ("oracle", True), ("submission", True)]
)
@pytest.mark.parametrize("explicit_name", [False, True])
def test_job_preserves_author_task_name_and_only_wraps_oracle_when_installing(
    tmp_path, mode, with_package, explicit_name
):
    import json
    import subprocess

    from support import FIXTURES
    from support.runner import RUNNER, RUNNER_SRC

    if not RUNNER.exists():
        pytest.skip("Runner environment not installed")
    cached = tmp_path / "sha256-cache-name"
    shutil.copytree(FIXTURES / "harbor-tasks/oracle-pass", cached)
    if explicit_name:
        with (cached / "task.toml").open("a") as handle:
            handle.write('\n[task]\nname = "author/task-name"\n')
    package = stored(tmp_path, "files", "#!/bin/bash\n")
    if mode == "submission":
        package["kind"] = "submission"
    spec = {
        "output_dir": str(tmp_path / "output"),
        "retry": {
            "max_retries": 0,
            "exclude_exceptions": [],
            "min_wait_sec": 1,
            "wait_multiplier": 1,
            "max_wait_sec": 1,
        },
        "artifact_limits": {},
        "tasks": [{"path": str(cached), "task_key": "catalog-task", "trial_ids": ["trial"]}],
        "agent": {"name": mode},
        "attachments": [package] if with_package else [],
        "bootstrap_timeout_sec": 17 if with_package else None,
        # Forces a private numeric task copy without requiring network enforcement.
        "network": {"agent": {"mode": "public"}, "verifier": {"mode": "public"}},
        "labels": {},
        "n_concurrent_trials": 1,
    }
    driver = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from maestro_runner import agents, job
from harbor.agents.factory import AgentFactory
from harbor.models.agent.name import AgentName
spec = json.loads(sys.argv[2])
execution = job.Execution(spec)
config = execution._config()
private = config.tasks[0].path
assert private.name == "0" and private != Path(spec["tasks"][0]["path"])
agent = config.agents[0]
job.emit("probe", task_name=agents.TASK_NAMES[str((private / "environment").resolve())],
    setup_timeout=agent.override_setup_timeout_sec,
    oracle_class=AgentFactory._AGENT_MAP[AgentName.ORACLE],
    nop_class=AgentFactory._AGENT_MAP[AgentName.NOP],
    agent_name=agent.name, import_path=agent.import_path)
"""
    result = subprocess.run(
        [str(RUNNER), "-I", "-c", driver, str(RUNNER_SRC), json.dumps(spec)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        },
    )
    assert result.returncode == 0, result.stderr
    found = json.loads(result.stdout.splitlines()[-1])["payload"]
    assert found["task_name"] == ("author/task-name" if explicit_name else "catalog-task")
    assert found["setup_timeout"] == (17 + 120 if with_package else None)
    if mode == "oracle":
        assert found["oracle_class"] == (
            "maestro_runner.agents:MaestroOracle"
            if with_package
            else "harbor.agents.oracle:OracleAgent"
        )
    else:
        # Harbor keeps its utility-agent rules for the "nop" name.
        assert found["agent_name"] == "nop" and found["import_path"] is None
        assert found["nop_class"] == "maestro_runner.agents:MaestroSubmission"
