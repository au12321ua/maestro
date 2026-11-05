"""Enabling or disabling a credential keeps its value decryptable.

The AES-GCM associated data binds the credential id and revision, so every
revision bump has to re-encrypt in the same write transaction.
"""

import pytest
from support import running, setup_admin

from maestro.core.errors import MaestroError

BODY = {"name": "openai", "kind": "model_api_key", "provider": "openai", "value": "sk-secret-1"}


def make(admin, **extra):
    return admin.post("/api/v1/admin/credentials", json={**BODY, **extra}, expect=201).json()[
        "data"
    ]


def toggle(admin, cred, enabled):
    verb = "enable" if enabled else "disable"
    return admin.post(
        f"/api/v1/admin/credentials/{cred['id']}/{verb}",
        json={"expected_revision": cred["revision"]},
        expect=200,
    ).json()["data"]


def rotate(admin):
    generation = admin.get("/api/v1/admin/master-key").json()["data"]["generation"]
    return admin.post(
        "/api/v1/admin/master-key/rotations", json={"expected_generation": generation}
    )


def test_disable_then_enable_keeps_the_value_resolvable(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        cred = make(admin)
        off = toggle(admin, cred, False)
        on = toggle(admin, off, True)
        assert on["revision"] == 3 and on["enabled"]
        assert service.credentials.resolve_value(cred["id"]) == ("sk-secret-1", 3)


def test_a_disabled_credential_survives_master_key_rotation(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        cred = make(admin)
        other = make(admin, name="second", value="sk-secret-2")
        off = toggle(admin, cred, False)
        rotated = rotate(admin)
        assert rotated.status_code == 200, rotated.text
        assert rotated.json()["data"]["reencrypted_credentials"] == 2
        with pytest.raises(MaestroError) as disabled:
            service.credentials.resolve_value(cred["id"])
        assert disabled.value.code == "CREDENTIAL_DISABLED"
        back = toggle(admin, off, True)
        assert service.credentials.resolve_value(cred["id"]) == ("sk-secret-1", back["revision"])
        assert service.credentials.resolve_value(other["id"])[0] == "sk-secret-2"
        assert rotate(admin).status_code == 200
        assert service.credentials.resolve_value(cred["id"])[0] == "sk-secret-1"


def test_enable_toggle_without_a_change_is_a_no_op(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        cred = make(admin)
        same = toggle(admin, cred, True)
        assert same["revision"] == 1
        assert service.credentials.resolve_value(cred["id"]) == ("sk-secret-1", 1)


def test_delete_restore_keeps_the_value_resolvable(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        cred = make(admin)
        entry = admin.delete(f"/api/v1/admin/credentials/{cred['id']}", expect=200).json()["data"]
        with pytest.raises(MaestroError) as gone:
            service.credentials.resolve_value(cred["id"])
        assert gone.value.code == "CREDENTIAL_DELETED"
        admin.post(f"/api/v1/admin/recycle-bin/{entry['id']}/restore", expect=200)
        assert service.credentials.resolve_value(cred["id"]) == ("sk-secret-1", 1)
        assert rotate(admin).status_code == 200
        assert service.credentials.resolve_value(cred["id"])[0] == "sk-secret-1"


def test_replace_value_after_toggling_still_works(service):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        cred = make(admin)
        back = toggle(admin, toggle(admin, cred, False), True)
        replaced = admin.put(
            f"/api/v1/admin/credentials/{cred['id']}/value",
            json={"value": "sk-secret-new", "expected_revision": back["revision"]},
            expect=200,
        ).json()["data"]
        assert replaced["revision"] == back["revision"] + 1
        assert service.credentials.resolve_value(cred["id"]) == (
            "sk-secret-new",
            replaced["revision"],
        )
        assert rotate(admin).status_code == 200
