"""Permission matrix: PAT never reaches admin; ownership cannot be forged."""

import pytest
from support import create_pat, invite_member, running, setup_admin, submit

ADMIN_CALLS = [
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
    ("GET", "/api/v1/admin/audit-events", None),
]


@pytest.mark.parametrize("method,path,body", ADMIN_CALLS)
def test_admin_endpoints_reject_pats_even_from_admins(service, method, path, body):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "alice")
        for caller in (create_pat(admin, "experiment"), create_pat(admin, "read")):
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
            ("POST", "/api/v1/me/pats", {"name": "n", "scope": "read"}),
        ):
            response = pat.request(method, path, json=body)
            assert response.json()["error"]["code"] == "WEB_SESSION_REQUIRED"


def test_read_pat_cannot_submit_but_reads_team_runs(service, version_id):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "alice")
        submit(member, version_id).raise_for_status()
        reader = create_pat(admin, "read")
        response = submit(reader, version_id)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "PAT_SCOPE_INSUFFICIENT"
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
        # Admin PAT has no override; admin web session does.
        assert create_pat(admin).post(f"/api/v1/runs/{run_a}/cancel").status_code == 403
        cancelled = admin.post(f"/api/v1/runs/{run_a}/cancel", expect=200).json()["data"]
        assert cancelled["state"] == "cancelled" and cancelled["stop_reason"] == "ADMIN_CANCELLED"
        # Owner's experiment PAT may cancel; read PAT may not.
        assert create_pat(alice, "read").post(f"/api/v1/runs/{run_b}/cancel").status_code == 403
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


def test_every_admin_route_carries_the_route_level_guard():
    """A new administrator route cannot rely on its service remembering the check."""
    from maestro.api.deps import admin_session

    routes = admin_routes()
    assert len(routes) > 40
    for method, route in routes:
        guards = [d.call for d in route.dependant.dependencies]
        assert admin_session in guards, f"{method} {route.path} lacks admin_session"


def test_every_admin_route_refuses_members_and_pats(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "alice")
        pats = (create_pat(admin, "experiment"), create_pat(admin, "read"))
        for method, route in admin_routes():
            # Placeholder ids and an empty body: the guard runs before either is read.
            path = route.path.replace("{", "").replace("}", "")
            response = member.request(method, path, json={})
            assert (response.status_code, response.json()["error"]["code"]) == (
                403,
                "FORBIDDEN",
            ), f"{method} {route.path}"
            for pat in pats:
                response = pat.request(method, path, json={})
                assert response.json()["error"]["code"] == "ADMIN_SESSION_REQUIRED", (
                    f"{method} {route.path}"
                )
