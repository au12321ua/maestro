"""The Python SDK against the real application through the TestClient transport.

Retries keep the idempotency key, errors never carry the token, attachments publish
directories and commits, every public operation has an SDK method, the SDK imports
without the service, and the generated files match the API.
"""

import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import httpx
import maestro_sdk.skills
import maestro_sdk.workspace
import pytest
from maestro_sdk import (
    ApiError,
    MaestroClient,
    TransportError,
    WaitTimeout,
)
from maestro_sdk.resources.attachments import files_digest
from support import ROOT, create_pat, running, setup_admin
from support.sdk import sdk

# --- story one: datasets ----------------------------------------------------


# --- story two: runs -------------------------------------------------------


def test_client_timeout_does_not_cancel_the_run(service, version_id, executor):
    executor.script = {"add-01": ["hang"]}
    with running(service) as client:
        admin = setup_admin(service, client)
        agent = sdk(create_pat(admin, ("experiment",)))
        config = {"dataset_version_id": version_id, "execution_mode": "oracle"}
        submitted = agent.runs.submit(config, name="slow")
        with pytest.raises(WaitTimeout) as timeout:
            agent.operations.wait(submitted.operation_id, timeout=0.2, interval=0.05)
        assert timeout.value.last.status in {"accepted", "running"}
        assert agent.runs.get(submitted.run_id).state in {"queued", "running"}
        agent.runs.cancel(submitted.run_id)
        assert agent.operations.wait(submitted.operation_id, interval=0.02).status == "cancelled"


# --- story three: query and export ----------------------------------------


# --- transport ----------------------------------------------------------------------------


class LoseFirstAnswer(httpx.BaseTransport):
    """Delivers the request, then drops the first answer to a matching one."""

    def __init__(self, inner, method, path):
        self.inner, self.method, self.path = inner, method, path
        self.lost = 0

    def handle_request(self, request):
        response = self.inner.handle_request(request)
        if request.method == self.method and request.url.path == self.path and not self.lost:
            self.lost += 1
            response.read()
            raise httpx.ReadError("connection reset", request=request)
        return response


def test_a_lost_answer_is_retried_under_the_same_key(service, version_id):
    """A lost response: one run, one unit of quota, the original answer."""
    with running(service) as client:
        admin = setup_admin(service, client)
        pat = create_pat(admin, ("experiment",), run_limit=2)
        transport = LoseFirstAnswer(pat.client._transport, "POST", "/api/v1/runs")
        http = httpx.Client(base_url="http://testserver", transport=transport)
        agent = sdk(pat, http=http)
        config = {"dataset_version_id": version_id, "execution_mode": "oracle"}
        submitted = agent.runs.submit(config, name="once", client_request_id="lost-answer")
        assert transport.lost == 1 and submitted.replayed
        assert len(agent.runs.find("lost-answer")) == 1
        pats = sdk(pat).me()
        assert pats.pat_id
        (record,) = admin.get("/api/v1/me/pats").json()["data"]
        assert record["accepted_runs"] == 1

        # A plain POST is not repeated: the caller learns the outcome is unknown.
        transport = LoseFirstAnswer(pat.client._transport, "POST", "/api/v1/exports")
        http = httpx.Client(base_url="http://testserver", transport=transport)
        with pytest.raises(TransportError):
            sdk(pat, http=http).exports.create([submitted.run_id])


def test_errors_are_structured_and_never_carry_the_token(service):
    with running(service) as client:
        setup_admin(service, client)
        bad = MaestroClient("http://testserver", "mae_pat_not-a-real-token", http=client)
        with pytest.raises(ApiError) as error:
            bad.me()
        assert error.value.status == 401 and error.value.request_id
        assert "not-a-real-token" not in str(error.value) and "not-a-real" not in repr(bad)
    with pytest.raises(ValueError):
        MaestroClient("http://user:secret@example.test", "t")


