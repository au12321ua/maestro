"""Members never learn which credential a model is bound to; secret author
parameters take generic secrets only, never a model API key."""

from support import create_pat, invite_member, running, setup_admin, submit
from test_configuration import agent_body, resolve
from test_datasets import import_and_prepare
from test_e2e_run import prepare_model

from maestro.testing.fakes import FakeDatasetSource


def _credential(admin, name, kind="model_api_key"):
    return admin.post(
        "/api/v1/admin/credentials",
        json={"name": name, "kind": kind, "provider": "demo", "value": f"{name}-value-0000"},
        expect=201,
    ).json()["data"]["id"]


def test_only_an_administrator_session_sees_the_model_credential(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        model_id, harness_id = prepare_model(admin)
        bound = admin.get(f"/api/v1/model-configs/{model_id}", expect=200).json()["data"]
        bound = bound["credential_id"]
        member = invite_member(service, admin, "member")
        admin_pat = create_pat(admin, "experiment")

        shown = resolve(admin, version_id, **agent_body(model_id, harness_id))
        assert shown["effective_config"]["model"]["credential_id"] == bound
        for viewer in (member, admin_pat):
            hidden = resolve(viewer, version_id, **agent_body(model_id, harness_id))
            assert "credential_id" not in hidden["effective_config"]["model"]
            assert bound not in str(hidden)
            # The digest still covers the binding: the same configuration either way.
            assert hidden["config_digest"] == shown["config_digest"]

        run_id = submit(member, version_id, **agent_body(model_id, harness_id)).json()["data"][
            "run_id"
        ]
        seen = member.get(f"/api/v1/runs/{run_id}", expect=200).json()["data"]["snapshot"]
        assert bound not in str(seen)
        assert "credential_id" not in seen["effective"]["model"]
        full = admin.get(f"/api/v1/runs/{run_id}", expect=200).json()["data"]["snapshot"]
        assert full["effective"]["model"]["credential_id"] == bound
        assert bound in full["provenance"]["credential_ids"]


def test_a_changed_model_credential_shows_as_a_hidden_difference(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        model_id, harness_id = prepare_model(admin)
        member = invite_member(service, admin, "member")
        source = submit(member, version_id, **agent_body(model_id, harness_id)).json()["data"]
        current = admin.get(f"/api/v1/model-configs/{model_id}", expect=200).json()["data"]
        replacement = _credential(admin, "replacement")
        admin.put(
            f"/api/v1/admin/model-configs/{model_id}",
            json={
                "expected_revision": current["revision"],
                **{
                    k: current[k]
                    for k in ("provider", "protocol", "endpoint", "model_id", "params")
                },
                "credential_id": replacement,
            },
            expect=200,
        )
        modified = submit(
            member,
            version_id,
            source_run_id=source["run_id"],
            **agent_body(model_id, harness_id),
        ).json()["data"]
        seen = member.get(f"/api/v1/runs/{modified['run_id']}", expect=200).json()["data"]
        changes = {d["path"]: d for d in seen["snapshot"]["provenance"]["source"]["differences"]}
        assert changes["/model/credential_id"] == {
            "path": "/model/credential_id",
            "before": None,
            "after": None,
            "hidden": True,
        }
        assert replacement not in str(seen)


def test_a_secret_parameter_cannot_take_a_model_api_key(service, source):
    files = {
        "needs-token": {
            **FakeDatasetSource.task_files("demo", "params", "needs-token"),
            "task.toml": (
                '[environment]\ncpus = 1\nmemory_mb = 2048\n[verifier.env]\nTOKEN = "${GRADER_TOKEN}"\n'
            ),
        },
    }
    source.add("demo", "params", "v1", list(files), files=files)
    with running(service) as client:
        admin = setup_admin(service, client)
        version_id, _ = import_and_prepare(admin, name="params")
        model_key = _credential(admin, "model-key")
        found = resolve(
            admin, version_id, parameters={"GRADER_TOKEN": {"credential_id": model_key}}
        )
        refused = [b for b in found["blockers"] if b["code"] == "CREDENTIAL_UNAVAILABLE"]
        assert [b["reason"] for b in refused] == ["WRONG_KIND"]
        generic = _credential(admin, "grader", kind="generic_secret")
        found = resolve(admin, version_id, parameters={"GRADER_TOKEN": {"credential_id": generic}})
        assert found["blockers"] == []
