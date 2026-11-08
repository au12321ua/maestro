"""Updates checked for and started in the web pages: the release check, the pending
update that pauses dispatching, the request handed to the host and the result read back.
The host side (``maestroctl update --request`` under systemd) is in test_maestroctl.py and
the live install test."""

import json
import os
import uuid

import pytest
from sqlalchemy import select, update
from support import running, setup_admin
from support.runs import accept

from maestro import backup
from maestro.core.errors import MaestroError
from maestro.db import schema as t
from maestro.domain import versions
from maestro.domain.states import Role
from maestro.services import updates
from maestro.services.principal import Channel, Principal

ADMIN = Principal("usr_admin", "admin", Role.ADMIN, Channel.WEB_SESSION, session_id="s")
NOTES = """Faster runs.

## 破坏性变更

- The run list moved.

## Fixes

- Something small.

## 更新后

Prepare every dataset again.
"""


def release(version="0.6.0", **extra) -> dict:
    return {
        "tag_name": f"v{version}",
        "name": f"v{version}",
        "html_url": f"https://github.com/o/r/releases/tag/v{version}",
        "published_at": "2026-11-07T00:00:00Z",
        "draft": False,
        "prerelease": False,
        "body": NOTES,
        **extra,
    }


def with_updates(service, latest="0.6.0", current="0.6.0.dev0", fetch=None):
    """Turn web updates on for ``service`` with a fake release source."""

    def found():
        return updates.parse_release(release(latest))

    service.updates = updates.Updates(
        service.db,
        enabled=True,
        repository="o/r",
        api_url="http://127.0.0.1:9",
        directory=service.settings.update_dir,
        fetch=fetch or found,
        current=current,
    )
    return service.updates


@pytest.fixture
def admin(service):
    with service.db.write() as conn:
        conn.execute(
            t.users.insert().values(
                id="usr_admin", username="admin", display_name="a", role="admin",
                status="active", password_hash="x", revision=1, created_at="t", updated_at="t",
            )
        )  # fmt: skip
    return ADMIN


def set_run_state(service, run_id, state):
    with service.db.write() as conn:
        conn.execute(update(t.runs).where(t.runs.c.id == run_id).values(state=state))


def request_file(service) -> dict | None:
    path = service.settings.update_dir / updates.REQUEST_FILE
    return json.loads(path.read_text()) if path.exists() else None


def report(service, update_id, state, pid=None, **extra) -> None:
    service.settings.update_dir.mkdir(exist_ok=True)
    status = {"format": 1, "id": update_id, "version": "0.6.0", "state": state,
              "pid": pid or os.getpid(), "phase": "done", **extra}  # fmt: skip
    (service.settings.update_dir / updates.STATUS_FILE).write_text(json.dumps(status))


def queue_reason(service, run_id):
    with service.db.read() as conn:
        return conn.execute(select(t.runs.c.queue_reason).where(t.runs.c.id == run_id)).scalar()


# --- versions and releases ----------------------------------------------------------


@pytest.mark.parametrize(
    ("candidate", "current", "newer"),
    [
        ("0.5.0", "0.5.0.dev0", True),  # a development build is offered its release
        ("0.5.0", "0.5.0", False),
        ("0.4.9", "0.5.0.dev3", False),  # never a downgrade
        ("0.5.0", "0.5.0rc1", True),
        ("0.5.0rc2", "0.5.0rc1", True),
        ("0.5.0b1", "0.5.0.dev9", True),
        ("0.10.0", "0.9.9", True),  # numbers, not text
        ("0.5.1", "0.5.0+local.1", True),
        ("x", "0.5.0", False),
        ("0.6.0", "unknown", False),
    ],
)
def test_versions_are_ordered_as_released(candidate, current, newer):
    assert versions.newer(candidate, current) is newer


def test_only_formal_releases_are_read_with_the_notes_worth_reading_first():
    found = updates.parse_release(release("1.2.3"))
    assert found["version"] == "1.2.3" and found["tag"] == "v1.2.3"
    assert [h["title"] for h in found["highlights"]] == ["破坏性变更", "更新后"]
    assert found["highlights"][0]["text"] == "- The run list moved."
    assert found["highlights"][1]["text"] == "Prepare every dataset again."
    for refused in (
        release(prerelease=True),
        release(draft=True),
        release(tag_name="v1.2.3rc1"),
        release(tag_name="dev-abc"),
        [],
    ):
        with pytest.raises(updates.Unavailable):
            updates.parse_release(refused)


def test_a_failed_lookup_says_why():
    def answer(status, body):
        return lambda url: (status, body)

    for status, body, says in (
        (404, b"{}", "no formal release"),
        (502, b"", "answered 502"),
        (200, b"not json", "no JSON"),
    ):
        with pytest.raises(updates.Unavailable, match=says):
            updates.latest_release("http://api", "o/r", answer(status, body))
    urls = []
    updates.latest_release("http://api", "o/r", lambda url: (urls.append(url), 200,
                           json.dumps(release()).encode())[1:])  # fmt: skip
    assert urls == ["http://api/repos/o/r/releases/latest"]