def test_attachments_publish_fetch_and_compare_directories(service, tmp_path):
    package = tmp_path / "tools"
    (package / "bin").mkdir(parents=True)
    (package / ".git").mkdir()
    (package / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (package / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "helper",
                "effects": ["agent_tools"],
                "compatible_harnesses": ["*"],
            }
        )
    )
    (package / "bin" / "run.sh").write_text("#!/bin/sh\necho hi\n")
    (package / "bin" / "run.sh").chmod(0o755)
    with running(service) as client:
        admin = setup_admin(service, client)
        agent = sdk(create_pat(admin, ("experiment",)))
        published = agent.attachments.publish(package, label="v1")
        assert published.name == "helper" and published.label == "v1"
        assert [f.path for f in published.files] == ["bin/run.sh", "manifest.json"]
        assert agent.attachments.publish(package).id == published.id  # same content
        (package / "bin" / "run.sh").write_text("#!/bin/sh\necho changed\n")
        child = agent.attachments.publish(package, parent=published)
        assert child.parent_id == published.id and child.id != published.id
        diff = agent.attachments.diff(child)
        assert [c.path for c in diff.changed] == ["bin/run.sh"] and not diff.added
        assert [a.id for a in agent.attachments.list(name="helper")] == [published.id, child.id]

        # Fetching rebuilds the snapshot: only what differs is written, modes included.
        copy = tmp_path / "copy"
        agent.attachments.fetch(published, copy)
        assert (copy / "bin" / "run.sh").read_text() == "#!/bin/sh\necho hi\n"
        assert (copy / "bin" / "run.sh").stat().st_mode & 0o100
        (copy / "extra").write_text("mine")
        agent.attachments.fetch(child, copy)
        assert (copy / "bin" / "run.sh").read_text() == "#!/bin/sh\necho changed\n"
        assert (copy / "extra").exists()
        agent.attachments.fetch(child, copy, prune=True)
        assert not (copy / "extra").exists()
        archive = agent.attachments.download(child, tmp_path / "helper.zip")
        with zipfile.ZipFile(archive) as opened:
            assert min(opened.namelist()).endswith("bin/run.sh")

        # Destination links never change a file outside the requested tree, even if
        # the linked contents already match and only chmod would have been needed.
        outside = tmp_path / "outside"
        outside.mkdir()
        victim = outside / "run.sh"
        victim.write_bytes(b"#!/bin/sh\necho hi\n")
        victim.chmod(0o600)
        root_link = tmp_path / "root-link"
        root_link.symlink_to(outside, target_is_directory=True)
        with pytest.raises(ValueError, match="link"):
            agent.attachments.fetch(published, root_link / "nested")
        assert not (outside / "nested").exists()
        unsafe = tmp_path / "unsafe"
        unsafe.mkdir()
        (unsafe / "bin").symlink_to(outside, target_is_directory=True)
        with pytest.raises(ValueError, match="link"):
            agent.attachments.fetch(published, unsafe, prune=True)
        assert victim.read_bytes() == b"#!/bin/sh\necho hi\n"
        assert victim.stat().st_mode & 0o777 == 0o600
        assert not (unsafe / "manifest.json").exists()  # preflight, no partial update
        (unsafe / "bin").unlink()
        (unsafe / "bin").mkdir()
        (unsafe / "bin" / "run.sh").symlink_to(victim)
        with pytest.raises(ValueError, match="link"):
            agent.attachments.fetch(published, unsafe)
        assert victim.stat().st_mode & 0o777 == 0o600

        # Hard links are replaced before permissions are applied locally.
        (unsafe / "bin" / "run.sh").unlink()
        (unsafe / "bin" / "run.sh").hardlink_to(victim)
        agent.attachments.fetch(published, unsafe)
        assert victim.stat().st_mode & 0o777 == 0o600
        assert (unsafe / "bin" / "run.sh").stat().st_ino != victim.stat().st_ino

        # Repeated fetches can change a file to a directory and back with prune.
        (package / "bin" / "run.sh").unlink()
        (package / "bin").rmdir()
        (package / "bin").write_text("now a file")
        flat = agent.attachments.publish(package)
        with pytest.raises(ValueError, match="prune=True"):
            agent.attachments.fetch(flat, copy)
        agent.attachments.fetch(flat, copy, prune=True)
        assert (copy / "bin").read_text() == "now a file"
        agent.attachments.fetch(published, copy, prune=True)
        assert (copy / "bin" / "run.sh").read_text() == "#!/bin/sh\necho hi\n"

        (package / "link").symlink_to("manifest.json")
        with pytest.raises(ValueError):
            agent.attachments.publish(package)


