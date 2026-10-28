"""M5 configuration: team policy revisions, model configurations and harness versions,
and the resolver shared by the resolution endpoint and run acceptance (03, 04)."""

import uuid

from sqlalchemy import func, select, update
from support import (
    PASSWORD,
    Api,
    create_pat,
    invite_member,
    running,
    setup_admin,
    submit,
    wait_operation,
)
from test_datasets import import_and_prepare, tasks_by_key
from test_e2e_run import prepare_model

from maestro.db import schema as t
from maestro.testing.fakes import FakeDatasetSource


def resolve(api, version_id, expect=201, **body):
    payload = {"dataset_version_id": version_id, "execution_mode": "oracle", **body}
    return api.post("/api/v1/config-resolutions", json=payload, expect=expect).json()["data"]


def agent_body(model_id, harness_id, **extra):
    return {
        "execution_mode": "agent",
        "model_config_id": model_id,
        "harness_version_id": harness_id,
        **extra,
    }


def codes(resolution) -> list[str]:
    return [b["code"] for b in resolution["blockers"]]


def error(response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


# --- team policy --------------------------------------------------------------------


def test_team_policy_revisions_take_effect_for_new_work_only(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "member")
        pat = create_pat(member)
        policy = member.get("/api/v1/team-policy", expect=200).json()["data"]
        assert policy["revision"] == 0 and policy["values"]["repetitions_max"] == 20

        accepted = submit(member, version_id, repetitions=2).json()["data"]
        change = {"values": {"repetitions_max": 1, "pat_default_run_limit": 7}}
        refused = member.put("/api/v1/admin/team-policy", json={**change, "expected_revision": 0})
        assert refused.status_code == 403
        assert (
            pat.put(
                "/api/v1/admin/team-policy", json={**change, "expected_revision": 0}
            ).status_code
            == 403
        )
        updated = admin.put(
            "/api/v1/admin/team-policy", json={**change, "expected_revision": 0}, expect=200
        ).json()["data"]
        assert updated["revision"] == 1 and updated["values"]["repetitions_max"] == 1
        stale = admin.put("/api/v1/admin/team-policy", json={**change, "expected_revision": 0})
        assert error(stale) == (409, "POLICY_REVISION_CHANGED")
        invalid = admin.put(
            "/api/v1/admin/team-policy",
            json={"values": {"repetitions_default": 5, "nope": 1}, "expected_revision": 1},
        )
        assert error(invalid) == (422, "POLICY_INVALID")
        assert {e["path"] for e in invalid.json()["error"]["field_errors"]} == {"/values/nope"}
        above = admin.put(
            "/api/v1/admin/team-policy",
            json={"values": {"repetitions_default": 5}, "expected_revision": 1},
        )
        assert above.json()["error"]["field_errors"][0]["code"] == "ABOVE_LIMIT"

        # The accepted run keeps its configuration; new work sees the new limits.
        run = member.get(f"/api/v1/runs/{accepted['run_id']}", expect=200).json()["data"]
        assert run["snapshot"]["effective"]["repetitions"] == 2
        assert run["snapshot"]["policy"]["team_policy_revision"] == 0
        over = submit(member, version_id, repetitions=2)
        assert error(over) == (422, "CONFIG_VALIDATION_FAILED")
        assert over.json()["error"]["field_errors"][0] == {
            "path": "/repetitions",
            "code": "LIMIT_EXCEEDED",
            "message": "repetitions 2 is over the team limit 1",
            "limit": 1,
        }
        token = member.post("/api/v1/me/pats", json={"name": "x", "scope": "read"}, expect=201)
        assert token.json()["data"]["run_limit"] == 7
        caps = client.get("/api/v1/capabilities").json()["data"]
        assert caps["team_policy_revision"] == 1 and caps["limits"]["repetitions_max"] == 1
        history = admin.get("/api/v1/admin/team-policy/revisions", expect=200).json()["data"]
        assert [h["revision"] for h in history] == [1]
        assert history[0]["created_by_username"] == "admin"


# --- model configurations and harnesses -----------------------------------------------


def test_model_configs_keep_secrets_out_and_revise(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "member")
        base = {
            "name": "m",
            "provider": "acme",
            "protocol": "anthropic",
            "endpoint": "https://api.acme.test",
            "model_id": "acme-1",
        }
        assert member.post("/api/v1/admin/model-configs", json=base).status_code == 403
        bad = admin.post(
            "/api/v1/admin/model-configs",
            json={
                **base,
                "endpoint": "https://user:pw@api.acme.test/v1?key=abc",
                "params": {
                    "api_key": "x",
                    "headers": {"x": "Bearer abcdefghijkl"},
                    "max_tokens": 9,
                },
            },
        )
        assert error(bad) == (422, "MODEL_CONFIG_INVALID")
        assert {(e["path"], e["code"]) for e in bad.json()["error"]["field_errors"]} == {
            ("/endpoint", "CREDENTIALS_IN_URL"),
            ("/params/api_key", "LOOKS_LIKE_SECRET"),
            ("/params/headers/x", "LOOKS_LIKE_SECRET"),
        }
        hub = admin.post(
            "/api/v1/admin/credentials",
            json={"name": "hub", "kind": "hub_token", "provider": "hub", "value": "v"},
            expect=201,
        ).json()["data"]
        wrong = admin.post("/api/v1/admin/model-configs", json={**base, "credential_id": hub["id"]})
        assert wrong.json()["error"]["field_errors"][0]["code"] == "WRONG_KIND"

        created = admin.post("/api/v1/admin/model-configs", json=base, expect=201).json()["data"]
        assert created["revision"] == 1 and created["credential_bound"] is False
        taken = admin.post("/api/v1/admin/model-configs", json=base)
        assert error(taken) == (409, "MODEL_CONFIG_NAME_TAKEN")
        revised = admin.put(
            f"/api/v1/admin/model-configs/{created['id']}",
            json={
                **{k: v for k, v in base.items() if k != "name"},
                "model_id": "acme-2",
                "expected_revision": 1,
            },
            expect=200,
        ).json()["data"]
        assert revised["revision"] == 2 and revised["model_id"] == "acme-2"
        assert [r["model_id"] for r in revised["revisions"]] == ["acme-2", "acme-1"]
        conflict = admin.put(
            f"/api/v1/admin/model-configs/{created['id']}",
            json={**{k: v for k, v in base.items() if k != "name"}, "expected_revision": 1},
        )
        assert error(conflict) == (409, "REVISION_CONFLICT")


def test_harness_versions_are_fixed_and_checked_against_the_executor(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        adapters = admin.get("/api/v1/admin/harness-adapters", expect=200).json()["data"]
        assert adapters == [
            {
                "agent": "fake-agent",
                "protocols": ["anthropic", "openai", "openai-compatible"],
                "parameters": None,
                "model_parameters": None,
                "package": None,
                "tested_version": None,
            }
        ]
        body = {
            "name": "h",
            "version": "1.0",
            "agent": "fake-agent",
            "protocols": ["anthropic"],
            "parameter_schema": {
                "parameters": {
                    "max_turns": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                    "style": {"type": "string", "enum": ["a", "b"]},
                }
            },
        }
        unknown = admin.post("/api/v1/admin/harness-versions", json={**body, "agent": "codex"})
        assert unknown.json()["error"]["field_errors"][0]["path"] == "/agent"
        broken = admin.post(
            "/api/v1/admin/harness-versions",
            json={**body, "parameter_schema": {"parameters": {"x": {"type": "date"}}}},
        )
        assert error(broken) == (422, "REQUEST_INVALID")  # the contract lists the types
        path = broken.json()["error"]["field_errors"][0]["path"]
        assert path == "/parameter_schema/parameters/x/type"
        created = admin.post("/api/v1/admin/harness-versions", json=body, expect=201).json()["data"]
        assert created["content_digest"].startswith("sha256:")
        again = admin.post("/api/v1/admin/harness-versions", json=body)
        assert error(again) == (409, "HARNESS_VERSION_EXISTS")


# --- resolution ---------------------------------------------------------------------------


def test_web_and_pat_resolve_to_the_same_configuration(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        model_id, harness_id = prepare_model(admin)
        member = invite_member(service, admin, "member")
        reader = create_pat(member, scope="read")
        body = agent_body(model_id, harness_id, repetitions=3, concurrency=2)
        web = resolve(member, version_id, **body)
        sdk = resolve(reader, version_id, **body)
        assert web["config_digest"] == sdk["config_digest"]
        assert web["field_sources"] == sdk["field_sources"]
        assert web["blockers"] == sdk["blockers"] == []
        # Resolutions belong to their maker (a person, whichever way they came in).
        assert admin.get(f"/api/v1/config-resolutions/{web['resolution_id']}").status_code == 404
        assert reader.get(f"/api/v1/config-resolutions/{web['resolution_id']}").status_code == 200
        mine = member.get(f"/api/v1/config-resolutions/{web['resolution_id']}", expect=200)
        assert mine.json()["data"]["config_digest"] == web["config_digest"]
        # Reading is open to a read token; submitting is not.
        refused = reader.post(
            "/api/v1/runs",
            json={"name": "x", "resolution_id": sdk["resolution_id"]},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert error(refused) == (403, "PAT_SCOPE_INSUFFICIENT")


def test_limits_block_instead_of_clamping_and_sources_explain_values(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        admin.put(
            "/api/v1/admin/team-policy",
            json={"values": {"agent_timeout_sec_max": 1500}, "expected_revision": 0},
            expect=200,
        )
        # 03 section 3 example: an override over the team limit is an error, never 1500.
        over = resolve(admin, version_id, timeouts={"agent_sec": 1800})
        assert codes(over) == ["LIMIT_EXCEEDED"]
        assert over["blockers"][0]["path"] == "/timeouts/agent_sec"
        shorter = resolve(admin, version_id, timeouts={"agent_sec": 30})
        assert shorter["blockers"] == []
        assert {x["agent_timeout_sec"] for x in shorter["effective_config"]["tasks"]} == {30}
        assert shorter["field_sources"]["/timeouts/agent_sec"] == {"layer": "override"}
        assert shorter["effective_config"]["deviates_from_author_baseline"] is True
        assert shorter["baseline_differences"] == [
            {"path": "/timeouts/agent_sec", "value": 30, "tasks_lower": 3, "tasks_higher": 0}
        ]
        assert [w["code"] for w in shorter["warnings"]] == ["SHORTER_THAN_AUTHOR"]
        plain = resolve(admin, version_id)
        assert plain["field_sources"]["/timeouts/agent_sec"]["layer"] == "author"
        assert plain["field_sources"]["/repetitions"] == {
            "layer": "team",
            "source_id": "team_policy",
            "revision": 1,
        }
        assert plain["effective_config"]["deviates_from_author_baseline"] is False
        # Resources may be raised, never lowered below the author's (U-22).
        more = resolve(admin, version_id, resources={"memory_mb": 4096})
        assert more["blockers"] == []
        assert {x["memory_mb"] for x in more["effective_config"]["tasks"]} == {4096}
        more_cpus = resolve(admin, version_id, resources={"cpus": 2})
        assert more_cpus["blockers"] == []
        less = resolve(admin, version_id, resources={"memory_mb": 1024})
        assert codes(less) == ["BELOW_AUTHOR_RESOURCES"]
        huge = resolve(admin, version_id, resources={"cpus": 64})
        assert codes(huge)[0] == "LIMIT_EXCEEDED"  # and it could never fit the host either
        # The capacity check is separate from limits and from the configuration digest.
        assert plain["cache_observations"] and plain["config_digest"] != more["config_digest"]


def test_a_stale_or_expired_resolution_is_not_accepted(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        pat = create_pat(admin, run_limit=5)
        before = resolve(pat, version_id)
        # A new team default changes what the same request resolves to (CFG-06 seen
        # from the other side: defaults apply to new resolutions only).
        admin.put(
            "/api/v1/admin/team-policy",
            json={"values": {"run_default_concurrency": 2}, "expected_revision": 0},
            expect=200,
        )
        stale = pat.post(
            "/api/v1/runs",
            json={"name": "x", "resolution_id": before["resolution_id"]},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert error(stale) == (409, "CONFIG_RESOLUTION_STALE")
        assert stale.json()["error"]["field_errors"] == [
            {"path": "/concurrency", "code": "CHANGED"}
        ]
        fresh = resolve(pat, version_id)
        with service.db.write() as conn:
            conn.execute(
                update(t.config_resolutions)
                .where(t.config_resolutions.c.id == fresh["resolution_id"])
                .values(expires_at="2000-01-01T00:00:00.000000Z")
            )
        expired = pat.post(
            "/api/v1/runs",
            json={"name": "x", "resolution_id": fresh["resolution_id"]},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert error(expired) == (410, "CONFIG_RESOLUTION_EXPIRED")
        quota = admin.get("/api/v1/me/pats").json()["data"][0]
        assert quota["accepted_runs"] == 0


def test_model_and_harness_blockers(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        model_id, harness_id = prepare_model(admin)
        member = invite_member(service, admin, "member")
        oracle_with_model = member.post(
            "/api/v1/config-resolutions",
            json={
                "dataset_version_id": version_id,
                "execution_mode": "oracle",
                "model_config_id": model_id,
            },
        )
        assert oracle_with_model.json()["error"]["field_errors"][0]["code"] == "ORACLE_HAS_NO_MODEL"
        missing = member.post(
            "/api/v1/config-resolutions",
            json={"dataset_version_id": version_id, "execution_mode": "agent"},
        )
        assert {e["path"] for e in missing.json()["error"]["field_errors"]} == {
            "/model_config_id",
            "/harness_version_id",
        }
        anthropic = admin.post(
            "/api/v1/admin/harness-versions",
            json={
                "name": "only-anthropic",
                "version": "1",
                "agent": "fake-agent",
                "protocols": ["anthropic"],
                "parameter_schema": {"parameters": {"turns": {"type": "integer", "default": 3}}},
            },
            expect=201,
        ).json()["data"]
        mismatch = resolve(member, version_id, **agent_body(model_id, anthropic["id"]))
        assert codes(mismatch) == ["HARNESS_PROTOCOL_UNSUPPORTED"]
        assert mismatch["effective_config"]["harness"]["params"] == {"turns": 3}
        params = resolve(
            member,
            version_id,
            **agent_body(model_id, harness_id, harness_params={"nope": 1}),
        )
        assert codes(params) == ["UNKNOWN_PARAMETER"]

        model = admin.get(f"/api/v1/model-configs/{model_id}").json()["data"]
        admin.post(
            f"/api/v1/admin/credentials/{model['credential_id']}/disable",
            json={"expected_revision": 1},
            expect=200,
        )
        disabled_key = resolve(member, version_id, **agent_body(model_id, harness_id))
        assert codes(disabled_key) == ["CREDENTIAL_UNAVAILABLE"]
        assert disabled_key["blockers"][0]["reason"] == "DISABLED"
        admin.post(f"/api/v1/admin/model-configs/{model_id}/enabled", json={"enabled": False})
        off = resolve(member, version_id, **agent_body(model_id, harness_id))
        assert codes(off) == ["DISABLED", "CREDENTIAL_UNAVAILABLE"]
        refused = submit(member, version_id, **agent_body(model_id, harness_id))
        assert error(refused) == (422, "CONFIG_UNAVAILABLE")
        assert member.get("/api/v1/model-configs").json()["data"] == []


def test_snapshots_keep_their_model_revision_and_bind_the_credential(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        model_id, harness_id = prepare_model(admin)
        member = invite_member(service, admin, "member")
        accepted = submit(member, version_id, **agent_body(model_id, harness_id)).json()["data"]
        model = admin.get(f"/api/v1/model-configs/{model_id}").json()["data"]
        admin.put(
            f"/api/v1/admin/model-configs/{model_id}",
            json={
                **{k: model[k] for k in ("provider", "protocol", "endpoint", "params")},
                "model_id": "demo-model-2",
                "credential_id": model["credential_id"],
                "expected_revision": 1,
            },
            expect=200,
        )
        run = member.get(f"/api/v1/runs/{accepted['run_id']}").json()["data"]
        snapshot = run["snapshot"]
        assert snapshot["effective"]["model"]["model_id"] == "demo-model"
        assert snapshot["effective"]["model"]["revision"] == 1
        # The member sees no binding (U-27); an administrator's session does.
        assert snapshot["provenance"]["credential_ids"] == []
        bound = admin.get(f"/api/v1/runs/{accepted['run_id']}").json()["data"]["snapshot"]
        assert bound["provenance"]["credential_ids"] == [model["credential_id"]]
        assert snapshot["provenance"]["labels"] == {
            "model": "demo",
            "harness": "fake-harness 1.0.0",
        }
        # The bound credential and the model configuration cannot go while it is queued.
        busy = admin.delete(f"/api/v1/admin/credentials/{model['credential_id']}")
        assert error(busy) == (409, "CREDENTIAL_IN_USE")
        busy = admin.delete(f"/api/v1/admin/model-configs/{model_id}")
        assert error(busy) == (409, "MODEL_CONFIG_IN_USE")
        busy = admin.delete(f"/api/v1/admin/harness-versions/{harness_id}")
        assert error(busy) == (409, "HARNESS_IN_USE")
        listed = member.get(f"/api/v1/runs?model_config_id={model_id}").json()["data"]
        assert [x["id"] for x in listed] == [accepted["run_id"]]

        member.post(f"/api/v1/runs/{accepted['run_id']}/cancel", expect=200)
        entry = admin.delete(f"/api/v1/admin/model-configs/{model_id}", expect=200).json()["data"]
        assert entry["object_type"] == "model_config"
        assert member.get("/api/v1/model-configs").json()["data"] == []
        gone = resolve(member, version_id, **agent_body(model_id, harness_id))
        assert codes(gone) == ["DELETED"]
        admin.post(
            f"/api/v1/admin/recycle-bin/{entry['id']}/purge",
            json={"confirm_object_id": model_id},
            expect=200,
        )
        assert member.get(f"/api/v1/model-configs/{model_id}").status_code == 410
        # The run's own copy is untouched.
        run = member.get(f"/api/v1/runs/{accepted['run_id']}").json()["data"]
        assert run["snapshot"]["effective"]["model"]["endpoint"].startswith("https://")


def test_a_change_between_resolution_and_acceptance_charges_nothing(
    service, version_id, monkeypatch
):
    from maestro.services.configuration import Resolver

    original = Resolver.resolve

    def resolve_then_disable(self, conn, request):
        resolved = original(self, conn, request)
        with service.db.write() as other:
            other.execute(update(t.model_configs).values(enabled=False))
        return resolved

    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        model_id, harness_id = prepare_model(admin)
        pat = create_pat(admin, run_limit=2)
        monkeypatch.setattr(Resolver, "resolve", resolve_then_disable)
        refused = submit(pat, version_id, **agent_body(model_id, harness_id))
        assert error(refused) == (409, "CONFIG_CHANGED")
        assert refused.json()["error"]["field_errors"][0]["path"] == "/model"
        monkeypatch.setattr(Resolver, "resolve", original)
        with service.db.read() as conn:
            assert conn.execute(select(func.count()).select_from(t.runs)).scalar() == 0
        assert admin.get("/api/v1/me/pats").json()["data"][0]["accepted_runs"] == 0


def test_provider_concurrency_follows_the_model(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        model_id, harness_id = prepare_model(admin)
        admin.put(
            "/api/v1/admin/capacity/providers/demo",
            json={"max_concurrent_trials": 1},
            expect=200,
        )
        over = resolve(admin, version_id, **agent_body(model_id, harness_id, concurrency=2))
        assert codes(over) == ["PROVIDER_LIMIT"]
        assert (
            submit(admin, version_id, **agent_body(model_id, harness_id, concurrency=2)).json()[
                "error"
            ]["code"]
            == "RUN_EXCEEDS_CAPACITY"
        )
        # Oracle runs call no provider, so the limit does not apply to them.
        assert resolve(admin, version_id, concurrency=2)["blockers"] == []


# --- author parameters -------------------------------------------------------------------


def test_author_parameter_values_are_resolved_and_secrets_stay_references(service, source):
    files = {
        "needs-region": {
            **FakeDatasetSource.task_files("demo", "params", "needs-region"),
            "task.toml": (
                "[environment]\ncpus = 1\nmemory_mb = 2048\n"
                '[verifier.env]\nMODE = "${GRADER_MODE:-strict}"\nREGION = "${REGION}"\n'
                'TOKEN = "${GRADER_TOKEN}"\n'
            ),
        },
        "plain": FakeDatasetSource.task_files("demo", "params", "plain"),
    }
    source.add("demo", "params", "v1", list(files), files=files)
    with running(service) as client:
        admin = setup_admin(service, client)
        version_id, _ = import_and_prepare(admin, name="params")
        tasks = tasks_by_key(admin, version_id)
        assert tasks["needs-region"]["required_parameters"] == ["GRADER_TOKEN", "REGION"]

        without = resolve(admin, version_id)
        assert [x["task_key"] for x in without["effective_config"]["tasks"]] == ["plain"]
        assert without["excluded_tasks"][0]["blockers"] == [
            {"code": "CONFIG_REQUIRED_MISSING", "parameters": ["GRADER_TOKEN", "REGION"]}
        ]
        literal = resolve(admin, version_id, parameters={"REGION": "eu", "GRADER_TOKEN": "x"})
        assert codes(literal) == ["SENSITIVE_PARAMETER_LITERAL"]
        unknown = resolve(admin, version_id, parameters={"NOPE": "1"})
        assert codes(unknown) == ["UNKNOWN_PARAMETER"]
        secret = admin.post(
            "/api/v1/admin/credentials",
            json={
                "name": "grader",
                "kind": "generic_secret",
                "provider": "grader",
                "value": "grader-secret-xyz",
            },
            expect=201,
        ).json()["data"]
        given = resolve(
            admin,
            version_id,
            parameters={"REGION": "eu", "GRADER_TOKEN": {"credential_id": secret["id"]}},
        )
        assert given["blockers"] == [] and given["excluded_tasks"] == []
        assert given["effective_config"]["parameters"] == {
            "GRADER_MODE": {"value": "strict"},
            "GRADER_TOKEN": {"credential_id": secret["id"]},
            "REGION": {"value": "eu"},
        }
        assert given["field_sources"]["/parameters/GRADER_MODE"] == {"layer": "author"}
        assert given["field_sources"]["/parameters/REGION"] == {"layer": "override"}
        accepted = submit(
            admin,
            version_id,
            parameters={"REGION": "eu", "GRADER_TOKEN": {"credential_id": secret["id"]}},
        ).json()["data"]
        run = admin.get(f"/api/v1/runs/{accepted['run_id']}").json()["data"]
        assert run["snapshot"]["provenance"]["credential_ids"] == [secret["id"]]
        assert "grader-secret-xyz" not in str(run["snapshot"])

        # A plan keeps parameter values in request form, so a run from the plan resolves.
        values = {"REGION": "eu", "GRADER_TOKEN": {"credential_id": secret["id"]}}
        plan = admin.post(
            "/api/v1/plans",
            json={
                "name": "params",
                "template": {
                    "dataset_version_id": version_id,
                    "execution_mode": "oracle",
                    "parameters": values,
                },
            },
            expect=201,
        ).json()["data"]
        assert plan["template"]["parameters"] == values
        planned = admin.post("/api/v1/config-resolutions", json={"plan_id": plan["id"]})
        assert planned.status_code == 201, planned.text
        assert planned.json()["data"]["effective_config"]["parameters"]["REGION"] == {"value": "eu"}


def test_everyone_sees_usable_credential_names_but_never_values(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "member")
        reader = create_pat(member, scope="read")
        for name in ("grader", "spare"):
            admin.post(
                "/api/v1/admin/credentials",
                json={
                    "name": name,
                    "kind": "generic_secret",
                    "provider": "p",
                    "value": f"v-{name}",
                },
                expect=201,
            )
        spare = next(
            c for c in admin.get("/api/v1/admin/credentials").json()["data"] if c["name"] == "spare"
        )
        admin.post(
            f"/api/v1/admin/credentials/{spare['id']}/disable",
            json={"expected_revision": 1},
            expect=200,
        )
        for caller in (member, reader):
            listed = caller.get("/api/v1/credentials", expect=200).json()["data"]
            assert [c["name"] for c in listed] == ["grader"]
            assert set(listed[0]) == {"id", "name", "kind", "provider", "description"}
            assert "v-grader" not in str(listed)
        # The administrative list stays administrative.
        assert member.get("/api/v1/admin/credentials").status_code == 403


def test_the_credential_value_is_read_at_execution(service, version_id, executor):
    seen = []
    original = executor.execute

    async def capture(spec, sink, cancel):
        seen.append(dict(spec.secrets))
        return await original(spec, sink, cancel)

    executor.execute = capture
    with running(service) as client:
        admin = setup_admin(service, client)
        model_id, harness_id = prepare_model(admin)
        model = admin.get(f"/api/v1/model-configs/{model_id}").json()["data"]
        accepted = submit(admin, version_id, **agent_body(model_id, harness_id)).json()["data"]
        wait_operation(admin, accepted["operation_id"])
    assert seen and seen[0] == {"model_api_key": "sk-demo-not-a-real-key-0000"}
    run = admin.get(f"/api/v1/runs/{accepted['run_id']}").json()["data"]
    assert "sk-demo" not in str(run)
    assert model["credential_id"] in run["snapshot"]["provenance"]["credential_ids"]


def test_a_credential_disabled_after_acceptance_stops_the_run_before_it_starts(
    service, version_id, executor
):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        model_id, harness_id = prepare_model(admin)
        accepted = submit(admin, version_id, **agent_body(model_id, harness_id)).json()["data"]
        model = admin.get(f"/api/v1/model-configs/{model_id}").json()["data"]
        admin.post(
            f"/api/v1/admin/credentials/{model['credential_id']}/disable",
            json={"expected_revision": 1},
            expect=200,
        )
    with running(service) as client:
        signed_in = client.post("/api/v1/session", json={"username": "admin", "password": PASSWORD})
        api = Api(client, csrf=signed_in.json()["data"]["csrf_token"])
        operation = wait_operation(api, accepted["operation_id"])
        trials = api.get(f"/api/v1/runs/{accepted['run_id']}/trials").json()["data"]
    assert operation["status"] == "failed"
    assert {(x["state"], x["reason_code"]) for x in trials} == {
        ("execution_error", "CREDENTIAL_UNAVAILABLE")
    }
    assert executor.cleaned == [] or all(x["attempts"] == [] for x in trials)


def test_expired_resolutions_are_swept_a_day_later(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        old = resolve(admin, version_id)["resolution_id"]
        fresh = resolve(admin, version_id)["resolution_id"]
    with service.db.write() as conn:
        conn.execute(
            update(t.config_resolutions)
            .where(t.config_resolutions.c.id == old)
            .values(expires_at="2026-01-01T00:00:00.000000Z")
        )
    assert service.maintenance.upkeep()["resolutions_removed"] == 1
    with service.db.read() as conn:
        left = set(conn.execute(select(t.config_resolutions.c.id)).scalars())
    assert left == {fresh}