def test_a_failed_check_keeps_the_last_good_answer(service, admin):
    answers = [updates.parse_release(release()), updates.Unavailable("offline")]

    def fetch():
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    service_updates = with_updates(service, fetch=fetch)
    assert service_updates.check(admin)["latest"]["version"] == "0.6.0"
    status = service_updates.check(admin)
    assert status["latest"]["version"] == "0.6.0" and status["check_error"] == "offline"
    assert status["update_available"] is True


def test_checks_are_daily_after_a_first_delay_and_hourly_after_a_failure(service):
    now = [1000.0]
    service_updates = with_updates(service)
    service_updates.clock = lambda: now[0]
    assert not service_updates.due(started=1000.0)
    now[0] += updates.FIRST_CHECK_DELAY_SEC
    assert service_updates.due(started=1000.0)
    service_updates._check()
    now[0] += updates.RETRY_AFTER_FAILURE_SEC
    assert not service_updates.due(started=1000.0)
    service_updates.fetch = lambda: (_ for _ in ()).throw(updates.Unavailable("down"))
    now[0] += updates.CHECK_INTERVAL_SEC
    service_updates._check()
    now[0] += updates.RETRY_AFTER_FAILURE_SEC
    assert service_updates.due(started=1000.0)
    service_updates.enabled = False
    assert not service_updates.due(started=1000.0)


# --- asking for an update -----------------------------------------------------------


def test_only_the_newest_checked_formal_release_newer_than_this_one_can_be_asked_for(
    service, admin
):
    service_updates = with_updates(service, latest="0.6.0", current="0.6.0")
    with pytest.raises(MaestroError) as unknown:
        service_updates.request(admin, "0.6.0", False)
    assert unknown.value.code == "UPDATE_TARGET_UNKNOWN"
    service_updates.check(admin)
    with pytest.raises(MaestroError) as same:
        service_updates.request(admin, "0.6.0", False)
    assert same.value.code == "UPDATE_NOT_NEWER"
    with pytest.raises(MaestroError) as preview:
        service_updates.request(admin, "0.6.1.dev0", False)
    assert preview.value.code == "REQUEST_INVALID"
    service_updates.enabled = False
    with pytest.raises(MaestroError) as off:
        service_updates.check(admin)
    assert off.value.code == "UPDATE_CHECK_DISABLED"


def test_update_waits_for_in_flight_work_while_runs_queue(service, admin, owner, version_id):
    service_updates = with_updates(service)
    service_updates.check(admin)
    busy = accept(service, owner, version_id)["run_id"]
    set_run_state(service, busy, "running")
    status = service_updates.request(admin, "0.6.0", False)
    assert status["pending"]["state"] == "draining"
    assert [r["id"] for r in status["in_flight"]["runs"]] == [busy]
    assert status["in_flight"]["runs"][0]["owner"]["username"] == "owner"
    assert request_file(service) is None
    # Runs are still accepted and queued, and wait; new imports and preparations do not
    # start at all.
    waiting = accept(service, owner, version_id)["run_id"]
    assert service.worker._claim() is None
    assert queue_reason(service, waiting) == "UPDATE_PENDING"
    for start in (
        lambda: service.datasets.request_import(
            admin, {"source_kind": "fake", "namespace": "demo", "name": "arith", "ref": "v2"}
        ),
        lambda: service.datasets.request_first_preparation(admin, version_id, None),
    ):
        with pytest.raises(MaestroError) as refused:
            start()
        assert refused.value.code == "UPDATE_PENDING"
    with pytest.raises(MaestroError) as again:
        service_updates.request(admin, "0.6.0", False)
    assert again.value.code == "UPDATE_ALREADY_PENDING"
    with service.db.read() as conn:
        assert service_updates.notice(conn)["state"] == "draining"

    # What a stop would interrupt is the same thing maestroctl asks the old version for.
    assert backup.activity(service.settings)["busy"] is True
    set_run_state(service, busy, "finished")
    assert backup.activity(service.settings)["busy"] is False
    service_updates.tick()
    sent = request_file(service)
    assert sent["version"] == "0.6.0" and sent["interrupt_running"] is False
    assert sent["id"] == status["pending"]["id"] and sent["format"] == 1
    assert service_updates.status(admin)["pending"]["state"] == "requested"
    assert service.worker._claim() is None

    # The host reports success: the pending update ends and the queue moves again.
    os.unlink(service.settings.update_dir / updates.REQUEST_FILE)
    report(service, sent["id"], "running", phase="installing")
    service_updates.tick()
    assert service_updates.status(admin)["pending"]["progress"]["phase"] == "installing"
    report(service, sent["id"], "succeeded", to_build="0.6.0-abc", from_build="0.6.0.dev0-x")
    service_updates.tick()
    status = service_updates.status(admin)
    assert status["pending"] is None and status["last"]["state"] == "succeeded"
    assert status["last"]["result"]["to_build"] == "0.6.0-abc"
    claimed = service.worker._claim()
    assert claimed is not None and claimed[0] == waiting