def git(repository: Path, *args: str) -> str:
    command = ["git", "-C", str(repository), "-c", "user.name=t", "-c", "user.email=t@t"]
    return subprocess.run([*command, *args], check=True, capture_output=True, text=True).stdout


def test_attachments_publish_what_a_commit_tracks(service, tmp_path):
    repository = tmp_path / "helper"
    (repository / "bin").mkdir(parents=True)
    git(repository, "init", "-q", "-b", "main")
    (repository / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "helper",
                "effects": ["agent_tools"],
                "compatible_harnesses": ["*"],
            }
        )
    )
    (repository / "bin" / "run.sh").write_text("#!/bin/sh\necho hi\n")
    (repository / "bin" / "run.sh").chmod(0o755)
    (repository / "same.txt").write_text("#!/bin/sh\necho hi\n")  # same content, mode 644
    (repository / ".gitignore").write_text("cache/\n")
    git(repository, "add", ".")
    git(repository, "commit", "-q", "-m", "v0")
    (repository / "cache").mkdir()
    (repository / "cache" / "big.bin").write_bytes(b"ignored")
    (repository / "untracked.txt").write_text("not committed")
    with running(service) as client:
        admin = setup_admin(service, client)
        agent = sdk(create_pat(admin, ("experiment",)))
        with pytest.warns(UserWarning, match="uncommitted changes"):
            v0 = agent.attachments.publish_commit(repository, label="v0")  # labels still work
        files = {f.path: f.executable for f in v0.files}
        assert files == {
            ".gitignore": False,
            "bin/run.sh": True,
            "manifest.json": False,
            "same.txt": False,
        }
        assert v0.content_digest == files_digest(
            [{"path": f.path, "sha256": f.sha256, "executable": f.executable} for f in v0.files]
        )
        # The commit is published, not the working tree; the same commit is the same snapshot.
        (repository / "bin" / "run.sh").write_text("#!/bin/sh\necho changed\n")
        with pytest.warns(UserWarning):
            assert agent.attachments.publish_commit(repository).id == v0.id
        git(repository, "commit", "-q", "-am", "change")
        (repository / "untracked.txt").unlink()
        child = agent.attachments.publish_commit(repository, parent=v0)
        assert child.parent_id == v0.id
        assert agent.attachments.publish_commit(repository, "HEAD~1").id == v0.id
        assert [c.path for c in agent.attachments.diff(child).changed] == ["bin/run.sh"]

        with pytest.raises(ValueError, match="inside"):
            agent.attachments.publish_commit(repository / "bin")
        with pytest.raises(ValueError, match="rev-parse"):
            agent.attachments.publish_commit(repository, "no-such-branch")
        (repository / "link").symlink_to("manifest.json")
        git(repository, "add", "link")
        git(repository, "commit", "-q", "-m", "link")
        with pytest.raises(ValueError, match="link"):
            agent.attachments.publish_commit(repository)
        git(repository, "rm", "-q", "link")
        commit = git(repository, "rev-parse", "HEAD").strip()
        git(repository, "update-index", "--add", "--cacheinfo", f"160000,{commit},vendor")
        git(repository, "commit", "-q", "-m", "submodule")
        with pytest.warns(UserWarning), pytest.raises(ValueError, match="submodule"):
            agent.attachments.publish_commit(repository)


def test_the_sdk_computes_the_services_content_digest():
    from maestro.execution.attachments import files_digest as service_digest

    files = [
        {"path": "z/é.txt", "sha256": "a" * 64, "executable": False},
        {"path": "bootstrap.sh", "sha256": "b" * 64, "executable": True},
        {"path": "manifest.json", "sha256": "c" * 64, "executable": False},
    ]
    assert files_digest(files) == service_digest(sorted(files, key=lambda f: f["path"]))


