"""Callers and waits shared by API tests."""

import contextlib
import time
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from maestro.api.app import create_app

# The repository, the test tree and its fixtures, wherever a test module sits below it.
ROOT = Path(__file__).resolve().parents[3]
TESTS = ROOT / "tests" / "maestro"
FIXTURES = TESTS / "fixtures"

PASSWORD = "correct horse battery"


class Api:
    """A browser-like or SDK-like caller of one TestClient."""

    def __init__(self, client: TestClient, csrf: str | None = None, token: str | None = None):
        self.client = client
        self.csrf = csrf
        self.token = token

    def _headers(self, extra=None):
        headers = dict(extra or {})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        elif self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        return headers

    def request(self, method, path, json=None, headers=None, expect=None):
        response = self.client.request(method, path, json=json, headers=self._headers(headers))
        if expect is not None:
            assert response.status_code == expect, response.text
        return response

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, json=None, **kw):
        return self.request("POST", path, json=json, **kw)

    def put(self, path, json=None, **kw):
        return self.request("PUT", path, json=json, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)


def make_client(service, run_worker=True):
    return TestClient(create_app(service, run_worker=run_worker))


def browser(service, run_worker=True) -> TestClient:
    return make_client(service, run_worker)


@contextlib.contextmanager
def running(service, run_worker=True):
    with make_client(service, run_worker) as client:
        yield client


def setup_admin(service, client: TestClient, username="admin") -> Api:
    token = service.accounts.issue_setup_token()
    response = client.post(
        "/api/v1/setup",
        json={"setup_token": token, "username": username, "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    return Api(client, csrf=response.json()["data"]["csrf_token"])


def invite_member(service, admin: Api, username: str, role="member") -> Api:
    """Accept an invitation in a separate cookie jar so sessions stay independent."""
    issued = admin.post("/api/v1/admin/invitations", json={"role": role}, expect=201).json()
    other = TestClient(admin.client.app)  # lifespan already runs on the admin client
    response = other.post(
        "/api/v1/invitations/accept",
        json={"token": issued["data"]["token"], "username": username, "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    return Api(other, csrf=response.json()["data"]["csrf_token"])


def create_pat(api: Api, permissions=("experiment",), run_limit=None) -> Api:
    body = {"name": f"pat-{uuid.uuid4().hex[:6]}", "permissions": list(permissions)}
    if run_limit is not None:
        body["run_limit"] = run_limit
    issued = api.post("/api/v1/me/pats", json=body, expect=201).json()["data"]
    return Api(TestClient(api.client.app), token=issued["token"])


def wait_operation(api: Api, operation_id: str, timeout=10.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        data = api.get(f"/api/v1/operations/{operation_id}", expect=200).json()["data"]
        if data["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            return data
        if time.monotonic() > deadline:
            raise AssertionError(f"operation still {data['status']}: {data}")
        time.sleep(0.02)


def submit(api: Api, version_id, key=None, **body) -> dict:
    payload = {
        "name": "baseline",
        "dataset_version_id": version_id,
        "execution_mode": "oracle",
        **body,
    }
    response = api.post(
        "/api/v1/runs", json=payload, headers={"Idempotency-Key": key or uuid.uuid4().hex}
    )
    return response
