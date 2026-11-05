"""Dataset import, partial admission and catalog access over HTTP with fake boundaries."""

import uuid

from support import create_pat, invite_member, running, setup_admin, submit, wait_operation
from support.datasets import evict, import_and_prepare, import_dataset, tasks_by_key, wait_run

from maestro.testing.fakes import FakeDatasetSource


def test_ds01_import_fixes_content_and_reimport_is_idempotent(service, source):
    with running(service) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "member")
        reader = create_pat(member, permissions=())

        found = admin.get("/api/v1/dataset-search?q=demo&source_kind=fake").json()["data"]
        assert {"namespace": "demo", "name": "arith", "visibility": "public"} in found
        versions = admin.get(
            "/api/v1/dataset-search/versions?source_kind=fake&namespace=demo&name=arith"
        ).json()["data"]
        assert versions[0]["ref"] == "v1"
        preview = admin.get(
            "/api/v1/dataset-search/version?source_kind=fake&namespace=demo&name=arith&ref=v1",
            expect=200,
        ).json()["data"]
        assert len(preview["tasks"]) == 3 and preview["existing_version_id"] is None

        version_id, prepared = import_and_prepare(admin)
        assert prepared["status"] == "succeeded"
        detail = reader.get(f"/api/v1/dataset-versions/{version_id}", expect=200).json()["data"]
        assert detail["resolved_digest"] == preview["resolved_digest"]
        assert detail["materialization_digest"].startswith("sha256:")
        assert (
            detail["task_count"],
            detail["admitted_task_count"],
            detail["submittable_task_count"],
            detail["cache_ready_task_count"],
        ) == (3, 3, 3, 3)
        for task in tasks_by_key(reader, version_id).values():
            assert task["admission"]["state"] == "admitted"
            assert task["can_submit"] and not task["blockers"] and not task["warnings"]
            kinds = {r["resource_kind"]: r for r in task["content"]["resources"]}
            assert kinds["task_content"]["availability"] == "available"
            assert kinds["environment_image"]["availability"] == "available"
            assert task["content"]["material_digest"] != task["content"]["digest"]
            assert task["local_preparation"]["cache_state"] == "ready"
            assert task["resources"]["cpus"] == 1 and task["parameters"] == []

        # The same import again returns the version it already made.
        again = import_dataset(admin)
        assert again["result"] == {"dataset_version_id": version_id, "existing": True}
        datasets = member.get("/api/v1/datasets").json()["data"]
        assert [len(d["versions"]) for d in datasets] == [1]

        # A moved tag is not silently replaced; confirming makes a new version.
        source.add("demo", "arith", "v1", ["add-01", "add-02", "mul-01"], revision="b")
        moved = import_dataset(admin)
        assert moved["status"] == "failed"
        assert moved["error"]["code"] == "CONTENT_REFERENCE_CHANGED"
        assert moved["error"]["previous_version_id"] == version_id
        second, _ = import_and_prepare(admin, accept_changed_content=True)
        assert second != version_id
        old = tasks_by_key(admin, version_id)
        assert all(x["admission"]["state"] == "admitted" for x in old.values())
        events = admin.get("/api/v1/admin/audit-events").json()["data"]
        assert {"dataset.import", "dataset.first_preparation"} & {e["action"] for e in events}


