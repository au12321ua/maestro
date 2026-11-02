"""Review fix: searching the external Hub is an administrator web-session action.

Listing what is already in the platform stays readable by every signed-in caller;
importing was always admin only.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from support import Api, create_pat, invite_member, running, setup_admin

from maestro.execution import hub as hub_module
from maestro.execution.hub import MAX_RUNNERS, HubSource

SEARCHES = [
    "/api/v1/dataset-search?q=demo&source_kind=fake",
    "/api/v1/dataset-search/versions?source_kind=fake&namespace=demo&name=arith",
    "/api/v1/dataset-search/version?source_kind=fake&namespace=demo&name=arith&ref=v1",
]
READABLE = ["/api/v1/datasets", "/api/v1/dataset-sources"]


@pytest.mark.parametrize("path", SEARCHES)
def test_only_an_admin_session_may_search_the_hub(service, path):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "alice")
        anonymous = Api(TestClient(client.app))
        assert anonymous.get(path).status_code == 401
        denied = member.get(path)
        assert denied.status_code == 403 and denied.json()["error"]["code"] == "FORBIDDEN"
        for pat in (
            create_pat(member, "read"),
            create_pat(member, "experiment"),
            create_pat(admin, "read"),
            create_pat(admin, "experiment"),
        ):
            refused = pat.get(path)
            assert refused.status_code == 403, refused.text
            assert refused.json()["error"]["code"] == "ADMIN_SESSION_REQUIRED"
        assert admin.get(path).status_code == 200


@pytest.mark.parametrize("path", READABLE)
def test_listing_what_the_platform_already_has_stays_open(service, path):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        member = invite_member(service, admin, "alice")
        for caller in (member, create_pat(member, "read"), create_pat(admin, "experiment")):
            assert caller.get(path).status_code == 200


def test_import_with_a_deleted_credential_is_refused_up_front(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        body = {"name": "hub", "kind": "hub_token", "provider": "hub", "value": "hf-secret-1"}
        cred = admin.post("/api/v1/admin/credentials", json=body, expect=201).json()["data"]
        entry = admin.delete(f"/api/v1/admin/credentials/{cred['id']}", expect=200).json()["data"]
        imported = admin.post(
            "/api/v1/admin/dataset-imports",
            json={
                "source_kind": "fake",
                "namespace": "demo",
                "name": "arith",
                "ref": "v1",
                "credential_id": cred["id"],
            },
        )
        assert imported.status_code == 409, imported.text
        assert imported.json()["error"]["code"] == "CREDENTIAL_DELETED"
        searched = admin.get(f"{SEARCHES[0]}&credential_id={cred['id']}")
        assert searched.status_code == 409
        assert searched.json()["error"]["code"] == "CREDENTIAL_DELETED"
        admin.post(f"/api/v1/admin/recycle-bin/{entry['id']}/restore", expect=200)
        assert admin.get(f"{SEARCHES[0]}&credential_id={cred['id']}").status_code == 200


class FakeProcess:
    pid = 0
    returncode = 0

    def __init__(self, probe):
        self.probe = probe

    async def communicate(self, _request):
        await asyncio.sleep(0.02)
        self.probe.running -= 1
        event = {
            "type": "result",
            "protocol_version": hub_module.PROTOCOL_VERSION,
            "payload": {"status": "ok", "data": []},
        }
        return json.dumps(event).encode() + b"\n", b""


class Probe:
    running = 0
    peak = 0


def test_one_shot_runners_are_bounded(tmp_path, monkeypatch):
    probe = Probe()

    async def spawn(*args, **kwargs):
        probe.running += 1
        probe.peak = max(probe.peak, probe.running)
        return FakeProcess(probe)

    monkeypatch.setattr(hub_module.asyncio, "create_subprocess_exec", spawn)
    source = HubSource(tmp_path / "python", tmp_path / "work")

    async def many():
        return await asyncio.gather(*(source.search(f"q{i}", None) for i in range(MAX_RUNNERS * 3)))

    assert asyncio.run(many()) == [[]] * (MAX_RUNNERS * 3)
    assert probe.peak == MAX_RUNNERS
    # A fresh loop (as each TestClient has) gets its own slots rather than a stale one.
    assert asyncio.run(many()) == [[]] * (MAX_RUNNERS * 3)
