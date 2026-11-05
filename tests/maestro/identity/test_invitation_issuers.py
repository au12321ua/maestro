"""An invitation is only as good as the administrator who issued it."""

from fastapi.testclient import TestClient
from support import PASSWORD, invite_member, running, setup_admin


def redeem(client: TestClient, token: str, username: str = "newcomer"):
    return TestClient(client.app).post(
        "/api/v1/invitations/accept",
        json={"token": token, "username": username, "password": PASSWORD},
    )


def test_an_invitation_stops_working_when_its_issuer_is_disabled(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        second = invite_member(service, admin, "second", role="admin")
        me = second.get("/api/v1/me").json()["data"]
        issued = second.post(
            "/api/v1/admin/invitations", json={"role": "admin"}, expect=201
        ).json()["data"]["token"]
        admin.post(
            f"/api/v1/admin/members/{me['id']}/disable",
            json={"expected_revision": me["revision"]},
            expect=200,
        )

        refused = redeem(client, issued)

        assert refused.status_code == 410, refused.text
        assert refused.json()["error"]["code"] == "INVITATION_INVALID"
        lookup = TestClient(client.app).post("/api/v1/invitations/lookup", json={"token": issued})
        assert lookup.status_code == 410


def test_an_invitation_from_an_active_admin_still_works(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        issued = admin.post(
            "/api/v1/admin/invitations", json={"role": "member"}, expect=201
        ).json()["data"]["token"]

        accepted = redeem(client, issued)

        assert accepted.status_code == 201, accepted.text
        assert accepted.json()["data"]["user"]["role"] == "member"


def test_a_reset_link_from_a_disabled_issuer_is_refused_too(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        second = invite_member(service, admin, "second", role="admin")
        victim = invite_member(service, admin, "victim")
        second_row = second.get("/api/v1/me").json()["data"]
        victim_id = victim.get("/api/v1/me").json()["data"]["id"]
        link = second.post(f"/api/v1/admin/members/{victim_id}/password-reset", expect=201).json()
        admin.post(
            f"/api/v1/admin/members/{second_row['id']}/disable",
            json={"expected_revision": second_row["revision"]},
            expect=200,
        )

        refused = TestClient(client.app).post(
            "/api/v1/invitations/accept",
            json={"token": link["data"]["token"], "password": "another password"},
        )

        assert refused.status_code == 410, refused.text
