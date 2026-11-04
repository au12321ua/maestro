"""Review fixes: password reset and change versus PATs and sessions, and stale acting admins."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from support import PASSWORD, create_pat, invite_member, running, setup_admin

from maestro.core.errors import MaestroError
from maestro.db import schema as t
from maestro.domain.states import Role, UserStatus
from maestro.services.principal import Channel, Principal


def reset_password(admin, client, user_id, password="brand new password") -> TestClient:
    issued = admin.post(f"/api/v1/admin/members/{user_id}/password-reset", expect=201).json()
    fresh = TestClient(client.app)
    response = fresh.post(
        "/api/v1/invitations/accept", json={"token": issued["data"]["token"], "password": password}
    )
    assert response.status_code == 201, response.text
    return fresh


def test_an_admin_reset_revokes_the_users_pats_and_sessions(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        alice = invite_member(service, admin, "alice")
        pats = [create_pat(alice, ("experiment",)), create_pat(alice, ())]
        other = create_pat(admin, ("experiment",))
        user = alice.get("/api/v1/me").json()["data"]
        assert all(p.get("/api/v1/me").status_code == 200 for p in pats)

        fresh = reset_password(admin, client, user["id"])

        assert alice.get("/api/v1/me").status_code == 401
        for pat in pats:
            revoked = pat.get("/api/v1/me")
            assert revoked.status_code == 401 and revoked.json()["error"]["code"] == "PAT_REVOKED"
        assert other.get("/api/v1/me").status_code == 200  # nobody else's tokens are touched
        # The new session works and shows the revoked tokens as such.
        assert fresh.get("/api/v1/me").json()["data"]["username"] == "alice"
        listed = fresh.get("/api/v1/me/pats").json()["data"]
        assert len(listed) == 2 and all(p["revoked_at"] for p in listed)
        with service.db.read() as conn:
            revisions = conn.execute(
                select(t.pats.c.revision).where(t.pats.c.owner_id == user["id"])
            )
            assert all(r > 1 for r in revisions.scalars())
            detail = conn.execute(
                select(t.audit_events.c.detail_json).where(
                    t.audit_events.c.action == "user.reset_password"
                )
            ).scalar()
        assert detail == {"revoked_pats": 2}


def test_a_self_service_password_change_keeps_pats_and_ends_other_sessions(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        alice = invite_member(service, admin, "alice")
        pat = create_pat(alice, ("experiment",))
        elsewhere = TestClient(client.app)
        elsewhere.post("/api/v1/session", json={"username": "alice", "password": PASSWORD})
        assert elsewhere.get("/api/v1/me").status_code == 200

        alice.post(
            "/api/v1/me/password",
            json={"current_password": PASSWORD, "new_password": "a different password"},
            expect=204,
        )

        assert elsewhere.get("/api/v1/me").status_code == 401
        assert alice.get("/api/v1/me").status_code == 200
        assert pat.get("/api/v1/me").status_code == 200


def principal_of(api) -> Principal:
    me = api.get("/api/v1/me").json()["data"]
    return Principal(me["id"], me["username"], Role(me["role"]), Channel.WEB_SESSION, "ses_stale")


def revision_of(admin, username) -> tuple[str, int]:
    members = admin.get("/api/v1/admin/members").json()["data"]
    user = next(m for m in members if m["username"] == username)
    return user["id"], user["revision"]


def test_a_disabled_admins_in_flight_request_cannot_disable_the_other_admin(service):
    with running(service, run_worker=False) as client:
        first = setup_admin(service, client)
        second = invite_member(service, first, "boss2", role="admin")
        as_first, as_second = principal_of(first), principal_of(second)
        first_id, first_revision = revision_of(first, "admin")
        second_id, second_revision = revision_of(first, "boss2")

        service.accounts.set_status(as_first, second_id, UserStatus.DISABLED, second_revision)
        # The second admin's request was authenticated before: it must not go through.
        with pytest.raises(MaestroError) as error:
            service.accounts.set_status(as_second, first_id, UserStatus.DISABLED, first_revision)
        assert error.value.status == 401
        with pytest.raises(MaestroError):
            service.accounts.create_invitation(as_second, Role.ADMIN, "late")
        with service.db.read() as conn:
            states = dict(conn.execute(select(t.users.c.username, t.users.c.status)).all())
        assert states == {"admin": "active", "boss2": "disabled"}


def test_a_demoted_acting_admin_is_refused_too(service):
    with running(service, run_worker=False) as client:
        first = setup_admin(service, client)
        second = invite_member(service, first, "boss2", role="admin")
        as_second = principal_of(second)
        first_id, first_revision = revision_of(first, "admin")
        issued = first.post("/api/v1/admin/invitations", json={}, expect=201).json()["data"]
        with (
            service.db.write() as conn
        ):  # no endpoint demotes today; the check must not rely on one
            conn.execute(update(t.users).where(t.users.c.username == "boss2").values(role="member"))
        for call in (
            lambda: service.accounts.set_status(
                as_second, first_id, UserStatus.DISABLED, first_revision
            ),
            lambda: service.accounts.create_invitation(as_second, Role.MEMBER, ""),
            lambda: service.accounts.revoke_invitation(as_second, issued["id"]),
        ):
            with pytest.raises(MaestroError) as error:
                call()
            assert error.value.status == 403
        assert first.get("/api/v1/admin/members").status_code == 200
