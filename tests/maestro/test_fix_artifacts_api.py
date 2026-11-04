"""Artifact download headers, who sees export Operations, and the export request limits."""

import re
import urllib.parse

from sqlalchemy import update
from support import create_pat, invite_member, running, setup_admin, submit, wait_operation

from maestro.db import schema as t
from maestro.domain import limits
from maestro.services.disk import GIB, DiskWatch


def finished_run(api, version_id, **body) -> str:
    accepted = submit(api, version_id, **body).json()["data"]
    wait_operation(api, accepted["operation_id"])
    return accepted["run_id"]


def rename_artifact(service, artifact_id: str, relative_path: str) -> None:
    with service.db.write() as conn:
        conn.execute(
            update(t.artifacts)
            .where(t.artifacts.c.id == artifact_id)
            .values(relative_path=relative_path)
        )


# --- Content-Disposition from an agent-controlled file name ---------------------------


def test_download_header_survives_any_file_name(service, version_id):
    with running(service) as client:
        admin = setup_admin(service, client)
        run_id = finished_run(admin, version_id)
        artifact = admin.get(f"/api/v1/runs/{run_id}/artifacts").json()["data"][0]
        url = f"/api/v1/artifacts/{artifact['id']}/download"

        def header(name: str) -> str:
            rename_artifact(service, artifact["id"], f"dir/{name}")
            response = admin.get(url, expect=200)
            assert "set-cookie" not in response.headers
            value = response.headers["content-disposition"]
            assert value.isascii() and not re.search(r"[\x00-\x1f\x7f]", value)
            return value

        assert header("plain.txt") == 'attachment; filename="plain.txt"'

        chinese = header("输出结果.txt")
        assert re.fullmatch(
            r"attachment; filename=\"[A-Za-z0-9._ -]+\"; filename\*=UTF-8''.+", chinese
        )
        encoded = chinese.split("filename*=UTF-8''")[1]
        assert urllib.parse.unquote(encoded) == "输出结果.txt"

        # Control characters are removed, so the value cannot split the header block.
        injected = header("evil\r\nSet-Cookie: x=1.txt")
        assert "\n" not in injected and "\r" not in injected
        assert (
            urllib.parse.unquote(injected.split("filename*=UTF-8''")[1])
            == "evilSet-Cookie: x=1.txt"
        )

        # Quotes and backslashes cannot close or escape the quoted string.
        tricky = header('a\\";b".txt')
        quoted = tricky.split('filename="')[1].split('"; filename*')[0]
        assert '"' not in quoted and "\\" not in quoted
        assert urllib.parse.unquote(tricky.split("filename*=UTF-8''")[1]) == "a;b.txt"

        long_name = header("长" * 5000 + ".log")
        assert len(long_name) < 2000
        assert urllib.parse.unquote(long_name.split("filename*=UTF-8''")[1]).endswith(".log")

        # Nothing printable is left: the header still names a file.
        assert header("\r\n\t").startswith('attachment; filename="')


# --- Operations of exports are as private as the exports ------------------------------


def operation_ids(api, query="") -> set[str]:
    return {x["id"] for x in api.get(f"/api/v1/operations{query}", expect=200).json()["data"]}


def test_export_operations_are_visible_only_to_the_owner_and_admin_sessions(service, version_id):
    with running(service) as client:
        admin = setup_admin(service, client)
        alice = invite_member(service, admin, "alice")
        bob = invite_member(service, admin, "bob")
        run_id = finished_run(alice, version_id)
        created = alice.post("/api/v1/exports", json={"run_ids": [run_id]}, expect=202)
        export_op = created.json()["data"]["operation_id"]
        wait_operation(alice, export_op)
        run_op = next(
            x["id"]
            for x in alice.get("/api/v1/operations?kind=run").json()["data"]
            if x["resource"]["id"] == run_id
        )
        url = f"/api/v1/operations/{export_op}"

        # The owner, with a session or with her own token, and an administrator's session.
        alice_pat = create_pat(alice, ())
        for who in (alice, alice_pat, admin):
            detail = who.get(url, expect=200).json()["data"]
            assert detail["result"]["export_id"] == created.json()["data"]["id"]
            assert export_op in operation_ids(who)
            assert export_op in operation_ids(who, "?kind=export")

        # Everybody else gets what GET /exports/{id} gives: not found, and no listing.
        bob_read = create_pat(bob, ())
        bob_experiment = create_pat(bob, ("experiment",))
        admin_pat = create_pat(admin, ())
        for who in (bob, bob_read, bob_experiment, admin_pat):
            assert who.get(url).status_code == 404
            assert export_op not in operation_ids(who)
            assert operation_ids(who, "?kind=export") == set()
            assert operation_ids(
                who, f"?owner_id={alice.get('/api/v1/me').json()['data']['id']}"
            ) >= {run_op}
            assert who.get(f"/api/v1/exports/{created.json()['data']['id']}").status_code == 404

        # Other kinds stay team-visible (a run's Operation, for instance).
        for who in (bob, bob_read, admin_pat):
            assert who.get(f"/api/v1/operations/{run_op}", expect=200)
            assert run_op in operation_ids(who)


# --- Export requests: per-user allowance and the disk watermark ------------------------


def test_exports_are_limited_per_user_and_paused_while_disk_is_low(service, version_id, settings):
    with running(service, run_worker=False) as client:
        admin = setup_admin(service, client)
        alice = invite_member(service, admin, "alice")
        bob = invite_member(service, admin, "bob")
        run_id = submit(alice, version_id).json()["data"]["run_id"]
        reader = create_pat(alice, ())
        body = {"run_ids": [run_id], "include_artifacts": False}
        allowed = limits.EXPORT_MAX_ACTIVE_PER_USER
        assert allowed == 2
        reader.post("/api/v1/exports", json=body, expect=202)
        alice.post("/api/v1/exports", json=body, expect=202)  # PATs and session share it

        refused = reader.post("/api/v1/exports", json=body)
        assert refused.status_code == 429
        error = refused.json()["error"]
        assert error["code"] == "EXPORT_LIMIT_REACHED" and error["retryable"] is True
        assert refused.headers["retry-after"]
        bob.post("/api/v1/exports", json=body, expect=202)  # everybody has an allowance

        # Finished exports no longer count.
        service.maintenance.run_once()
        reader.post("/api/v1/exports", json=body, expect=202)

        free = {"bytes": 100 * GIB}
        service.exports.disk = DiskWatch(
            settings.data_dir,
            type(settings)(
                **{
                    **settings.__dict__,
                    "storage_accept_min_free_gb": 20,
                    "storage_dispatch_min_free_gb": 10,
                    "storage_accept_min_free_percent": 0,
                    "storage_dispatch_min_free_percent": 0,
                }
            ),
            usage=lambda _: (1000 * GIB, 0, free["bytes"]),
        )
        service.maintenance.run_once()
        bob.post("/api/v1/exports", json=body, expect=202)
        free["bytes"] = 15 * GIB  # below the acceptance watermark
        service.maintenance.run_once()
        low = alice.post("/api/v1/exports", json=body)
        assert low.status_code == 503
        assert low.json()["error"]["code"] == "STORAGE_LOW" and low.json()["error"]["retryable"]
        assert "exports" in low.json()["error"]["message"]
        # Downloads and queries keep working; only new packages are paused.
        assert alice.get("/api/v1/exports", expect=200).json()["data"]
        free["bytes"] = 100 * GIB
        alice.post("/api/v1/exports", json=body, expect=202)