def test_ds02_partial_failure_retry_and_cold_cache(service, source, environment):
    source.add("demo", "tri", "v1", ["a", "b", "c"])
    environment.fail_prepare["c"] = ["transient"]
    with running(service) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "member")
        version_id, prepared = import_and_prepare(admin, name="tri")
        assert prepared["status"] == "failed"
        assert prepared["error"]["code"] == "FIRST_PREPARATION_INCOMPLETE"
        tasks = tasks_by_key(member, version_id)
        assert [tasks[k]["admission"]["state"] for k in "abc"] == [
            "admitted",
            "admitted",
            "first_failed",
        ]
        blocker = tasks["c"]["blockers"][0]
        assert blocker["code"] == "TASK_NOT_ADMITTED"
        assert blocker["first_preparation_error"]["code"] == "PREPARATION_RESTORE_FAILED"
        detail = member.get(f"/api/v1/dataset-versions/{version_id}").json()["data"]
        assert (detail["admitted_task_count"], detail["submittable_task_count"]) == (2, 2)
        assert detail["cache_ready_task_count"] == 2

        first = submit(member, version_id).json()["data"]
        assert first["planned_trials"] == 2
        assert [x["task_key"] for x in first["excluded_tasks"]] == ["c"]
        explicit = submit(
            member, version_id, task_selection={"mode": "explicit", "task_ids": [tasks["c"]["id"]]}
        )
        assert explicit.status_code == 422
        assert explicit.json()["error"]["field_errors"][0]["code"] == "TASK_NOT_ADMITTED"

        # Only an administrator retries the failed task; the retry admits it.
        assert (
            member.post(
                f"/api/v1/admin/dataset-versions/{version_id}/preparations", json={}
            ).status_code
            == 403
        )
        retry = admin.post(
            f"/api/v1/admin/dataset-versions/{version_id}/preparations", json={}, expect=202
        ).json()["data"]
        assert retry["progress"]["task_ids"] == [tasks["c"]["id"]]
        assert wait_operation(admin, retry["id"])["status"] == "succeeded"
        assert all(x["can_submit"] for x in tasks_by_key(member, version_id).values())
        old = wait_run(member, first["run_id"])
        assert [x["task_key"] for x in old["snapshot"]["effective"]["tasks"]] == ["a", "b"]

        # Evicting caches keeps admission; new runs still select and restore them.
        for key in "ab":
            evict(admin, tasks[key])
        cold = tasks_by_key(member, version_id)
        for key in "ab":
            assert cold[key]["admission"]["state"] == "admitted"
            assert cold[key]["local_preparation"]["cache_state"] == "stale"
            assert cold[key]["can_submit"]
            assert cold[key]["warnings"] == [
                {"code": "PREPARATION_CACHE_MISS", "action": "restore_on_run"}
            ]
            assert cold[key]["required_preparation"][0]["action"] == "restore_cache"
        second = submit(member, version_id).json()["data"]
        assert second["planned_trials"] == 3
        assert {x["task_key"]: x["cache_state"] for x in second["cache_observations"]} == {
            "a": "cold",
            "b": "cold",
            "c": "ready",
        }
        run = wait_run(member, second["run_id"])
        assert run["counts"]["scored"] == 3
        assert sorted(environment.loads) == ["a", "b"]
        warm = tasks_by_key(member, version_id)
        assert all(x["local_preparation"]["cache_state"] == "ready" for x in warm.values())
        assert [warm[k]["admission"]["revision"] for k in "ab"] == [
            cold[k]["admission"]["revision"] for k in "ab"
        ]


def test_ds03_sdk_reads_but_cannot_administer(service, source):
    source.add("demo", "solo", "v1", ["solo"])
    with running(service) as client:
        admin = setup_admin(service, client)
        version_id, _ = import_and_prepare(admin, name="solo")
        (task,) = tasks_by_key(admin, version_id).values()
        evict(admin, task)
        member = invite_member(service, admin, "member")
        reader = create_pat(member, permissions=())
        experimenter = create_pat(member, permissions=("experiment",), run_limit=5)
        admin_pat = create_pat(admin, permissions=("experiment",))

        for pat in (reader, experimenter):
            assert pat.get("/api/v1/dataset-search?q=solo&source_kind=fake").status_code == 403
            assert pat.get("/api/v1/datasets").status_code == 200
            listed = pat.get(f"/api/v1/dataset-versions/{version_id}/tasks").json()["data"]
            assert listed[0]["admission"]["state"] == "admitted"
            assert listed[0]["local_preparation"]["cache_state"] == "stale"
            assert pat.get("/api/v1/dataset-sources").json()["data"] == [
                {"kind": "fake", "available": True}
            ]
        for pat in (reader, experimenter, admin_pat):
            body = {"source_kind": "fake", "namespace": "demo", "name": "solo", "ref": "v1"}
            denied = pat.post("/api/v1/admin/dataset-imports", json=body)
            assert denied.status_code == 403
            # Members' tokens are capped by role; the admin's lacks the datasets permission.
            assert denied.json()["error"]["code"] in {"FORBIDDEN", "PAT_PERMISSION_INSUFFICIENT"}
            denied = pat.post(f"/api/v1/admin/dataset-versions/{version_id}/preparations", json={})
            assert denied.status_code == 403
            prep = task["local_preparation"]["id"]
            assert pat.post(f"/api/v1/admin/preparations/{prep}/evict").status_code == 403

        refused = submit(reader, version_id)
        assert refused.status_code == 403
        assert refused.json()["error"]["code"] == "PAT_PERMISSION_INSUFFICIENT"
        accepted = submit(experimenter, version_id).json()["data"]
        run = wait_run(experimenter, accepted["run_id"])
        assert run["counts"]["scored"] == 1  # restored inside the run, no admin step
        prep = reader.get(f"/api/v1/preparations/{task['local_preparation']['id']}").json()
        assert prep["data"]["cache_state"] == "ready" and prep["data"]["pins"] == []