def test_skills_install_and_workspace_init(tmp_path):
    root = maestro_sdk.workspace.init(tmp_path / "work")
    assert (root / "notes").is_dir() and (root / "attachments").is_dir()
    assert "/attachments/" in (root / ".gitignore").read_text()
    assert not (root / "notes" / "attachments-workflow.md").exists()
    assert (root / "AGENTS.md").read_text() == (
        ROOT / "sdk" / "src" / "maestro_sdk" / "templates" / "AGENTS.md"
    ).read_text()
    shipped = maestro_sdk.skills.names()
    assert {"maestro-sdk", "maestro-package"} <= set(shipped)
    for target in maestro_sdk.skills.TARGETS:
        for name in shipped:
            skill = root / target / name
            assert (skill / "SKILL.md").read_text() == (
                maestro_sdk.skills.source() / name / "SKILL.md"
            ).read_text()
            assert maestro_sdk.skills.installed_version(skill) == maestro_sdk.skills.version()

    # Again: the person's files stay, the skills are replaced as a whole.
    (root / "AGENTS.md").write_text("mine")
    (root / "notes" / "a.md").write_text("note")
    stale = root / ".claude" / "skills" / "maestro-sdk" / "old.md"
    stale.write_text("from an older SDK")
    other = root / ".claude" / "skills" / "someone-else"
    other.mkdir()
    maestro_sdk.workspace.init(root)
    assert (root / "AGENTS.md").read_text() == "mine"
    assert (root / "notes" / "a.md").read_text() == "note"
    assert not stale.exists() and other.is_dir()


def test_workspace_attachment_workflow_adds_notes_without_overwriting(tmp_path):
    root = maestro_sdk.workspace.init(tmp_path / "work", workflow="attachments")
    notes = root / "notes" / "attachments-workflow.md"
    assert "notes/attachments-workflow.md" in (root / "AGENTS.md").read_text()
    assert "prepare_candidate" in notes.read_text()
    notes.write_text("my workflow notes")
    (root / "AGENTS.md").write_text("my context")
    maestro_sdk.workspace.init(root, workflow="attachments")
    assert notes.read_text() == "my workflow notes"
    assert (root / "AGENTS.md").read_text() == "my context"

    plain = maestro_sdk.workspace.init(tmp_path / "plain")
    context = (plain / "AGENTS.md").read_bytes()
    maestro_sdk.workspace.init(plain, workflow="attachments")
    assert (plain / "notes" / "attachments-workflow.md").is_file()
    assert (plain / "AGENTS.md").read_bytes() == context
    with pytest.raises(ValueError, match="available workflows: attachments"):
        maestro_sdk.workspace.init(tmp_path / "unknown", workflow="unknown")
    assert not (tmp_path / "unknown").exists()


# --- contract -----------------------------------------------------------------------------

# Web-account flows (session, setup, invitations, own PATs and password) and administration
# need a browser session by design; health checks are for the deployment.
NOT_FOR_PATS = (
    "/api/v1/admin/",
    "/api/v1/session",
    "/api/v1/setup",  # first administrator; ``/api/v1/setup-network`` is for PATs too
    "/api/v1/invitations/",
    "/api/v1/me/",
    "/api/v1/dataset-search",
    "/health/",
)
SDK_METHODS = {
    "capabilities": "capabilities",
    "get_me": "me",
    "system_status": "system_status",
    "list_sources": "catalog.sources",
    "list_datasets": "catalog.datasets",
    "get_dataset_version": "catalog.version",
    "list_tasks": "catalog.tasks",
    "get_preparation": "catalog.preparation",
    "list_model_configs": "configs.models",
    "get_model_config": "configs.model",
    "list_harnesses": "configs.harnesses",
    "get_harness": "configs.harness",
    "list_credential_names": "configs.credential_names",
    "get_team_policy": "configs.team_policy",
    "get_setup_network": "configs.setup_network",
    "list_setup_network_revisions": "configs.setup_network_revisions",
    "resolve_config": "configs.resolve",
    "get_resolution": "configs.resolution",
    "list_attachments": "attachments.list",
    "get_attachment": "attachments.get",
    "missing_contents": "attachments.publish",
    "upload_content": "attachments.publish",
    "publish_attachment": "attachments.publish",
    "download_file": "attachments.fetch",
    "download_archive": "attachments.download",
    "attachment_diff": "attachments.diff",
    "attachment_enabled": "attachments.set_enabled",
    "delete_attachment": "attachments.delete",
    "list_plans": "plans.list",
    "get_plan": "plans.get",
    "create_plan": "plans.create",
    "update_plan": "plans.update",
    "copy_plan": "plans.copy",
    "archive_plan": "plans.archive",
    "delete_plan": "plans.delete",
    "compare_runs": "comparisons.compare",
    "list_collections": "collections.list",
    "create_collection": "collections.create",
    "get_collection": "collections.get",
    "update_collection": "collections.update",
    "copy_collection": "collections.copy",
    "delete_collection": "collections.delete",
    "compare_collection": "collections.compare",
    "list_collection_candidates": "collections.candidates",
    "add_collection_runs": "collections.add_runs",
    "label_collection_run": "collections.label_run",
    "remove_collection_run": "collections.remove_run",
    "list_collection_events": "collections.events",
    "list_snapshots": "snapshots.list",
    "create_snapshot": "snapshots.create",
    "get_snapshot": "snapshots.get",
    "delete_snapshot": "snapshots.delete",
    "add_snapshot_note": "snapshots.add_note",
    "update_snapshot_note": "snapshots.update_note",
    "submit_run": "runs.submit",
    "rerun": "runs.rerun",
    "run_request": "runs.request",
    "get_run": "runs.get",
    "list_runs": "runs.list",
    "run_trials": "runs.trials",
    "run_summary": "runs.summary",
    "cancel_run": "runs.cancel",
    "delete_run": "runs.delete",
    "run_artifacts": "runs.artifacts",
    "download_run_archive": "runs.download_archive",
    "preview_batch": "batches.preview",
    "submit_batch": "batches.submit",
    "get_batch": "batches.get",
    "list_batches": "batches.list",
    "cancel_batch": "batches.cancel",
    "get_operation": "operations.get",
    "list_operations": "operations.list",
    "download_artifact": "artifacts.download",
    "delete_artifact": "artifacts.delete",
    "create_export": "exports.create",
    "get_export": "exports.get",
    "list_exports": "exports.list",
    "download_export": "exports.download",
}


