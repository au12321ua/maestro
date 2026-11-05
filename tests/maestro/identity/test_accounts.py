from fastapi.testclient import TestClient
from support import PASSWORD, Api, create_pat, invite_member, running, setup_admin


def test_setup_is_one_time_and_needs_token(service):
    with running(service) as client:
        assert client.get("/api/v1/setup").json()["data"]["needs_setup"] is True
        token = service.accounts.issue_setup_token()
        bad = client.post(
            "/api/v1/setup",
            json={"setup_token": "mst_set_" + "x" * 43, "username": "root", "password": PASSWORD},
        )
        assert bad.status_code == 403 and bad.json()["error"]["code"] == "SETUP_TOKEN_INVALID"
        ok = client.post(
            "/api/v1/setup", json={"setup_token": token, "username": "root", "password": PASSWORD}
        )
        assert ok.status_code == 201
        assert ok.json()["data"]["user"]["role"] == "admin"
        assert client.get("/api/v1/setup").json()["data"]["needs_setup"] is False
        # The token file is consumed and no new token can be issued.
        assert not service.settings.setup_token_file.exists()
        assert service.accounts.issue_setup_token() is None
        again = client.post(
            "/api/v1/setup", json={"setup_token": token, "username": "root2", "password": PASSWORD}
        )
        assert again.status_code == 403


def test_session_cookie_csrf_and_logout(service):
    with running(service) as client:
        admin = setup_admin(service, client)
        assert admin.get("/api/v1/me", expect=200).json()["data"]["auth_channel"] == "web_session"
        # Unsafe request without CSRF header is refused.
        no_csrf = client.post("/api/v1/me/pats", json={"name": "x", "permissions": []})
        assert no_csrf.status_code == 403 and no_csrf.json()["error"]["code"] == "CSRF_FAILED"
        session = admin.get("/api/v1/session", expect=200).json()["data"]
        assert session["csrf_token"] == admin.csrf
        admin.delete("/api/v1/session", expect=204)
        assert client.get("/api/v1/me").status_code == 401


def test_login_failures_are_generic_and_audited(service):
    with running(service) as client:
        setup_admin(service, client)
        fresh = TestClient(client.app)
        for body in (
            {"username": "admin", "password": "wrong password"},
            {"username": "ghost", "password": PASSWORD},
        ):
            response = fresh.post("/api/v1/session", json=body)
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "LOGIN_FAILED"
        assert (
            fresh.post(
                "/api/v1/session", json={"username": "admin", "password": PASSWORD}
            ).status_code
            == 201
        )
    from sqlalchemy import select

    from maestro.db import schema as t

    with service.db.read() as conn:
        outcomes = (
            conn.execute(
                select(t.audit_events.c.outcome).where(t.audit_events.c.action == "session.login")
            )
            .scalars()
            .all()
        )
    assert sorted(outcomes) == ["failed", "failed", "succeeded"]


def test_login_rate_limit(service):
    with running(service) as client:
        setup_admin(service, client)
        codes = [
            client.post(
                "/api/v1/session", json={"username": "admin", "password": "nope nope nope"}
            ).status_code
            for _ in range(12)
        ]
        assert codes[-1] == 429


