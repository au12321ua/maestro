"""Permission matrix: a PAT reaches administration only with the permission a route
declares and never reaches session-only routes; ownership cannot be forged."""

import pytest
from support import create_pat, invite_member, running, setup_admin, submit

ALL_ADMIN = ("experiment", "datasets", "models", "operations", "override", "audit")

# Session-only: no PAT reaches these, whatever it was granted.
SESSION_CALLS = [
    ("GET", "/api/v1/admin/members", None),
    ("POST", "/api/v1/admin/invitations", {}),
    ("GET", "/api/v1/admin/credentials", None),
    (
        "POST",
        "/api/v1/admin/credentials",
        {"name": "k", "kind": "hub_token", "provider": "hub", "value": "v"},
    ),
    ("GET", "/api/v1/admin/master-key", None),
    ("POST", "/api/v1/admin/master-key/rotations", {"expected_generation": 1}),
]


@pytest.mark.parametrize("method,path,body", SESSION_CALLS)
def test_session_only_endpoints_reject_every_pat(service, method, path, body):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "alice")
        for caller in (create_pat(admin, ALL_ADMIN), create_pat(admin, ())):
            response = caller.request(method, path, json=body)
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "ADMIN_SESSION_REQUIRED"
        response = member.request(method, path, json=body)
        assert response.status_code == 403 and response.json()["error"]["code"] == "FORBIDDEN"
        assert admin.request(method, path, json=body).status_code in {200, 201}


