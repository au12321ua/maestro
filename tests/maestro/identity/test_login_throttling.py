"""Login throttling counts failures, per account and source, in bounded memory."""

import types

import pytest
from fastapi.testclient import TestClient
from support import PASSWORD, Api, invite_member, running, setup_admin

from maestro.core.errors import MaestroError
from maestro.services import accounts
from maestro.services.accounts import RateLimiter

LIMIT = 10


def login(service, username, password, source="10.0.0.1"):
    return service.accounts.login(username, password, source)


def refused(service, username, password, source="10.0.0.1") -> str:
    with pytest.raises(MaestroError) as error:
        login(service, username, password, source)
    return error.value.code


def test_a_stranger_cannot_lock_the_admin_out_from_another_address(service):
    with running(service, run_worker=False) as client:
        setup_admin(service, client)
        for _ in range(LIMIT):
            assert refused(service, "admin", "attacker guess", "203.0.113.9") == "LOGIN_FAILED"
        assert refused(service, "admin", "attacker guess", "203.0.113.9") == "RATE_LIMITED"
        # The attacker's own address stays throttled, the admin's address is unaffected.
        assert refused(service, "admin", PASSWORD, "203.0.113.9") == "RATE_LIMITED"
        user, _token = login(service, "admin", PASSWORD, "198.51.100.7")
        assert user["username"] == "admin"


def test_successful_logins_are_not_counted(service):
    with running(service, run_worker=False) as client:
        setup_admin(service, client)
        for _ in range(LIMIT * 3):
            login(service, "admin", PASSWORD)
        # A few typos after many good logins still get their full allowance.
        for _ in range(LIMIT):
            assert refused(service, "admin", "typo typo typo") == "LOGIN_FAILED"
        assert refused(service, "admin", "typo typo typo") == "RATE_LIMITED"


def test_one_address_spraying_many_usernames_is_stopped(service):
    with running(service, run_worker=False) as client:
        setup_admin(service, client)
        spray = LIMIT * accounts.SOURCE_FAILURE_FACTOR
        for i in range(spray):
            assert refused(service, f"ghost{i}", "any password", "192.0.2.5") == "LOGIN_FAILED"
        assert refused(service, "ghost-new", "any password", "192.0.2.5") == "RATE_LIMITED"
        assert login(service, "admin", PASSWORD, "192.0.2.6")


def test_unknown_disabled_and_wrong_password_look_the_same(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        invite_member(service, admin, "dora")
        dora = next(
            m for m in admin.get("/api/v1/admin/members").json()["data"] if m["username"] == "dora"
        )
        admin.post(
            f"/api/v1/admin/members/{dora['id']}/disable",
            json={"expected_revision": dora["revision"]},
            expect=200,
        )
        fresh = TestClient(client.app)
        seen = set()
        for body in (
            {"username": "ghost", "password": PASSWORD},
            {"username": "admin", "password": "wrong password"},
            {"username": "dora", "password": PASSWORD},
        ):
            response = fresh.post("/api/v1/session", json=body)
            seen.add((response.status_code, response.json()["error"]["message"]))
        assert seen == {(401, "Incorrect username or password")}


def test_the_advertised_limit_is_the_account_and_source_limit(service):
    with running(service, run_worker=False) as client:
        setup_admin(service, client)
        advertised = client.get("/api/v1/capabilities").json()["data"]["limits"]
        assert advertised["login_attempts_per_minute"] == service.accounts.limiter.per_minute
        assert advertised["login_attempts_per_minute"] == LIMIT


def test_oversized_login_fields_are_rejected_before_any_state(service):
    with running(service, run_worker=False) as client:
        setup_admin(service, client)
        anon = TestClient(client.app)
        for body in (
            {"username": "u" * (5 * 1024 * 1024), "password": "x"},
            {"username": "u" * 129, "password": "x"},
            {"username": "admin", "password": "p" * 257},
        ):
            response = anon.post("/api/v1/session", json=body)
            assert response.status_code == 422, response.status_code
        assert not [key for key in service.accounts.limiter.hits if key.startswith("login")]


def test_changing_the_password_has_its_own_failure_limit(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        body = {"current_password": "not the password", "new_password": "another long password"}
        for _ in range(LIMIT):
            response = admin.post("/api/v1/me/password", json=body)
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "PASSWORD_INCORRECT"
        blocked = admin.post("/api/v1/me/password", json=body)
        assert blocked.status_code == 429 and blocked.json()["error"]["code"] == "RATE_LIMITED"
        right = {"current_password": PASSWORD, "new_password": "another long password"}
        assert admin.post("/api/v1/me/password", json=right).status_code == 429


def test_successful_password_changes_are_not_counted(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        current = PASSWORD
        for i in range(LIMIT + 2):
            new = f"password number {i:03d}"
            admin.post(
                "/api/v1/me/password",
                json={"current_password": current, "new_password": new},
                expect=204,
            )
            current = new


def test_invitation_and_setup_attempts_still_count_every_try(service):
    with running(service, run_worker=False) as client:
        anon = Api(TestClient(client.app))
        bad = {"token": "mst_inv_" + "x" * 43}
        for _ in range(LIMIT):
            assert anon.post("/api/v1/invitations/lookup", json=bad).status_code == 410
        limited = anon.post("/api/v1/invitations/lookup", json=bad)
        assert limited.status_code == 429
        assert limited.headers["Retry-After"] == "60"


class Clock:
    now = 1000.0

    def monotonic(self):
        return self.now


def test_limiter_forgets_expired_keys_and_caps_the_key_count(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(accounts, "time", types.SimpleNamespace(monotonic=clock.monotonic))
    limiter = RateLimiter(3, max_keys=5)
    for i in range(50):
        limiter.record(f"k{i}")
        assert len(limiter.hits) <= 5
    assert list(limiter.hits) == [f"k{i}" for i in range(45, 50)]
    for _ in range(3):
        limiter.record("busy")
    with pytest.raises(MaestroError) as error:
        limiter.check("busy")
    assert error.value.code == "RATE_LIMITED"
    clock.now += 61
    limiter.check("busy")  # the window passed
    limiter.record("fresh")
    assert list(limiter.hits) == ["fresh"]
    limiter.check("never-seen")
    assert "never-seen" not in limiter.hits
