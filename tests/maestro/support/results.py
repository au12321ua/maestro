"""Shared results helpers for tests."""

import asyncio
import uuid

from sqlalchemy import insert, update

from maestro.core import clock
from maestro.db import schema as t
from maestro.domain.states import Role
from maestro.services.principal import Channel, Principal
from support import (
    submit,
    wait_operation,
)


def finished_run(api, version_id, **body) -> str:
    response = submit(api, version_id, **body)
    assert response.status_code == 202, response.text
    accepted = response.json()["data"]
    wait_operation(api, accepted["operation_id"])
    return accepted["run_id"]


def bin_entry(admin, object_id, state="recycled") -> dict:
    entries = admin.get(f"/api/v1/admin/recycle-bin?state={state}").json()["data"]
    return next(x for x in entries if x["object_id"] == object_id)


def purge(admin, entry, confirm=None):
    return admin.post(
        f"/api/v1/admin/recycle-bin/{entry['id']}/purge",
        json={"confirm_object_id": confirm or entry["object_id"]},
    )


def make_owner(service) -> Principal:
    with service.db.write() as conn:
        conn.execute(
            insert(t.users).values(
                id="usr_owner",
                username="owner",
                display_name="o",
                role="admin",
                status="active",
                password_hash="x",
                revision=1,
                created_at="t",
                updated_at="t",
            )
        )
    return Principal("usr_owner", "owner", Role.ADMIN, Channel.WEB_SESSION, session_id="s")


def run(service, owner, version_id, **body) -> str:
    _, data, _ = service.runs.submit(
        owner,
        uuid.uuid4().hex,
        {"name": "r", "dataset_version_id": version_id, "execution_mode": "oracle", **body},
    )
    asyncio.run(service.worker.drain())
    return data["run_id"]


def backdate(service, artifact_id: str) -> None:
    with service.db.write() as conn:
        conn.execute(
            update(t.artifacts)
            .where(t.artifacts.c.id == artifact_id)
            .values(expires_at=clock.to_text(clock.now() - clock.timedelta(seconds=1)))
        )