def test_pat_cannot_manage_pats(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        pat = create_pat(admin)
        for method, path, body in (
            ("GET", "/api/v1/me/pats", None),
            ("POST", "/api/v1/me/pats", {"name": "n", "permissions": []}),
        ):
            response = pat.request(method, path, json=body)
            assert response.json()["error"]["code"] == "WEB_SESSION_REQUIRED"


def test_read_pat_cannot_submit_but_reads_team_runs(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "alice")
        submit(member, version_id).raise_for_status()
        reader = create_pat(admin, ())
        response = submit(reader, version_id)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "PAT_PERMISSION_INSUFFICIENT"
        assert len(reader.get("/api/v1/runs").json()["data"]) == 1


def test_cancel_ownership_and_admin_override(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        alice = invite_member(service, admin, "alice")
        bob = invite_member(service, admin, "bob")
        run_a = submit(alice, version_id).json()["data"]["run_id"]
        run_b = submit(alice, version_id).json()["data"]["run_id"]
        run_c = submit(alice, version_id).json()["data"]["run_id"]
        denied = bob.post(f"/api/v1/runs/{run_a}/cancel")
        assert denied.status_code == 403
        view = bob.get(f"/api/v1/runs/{run_a}").json()["data"]
        assert view["allowed_actions"] == ["view", "rerun", "modify"]  # not "cancel"
        assert view["owner"]["username"] == "alice"
        assert "cancel" in alice.get(f"/api/v1/runs/{run_a}").json()["data"]["allowed_actions"]
        # Overriding another member needs the override permission; a web session has it.
        assert create_pat(admin).post(f"/api/v1/runs/{run_a}/cancel").status_code == 403
        cancelled = admin.post(f"/api/v1/runs/{run_a}/cancel", expect=200).json()["data"]
        assert cancelled["state"] == "cancelled" and cancelled["stop_reason"] == "ADMIN_CANCELLED"
        overriding = create_pat(admin, ("override",))
        cancelled = overriding.post(f"/api/v1/runs/{run_b}/cancel", expect=200).json()["data"]
        assert cancelled["stop_reason"] == "ADMIN_CANCELLED"
        run_b = submit(alice, version_id).json()["data"]["run_id"]
        # Owner's experiment PAT may cancel; read PAT may not.
        assert create_pat(alice, ()).post(f"/api/v1/runs/{run_b}/cancel").status_code == 403
        own = create_pat(alice).post(f"/api/v1/runs/{run_b}/cancel", expect=200).json()["data"]
        assert own["stop_reason"] == "USER_CANCELLED"
        # Cancelling twice is idempotent.
        alice.post(f"/api/v1/runs/{run_c}/cancel", expect=200)
        again = alice.post(f"/api/v1/runs/{run_c}/cancel", expect=200).json()["data"]
        assert again["state"] == "cancelled"
        trials = alice.get(f"/api/v1/runs/{run_c}/trials").json()["data"]
        assert {x["state"] for x in trials} == {"not_started"}
        assert {x["reason_code"] for x in trials} == {"USER_CANCELLED"}


def test_owner_cannot_be_forged_through_request_fields(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        alice = invite_member(service, admin, "alice")
        admin_id = admin.get("/api/v1/me").json()["data"]["id"]
        response = submit(alice, version_id, owner_id=admin_id)
        assert response.status_code == 422  # unknown fields are rejected, not trusted


def admin_routes():
    """Every administrator route: the /admin tree plus searching the external source.

    Walks the routers the app includes (``routes.routers``), whose routes carry their
    router-level dependencies.
    """
    from maestro.api import routes

    found = []
    for router in routes.routers():
        for route in router.routes:
            if route.path.startswith(("/api/v1/admin/", "/api/v1/dataset-search")):
                found += [(method, route) for method in sorted(route.methods - {"HEAD"})]
    return found


def route_guard(route):
    """The route's declared guard: 'session' or the permission a PAT needs."""
    from maestro.api.deps import admin_session

    for dependency in route.dependant.dependencies:
        if dependency.call is admin_session:
            return "session"
        permission = getattr(dependency.call, "permission", None)
        if permission is not None:
            return permission.value
    return None


# Prefixes that must stay session-only: a leaked token must not mint tokens, take over
# accounts, move keys or destroy data for good.
SESSION_ONLY = (
    "/api/v1/admin/members",
    "/api/v1/admin/invitations",
    "/api/v1/admin/credentials",
    "/api/v1/admin/master-key",
    "/api/v1/admin/pats",
    "/api/v1/admin/recycle-bin/{entry_id}/",
)


def test_every_admin_route_declares_its_guard():
    """A new administrator route cannot rely on its service remembering the check."""
    routes = admin_routes()
    assert len(routes) > 40
    for method, route in routes:
        guard = route_guard(route)
        assert guard is not None, f"{method} {route.path} declares no guard"
        if route.path.startswith(SESSION_ONLY):
            assert guard == "session", f"{method} {route.path} must stay session-only"


def test_every_admin_route_refuses_members_and_tokens_without_its_permission(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "alice")
        member_pat = create_pat(member, ("experiment",))
        everything = create_pat(admin, ALL_ADMIN)
        lacking = {p: create_pat(admin, tuple(x for x in ALL_ADMIN if x != p)) for p in ALL_ADMIN}
        for method, route in admin_routes():
            # Placeholder ids and an empty body: the guard runs before either is read.
            path = route.path.replace("{", "").replace("}", "")
            where = f"{method} {route.path}"
            response = member.request(method, path, json={})
            assert (response.status_code, response.json()["error"]["code"]) == (
                403,
                "FORBIDDEN",
            ), where
            response = member_pat.request(method, path, json={})
            assert response.status_code == 403, where
            guard = route_guard(route)
            if guard == "session":
                response = everything.request(method, path, json={})
                assert response.json()["error"]["code"] == "ADMIN_SESSION_REQUIRED", where
            else:
                response = lacking[guard].request(method, path, json={})
                assert response.json()["error"]["code"] == "PAT_PERMISSION_INSUFFICIENT", where
                response = everything.request(method, path, json={})
                assert response.status_code != 403, where


def test_tokens_are_capped_by_role_and_admin_tokens_live_shorter(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "alice")
        refused = member.post("/api/v1/me/pats", json={"name": "x", "permissions": ["datasets"]})
        assert refused.status_code == 422
        assert refused.json()["error"]["field_errors"][0]["code"] == "ROLE_EXCEEDED"
        long = admin.post(
            "/api/v1/me/pats",
            json={"name": "x", "permissions": ["audit"], "expires_in_days": 31},
        )
        assert long.status_code == 422
        assert long.json()["error"]["field_errors"][0]["limit"] == 30
        issued = admin.post(
            "/api/v1/me/pats", json={"name": "x", "permissions": ["audit"]}, expect=201
        ).json()["data"]
        assert issued["permissions"] == ["audit"]
        # An ordinary token keeps the longer limit.
        admin.post(
            "/api/v1/me/pats",
            json={"name": "y", "permissions": ["experiment"], "expires_in_days": 31},
            expect=201,
        )
        me = create_pat(admin, ("audit", "experiment")).get("/api/v1/me").json()["data"]
        assert me["pat_permissions"] == ["audit", "experiment"]


def test_a_token_can_edit_models_but_never_bind_a_key(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        models = create_pat(admin, ("models",))
        body = {
            "name": "m1",
            "provider": "demo",
            "protocol": "openai-compatible",
            "endpoint": "https://models.example.test/v1",
            "model_id": "demo-model",
        }
        created = models.post("/api/v1/admin/model-configs", json=body)
        assert created.status_code == 201, created.text
        bound = models.post(
            "/api/v1/admin/model-configs", json={**body, "name": "m2", "credential_id": "cred_x"}
        )
        assert bound.json()["error"]["code"] == "ADMIN_SESSION_REQUIRED"


def test_record_routes_and_current_best_declare_authentication_and_reject_read_pat_writes(
    service, version_id
):
    from maestro.api import routes
    from maestro.api.deps import experiment, principal

    found = [
        route
        for router in routes.routers()
        for route in router.routes
        if route.path.startswith("/api/v1/records") or route.path.endswith("/current-best")
    ]
    assert sum(len(route.methods) for route in found) == 7
    for route in found:
        guard = experiment if route.methods & {"POST", "PUT"} else principal
        assert any(d.call is guard for d in route.dependant.dependencies), route.path
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        reader = create_pat(admin, ())
        run_id = submit(admin, version_id).json()["data"]["run_id"]
        payload = {
            "target": {"type": "run", "id": run_id},
            "kind": "note",
            "title": "Own note",
            "body": {},
        }
        record = admin.post("/api/v1/records", json=payload, expect=201).json()["data"]
        path = f"/api/v1/records/{record['id']}"
        for method, url, body in (
            ("POST", "/api/v1/records", payload),
            ("PUT", path, {"expected_revision": 1, "title": "x"}),
            ("DELETE", path, None),
            (
                "PUT",
                "/api/v1/collections/missing/current-best",
                {"snapshot_id": None, "reason": "x"},
            ),
        ):
            response = reader.request(method, url, json=body)
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "PAT_PERMISSION_INSUFFICIENT"
        for url in (path, path + "/history", f"/api/v1/records?target_type=run&target_id={run_id}"):
            reader.get(url, expect=200)
        client.cookies.clear()
        for route in found:
            url = route.path.replace("{record_id}", record["id"]).replace(
                "{collection_id}", "missing"
            )
            response = client.request(next(iter(route.methods)), url, json={})
            assert response.status_code == 401


def test_task_content_reads_and_held_out_views_are_readable_without_write_permissions(
    service, version_id
):
    from support.configuration import agent_body, prepare_model

    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "reader")
        model, harness = prepare_model(admin)
        task = member.get(f"/api/v1/dataset-versions/{version_id}/tasks").json()["data"][0]
        collection = admin.post(
            "/api/v1/collections",
            json={
                "name": "held out",
                "scenario": "models",
                "base": {
                    "request": {"dataset_version_id": version_id, **agent_body(model, harness)}
                },
                "held_out_task_ids": [task["id"]],
            },
            expect=201,
        ).json()["data"]
        for caller in (member, create_pat(member, ()), create_pat(admin, ())):
            caller.get(f"/api/v1/tasks/{task['id']}", expect=200)
            reads = caller.post(
                "/api/v1/tasks/reads", json={"task_ids": [task["id"]]}, expect=200
            ).json()["data"]
            assert reads
            caller.get(f"/api/v1/collections/{collection['id']}/held-out", expect=200)
        reader = create_pat(member, ())
        assert (
            reader.post(
                f"/api/v1/collections/{collection['id']}/copy", json={"held_out_task_ids": []}
            ).status_code
            == 403
        )
        anonymous = type(client)(client.app)
        for method, path, body in (
            ("GET", f"/api/v1/tasks/{task['id']}", None),
            ("POST", "/api/v1/tasks/reads", {"task_ids": [task["id"]]}),
            ("GET", f"/api/v1/collections/{collection['id']}/held-out", None),
        ):
            assert anonymous.request(method, path, json=body).status_code == 401