def test_invitation_single_use_and_password_reset(service):
    with running(service) as client:
        admin = setup_admin(service, client)
        issued = admin.post("/api/v1/admin/invitations", json={}, expect=201).json()["data"]
        guest = TestClient(client.app)
        info = guest.post("/api/v1/invitations/lookup", json={"token": issued["token"]}).json()
        assert info["data"]["kind"] == "invitation"
        accepted = guest.post(
            "/api/v1/invitations/accept",
            json={"token": issued["token"], "username": "alice", "password": PASSWORD},
        )
        assert accepted.status_code == 201
        reused = TestClient(client.app).post(
            "/api/v1/invitations/accept",
            json={"token": issued["token"], "username": "bob", "password": PASSWORD},
        )
        assert reused.status_code == 410
        alice_id = accepted.json()["data"]["user"]["id"]
        reset = admin.post(f"/api/v1/admin/members/{alice_id}/password-reset", expect=201).json()[
            "data"
        ]
        admin_id = admin.get("/api/v1/me").json()["data"]["id"]
        url = f"/api/v1/admin/members/{admin_id}/password-reset"
        # The role follows the target account, not a fixed "member".
        assert admin.post(url, expect=201).json()["data"]["role"] == "admin"
        assert reset["kind"] == "password_reset"
        # Reset ends existing sessions and sets the new password.
        TestClient(client.app).post(
            "/api/v1/invitations/accept",
            json={"token": reset["token"], "password": "brand new password"},
        ).raise_for_status()
        assert guest.get("/api/v1/me").status_code == 401
        assert (
            TestClient(client.app)
            .post("/api/v1/session", json={"username": "alice", "password": "brand new password"})
            .status_code
            == 201
        )


def test_disable_member_stops_sessions_and_pats(service):
    with running(service) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "alice")
        pat = create_pat(member, ())
        assert pat.get("/api/v1/me").status_code == 200
        user = member.get("/api/v1/me").json()["data"]
        admin.post(
            f"/api/v1/admin/members/{user['id']}/disable",
            json={"expected_revision": user["revision"]},
            expect=200,
        )
        assert member.get("/api/v1/me").status_code == 401
        assert pat.get("/api/v1/me").status_code == 401
        stale = admin.post(
            f"/api/v1/admin/members/{user['id']}/enable",
            json={"expected_revision": user["revision"]},
        )
        assert stale.status_code == 409
        me = admin.get("/api/v1/me").json()["data"]
        self_disable = admin.post(
            f"/api/v1/admin/members/{me['id']}/disable", json={"expected_revision": me["revision"]}
        )
        assert self_disable.json()["error"]["code"] == "CANNOT_DISABLE_SELF"


def test_pat_lifecycle_shown_once_and_revocation(service):
    with running(service) as client:
        admin = setup_admin(service, client)
        issued = admin.post(
            "/api/v1/me/pats",
            json={"name": "agent", "permissions": ["experiment"], "run_limit": 5},
            expect=201,
        ).json()["data"]
        assert issued["token"].startswith(issued["token_prefix"])
        listed = admin.get("/api/v1/me/pats").json()["data"]
        assert "token" not in listed[0] and listed[0]["remaining_runs"] == 5
        pat = Api(TestClient(client.app), token=issued["token"])
        assert pat.get("/api/v1/me").json()["data"]["auth_channel"] == "pat"
        admin.post(f"/api/v1/me/pats/{issued['id']}/revoke", expect=200)
        denied = pat.get("/api/v1/me")
        assert denied.status_code == 401 and denied.json()["error"]["code"] == "PAT_REVOKED"
        assert admin.get("/api/v1/me/pats").json()["data"][0]["state"] == "revoked"
        # Deleting a token removes it from the list (and would revoke a live one).
        entry = admin.delete(f"/api/v1/me/pats/{issued['id']}", expect=200).json()["data"]
        assert entry["object_type"] == "pat" and entry["state"] == "recycled"
        assert admin.get("/api/v1/me/pats").json()["data"] == []
        invalid = admin.post(
            "/api/v1/me/pats", json={"name": "x", "permissions": [], "expires_in_days": 9999}
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"]["field_errors"][0]["path"] == "/expires_in_days"


def test_errors_use_envelope_with_request_id(service):
    with running(service) as client:
        response = client.get("/api/v1/runs")
        body = response.json()
        assert response.status_code == 401
        assert body["error"]["code"] == "UNAUTHENTICATED"
        assert body["meta"]["request_id"] == response.headers["X-Request-Id"]
        assert client.get("/api/v1/nowhere").json()["error"]["code"] == "NOT_FOUND"