def test_author_parameters_unsupported_tasks_and_revocation(service, source):
    files = {
        "needs-key": {
            **FakeDatasetSource.task_files("demo", "params", "needs-key"),
            "task.toml": (
                "[environment]\ncpus = 2\nmemory_mb = 4096\n"
                '[verifier.env]\nMODE = "${GRADER_MODE:-strict}"\nREGION = "${REGION}"\n'
            ),
        },
        "gpu": {
            **FakeDatasetSource.task_files("demo", "params", "gpu"),
            "task.toml": "[environment]\ngpus = 1\n",
        },
        "plain": FakeDatasetSource.task_files("demo", "params", "plain"),
    }
    source.add("demo", "params", "v1", list(files), files=files)
    with running(service) as client:
        admin = setup_admin(service, client)
        version_id, prepared = import_and_prepare(admin, name="params")
        assert prepared["status"] == "failed"
        tasks = tasks_by_key(admin, version_id)
        params = {p["name"]: p for p in tasks["needs-key"]["parameters"]}
        assert params["GRADER_MODE"]["default"] == "strict"
        assert params["GRADER_MODE"]["required"] is False
        assert params["REGION"]["required"] is True and params["REGION"]["default"] is None
        assert tasks["needs-key"]["resources"]["cpus"] == 2
        assert tasks["needs-key"]["resources"]["constraint_strength"] == "unknown"
        assert tasks["needs-key"]["admission"]["state"] == "admitted"
        # A required parameter is a value the run must give, not a blocker of the task.
        assert tasks["needs-key"]["blockers"] == []
        assert tasks["needs-key"]["required_parameters"] == ["REGION"]
        assert tasks["gpu"]["admission"]["state"] == "first_failed"
        error = tasks["gpu"]["blockers"][0]["first_preparation_error"]
        assert error["code"] == "PREPARATION_UNSUPPORTED" and "gpus" in error["message"]

        plain = tasks["plain"]
        member = invite_member(service, admin, "member")
        revoke = f"/api/v1/admin/task-admissions/{plain['admission']['id']}/revoke"
        body = {"reason": "unsafe verifier", "expected_revision": plain["admission"]["revision"]}
        assert member.post(revoke, json=body).status_code == 403
        revoked = admin.post(revoke, json=body, expect=200).json()["data"]
        assert revoked["state"] == "revoked"
        after = tasks_by_key(member, version_id)["plain"]
        assert after["blockers"][0]["code"] == "TASK_ADMISSION_REVOKED"
        refused = submit(member, version_id)
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == "NO_SUBMITTABLE_TASKS"
        assert {f["code"] for f in refused.json()["error"]["field_errors"]} == {
            "CONFIG_REQUIRED_MISSING",
            "TASK_NOT_ADMITTED",
            "TASK_ADMISSION_REVOKED",
        }


def test_source_errors_are_classified_for_the_caller(service, source):
    with running(service) as client:
        admin = setup_admin(service, client)
        missing = admin.get(
            "/api/v1/dataset-search/version?source_kind=fake&namespace=demo&name=none&ref=v1"
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "SOURCE_VERSION_NOT_FOUND"
        (task,) = [
            t for t in source.versions[("demo", "arith", "v1")].tasks if t.task_key == "add-01"
        ]
        source.fail_fetch[task.content_digest] = ["auth"]
        failed = import_dataset(admin)
        assert failed["status"] == "failed"
        assert failed["error"]["code"] == "SOURCE_AUTH_FAILED"
        assert admin.get("/api/v1/datasets").json()["data"] == []
        unknown = admin.post(
            "/api/v1/admin/dataset-imports",
            json={
                "source_kind": "fake",
                "namespace": "demo",
                "name": "arith",
                "ref": "v1",
                "credential_id": "cred_" + uuid.uuid4().hex[:26],
            },
        )
        assert unknown.status_code == 422


def test_a_task_declaring_a_reserved_name_is_blocked_even_with_only_a_default(service, source):
    files = {
        "evil": {
            **FakeDatasetSource.task_files("demo", "params", "evil"),
            "task.toml": (
                "[environment]\ncpus = 1\nmemory_mb = 2048\n"
                '[verifier.env]\nA = "${LD_PRELOAD:-/tmp/evil.so}"\n'
                'B = "${PATH:-/tmp/bin:/usr/bin}"\n'
                'C = "${HTTPS_PROXY:-http://proxy.example:8080}"\n'
            ),
        },
    }
    source.add("demo", "params", "v1", list(files), files=files)
    with running(service) as client:
        admin = setup_admin(service, client)
        version_id, _ = import_and_prepare(admin, name="params")
        found = admin.post(
            "/api/v1/config-resolutions",
            json={"dataset_version_id": version_id, "execution_mode": "oracle"},
            expect=201,
        ).json()["data"]
        assert found["effective_config"].get("parameters") in ({}, None)
        assert found["blockers"] or found["excluded_tasks"]