def test_update_now_interrupts_instead_of_waiting(service, admin, owner, version_id):
    service_updates = with_updates(service)
    service_updates.check(admin)
    set_run_state(service, accept(service, owner, version_id)["run_id"], "running")
    service_updates.request(admin, "0.6.0", False)
    assert request_file(service) is None
    status = service_updates.interrupt(admin)
    assert status["pending"]["state"] == "requested"
    assert request_file(service)["interrupt_running"] is True


def test_cancelling_resumes_dispatching_until_the_host_has_taken_the_request(
    service, admin, owner, version_id
):
    service_updates = with_updates(service)
    service_updates.check(admin)
    busy = accept(service, owner, version_id)["run_id"]
    set_run_state(service, busy, "running")
    service_updates.request(admin, "0.6.0", False)
    status = service_updates.cancel(admin)
    assert status["pending"] is None and status["last"]["state"] == "cancelled"
    assert status["last"]["finished_by"]["username"] == "admin"
    with pytest.raises(MaestroError) as nothing:
        service_updates.cancel(admin)
    assert nothing.value.code == "UPDATE_NOT_PENDING"

    # Handed over but not yet taken: cancelling takes the request back.
    set_run_state(service, busy, "finished")
    service_updates.request(admin, "0.6.0", False)
    assert request_file(service) is not None
    assert service_updates.cancel(admin)["last"]["state"] == "cancelled"
    assert request_file(service) is None

    # Taken by the host: too late to cancel; the update reports its own result.
    service_updates.request(admin, "0.6.0", False)
    os.unlink(service.settings.update_dir / updates.REQUEST_FILE)
    with pytest.raises(MaestroError) as late:
        service_updates.cancel(admin)
    assert late.value.code == "UPDATE_IN_PROGRESS"


def test_failed_updates_end_the_pending_state_with_the_reason(service, admin):
    service_updates = with_updates(service)
    service_updates.check(admin)
    first = service_updates.request(admin, "0.6.0", False)["pending"]["id"]
    report(service, "upd_someone_else", "failed")
    service_updates.tick()
    assert service_updates.status(admin)["pending"]["id"] == first
    report(service, first, "failed", rolled_back=True, message="migration failed")
    service_updates.tick()
    last = service_updates.status(admin)["last"]
    assert last["state"] == "failed" and last["result"]["rolled_back"] is True
    assert last["result"]["message"] == "migration failed"

    # A task that died without a result does not keep dispatching paused forever.
    second = service_updates.request(admin, "0.6.0", False)["pending"]["id"]
    report(service, second, "running", pid=2**22 + 12345)
    service_updates.tick()
    last = service_updates.status(admin)["last"]
    assert last["id"] == second and last["state"] == "failed"
    assert "without reporting a result" in last["result"]["message"]


def test_a_pending_update_survives_a_restart(service, admin):
    service_updates = with_updates(service)
    service_updates.check(admin)
    service_updates.request(admin, "0.6.0", False)
    restarted = with_updates(service)
    assert restarted.status(admin)["pending"]["version"] == "0.6.0"
    with service.db.read() as conn:
        assert updates.dispatch_paused(conn)


# --- the web API --------------------------------------------------------------------


def test_admins_drive_updates_through_the_api_and_members_see_the_notice(service):
    with_updates(service)
    with running(service, run_worker=False) as client:
        api = setup_admin(service, client)
        caps = client.get("/api/v1/capabilities").json()["data"]
        assert caps["features"]["web_update"] is True
        assert caps["actions"]["admin.update"]["pat"] is False
        status = api.get("/api/v1/admin/update", expect=200).json()["data"]
        assert status["latest"] is None and status["current_version"] == "0.6.0.dev0"
        status = api.post("/api/v1/admin/update/check", expect=200).json()["data"]
        assert status["update_available"] and status["latest"]["highlights"]
        api.post("/api/v1/admin/update/request", json={"version": "0.6.0"}, expect=200)
        notice = api.get("/api/v1/system-status", expect=200).json()["data"]["detail"]
        assert notice["update"]["version"] == "0.6.0"
        assert notice["update"]["state"] == "requested"  # nothing was in flight
        api.post("/api/v1/admin/update/cancel", expect=200)
        detail = api.get("/api/v1/system-status", expect=200).json()["data"]["detail"]
        assert detail["update"] is None
        refused = api.post("/api/v1/admin/update/now", expect=409).json()
        assert refused["error"]["code"] == "UPDATE_NOT_PENDING"


def test_updates_are_session_only_administration(service):
    with_updates(service)
    with running(service, run_worker=False) as client:
        api = setup_admin(service, client)
        issued = api.post(
            "/api/v1/me/pats",
            json={"name": f"p-{uuid.uuid4().hex[:6]}", "permissions": ["operations"]},
            expect=201,
        ).json()["data"]
        for method, path in (("GET", ""), ("POST", "/check"), ("POST", "/cancel")):
            response = client.request(
                method,
                f"/api/v1/admin/update{path}",
                headers={"Authorization": f"Bearer {issued['token']}"},
            )
            assert response.status_code == 403, response.text