def test_every_operation_open_to_pats_has_an_sdk_method():
    document = json.loads((ROOT / "openapi" / "maestro-v1.json").read_text())
    public = {
        operation["operationId"]
        for path, methods in document["paths"].items()
        if not path.startswith(NOT_FOR_PATS) or path.startswith("/api/v1/setup-network")
        for operation in methods.values()
    }
    assert public == set(SDK_METHODS)
    client = MaestroClient("http://127.0.0.1:9")
    for dotted in SDK_METHODS.values():
        target = client
        for part in dotted.split("."):
            target = getattr(target, part)
        assert callable(target), dotted


def test_capabilities_report_the_contract_the_sdk_was_built_from(service):
    from maestro_sdk._contract import CONTRACT_DIGEST

    with running(service) as client:
        caps = MaestroClient("http://testserver", http=client).capabilities()
        assert caps.contract_digest == CONTRACT_DIGEST


def test_sdk_imports_without_the_service():
    code = (
        "import sys\n"
        "for name in ('maestro', 'fastapi', 'sqlalchemy', 'harbor'):\n"
        "    sys.modules[name] = None\n"
        "import maestro_sdk\n"
        "import maestro_sdk.workflows.attachments\n"
        "print(maestro_sdk.MaestroClient('http://127.0.0.1:9'))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    for path in (ROOT / "sdk" / "src" / "maestro_sdk").rglob("*.py"):
        text = path.read_text()
        assert "from maestro." not in text and "import maestro." not in text, path


def test_generated_contract_and_sdk_models_are_current():
    """Changing the API without regenerating fails here (web types: npm check:api)."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "generate_clients.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_the_skill_and_the_guide_only_name_real_sdk_calls():
    """The skill (SKILL.md and its references) and sdk/README.md teach agents; a renamed
    method fails here."""
    client = MaestroClient("http://127.0.0.1:9")
    docs = sorted((ROOT / "skills").glob("*/SKILL.md"))
    docs += sorted((ROOT / "skills").glob("*/references/*.md"))
    assert len(docs) > 2
    for doc in (*docs, ROOT / "sdk" / "README.md"):
        calls = set(re.findall(r"client\.([a-z_]+(?:\.[a-z_]+)?)\(", doc.read_text()))
        assert calls or doc.parent.name == "references", doc  # errors.md names none
        for dotted in calls:
            target = client
            for part in dotted.split("."):
                assert hasattr(target, part), f"{doc.name}: client.{dotted}"
                target = getattr(target, part)
