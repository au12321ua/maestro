"""Updates to a formal release, checked for and started in the web pages.

The service never installs anything itself. It looks up the newest formal release of the
repository named in ``config.toml`` (GitHub Releases: no drafts, no pre-releases). When an
administrator asks for it, the service records a pending update: runs are still accepted
and queued but none is dispatched, and new imports, first preparations and evictions are
refused. Once nothing a restart would interrupt is left, or the administrator chose to
interrupt it, the service writes a request that names only the version into
``data_dir/update``. A root systemd path unit written by ``maestroctl`` runs
``maestroctl update --request``: it downloads that version from the configured
repository, verifies it and runs the usual update (backup, migration, readiness check,
rollback on failure). Its progress and result come back in a status file in the same
directory; the pending update ends when the result is in, whichever version reads it.

The pending update is a database row, so it survives restarts, including the one the
update itself causes; the backup taken before migrating holds it too, so a rolled-back
old version finds it as well.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx
from sqlalchemy import Connection, func, insert, select, update

from maestro import __version__
from maestro.core import clock
from maestro.core.errors import FieldError, MaestroError
from maestro.core.ids import new_id
from maestro.db import schema as t
from maestro.db.engine import Database
from maestro.domain import versions
from maestro.services import audit
from maestro.services.principal import Principal, require_admin_session

log = logging.getLogger(__name__)

# Run states and operation kinds a stop interrupts. Queued runs wait for the new version,
# exports are rebuilt after a restart, and runs and batches have their own operations,
# which the run states already cover. ``maestro activity`` (used by ``maestroctl update``)
# counts the same work, so a drained service is never refused by the update.
ACTIVE_RUN_STATES = ("preparing", "running", "finalizing")
ACTIVE_OPERATION_STATUSES = ("accepted", "running")
UNINTERRUPTED_KINDS = ("run", "batch", "export")

DRAINING = "draining"
REQUESTED = "requested"
PENDING = (DRAINING, REQUESTED)
FINISHED = ("succeeded", "failed", "cancelled")

REQUEST_FILE = "request.json"
STATUS_FILE = "status.json"
FORMAT = 1

CHECK_INTERVAL_SEC = 24 * 3600
RETRY_AFTER_FAILURE_SEC = 3600
# The first automatic check waits a little after start, so a service that keeps
# restarting does not ask GitHub every time.
FIRST_CHECK_DELAY_SEC = 60
TICK_SEC = 2.0
TIMEOUT_SEC = 30
MAX_ANSWER_BYTES = 1024 * 1024
NOTES_MAX = 64 * 1024
IN_FLIGHT_LISTED = 100
# Sections of the release notes worth reading before updating, shown above the notes.
HIGHLIGHT = re.compile(
    r"破坏|不兼容|迁移|首次准备|重新准备|更新前|更新后|"
    r"breaking|incompatib|migrat|re-?prepar|first preparation|before updat|after updat",
    re.IGNORECASE,
)
HEADING = re.compile(r"(#{1,6})\s+(.*?)\s*#*\s*")


class Unavailable(Exception):
    """The release source could not be asked or gave no usable answer."""


def _get(url: str) -> tuple[int, bytes]:
    """Status and body of a GET through the service's own proxy environment."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"maestro/{__version__}",
    }
    try:
        with (
            httpx.Client(timeout=TIMEOUT_SEC, follow_redirects=True, trust_env=True) as client,
            client.stream("GET", url, headers=headers) as response,
        ):
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > MAX_ANSWER_BYTES:
                    raise Unavailable(f"the answer from {url} is too large")
            return response.status_code, bytes(body)
    except httpx.HTTPError as error:
        raise Unavailable(f"{type(error).__name__}: {error}") from error


def highlights(notes: str) -> list[dict]:
    """Sections of Markdown ``notes`` whose heading names breaking changes or work to do
    around the update (migration, preparing again), in order."""
    found: list[dict] = []
    current: dict | None = None
    level = 0
    for line in notes.splitlines():
        heading = HEADING.fullmatch(line.strip())
        if heading:
            depth = len(heading[1])
            if current is not None and depth <= level:
                found.append(current)
                current = None
            if current is None and HIGHLIGHT.search(heading[2]):
                current, level = {"title": heading[2], "lines": []}, depth
                continue
        if current is not None:
            current["lines"].append(line)
    if current is not None:
        found.append(current)
    return [
        {"title": section["title"], "text": "\n".join(section["lines"]).strip()}
        for section in found
    ]


def parse_release(document: object) -> dict:
    """What the page shows of one GitHub release; refuses anything but a formal one."""
    if not isinstance(document, dict):
        raise Unavailable("the release source gave no release")
    tag = document.get("tag_name")
    if not isinstance(tag, str) or not versions.FORMAL.fullmatch(tag.removeprefix("v")):
        raise Unavailable(f"the newest release has tag {tag!r}, not a formal version")
    if document.get("draft") or document.get("prerelease"):
        raise Unavailable(f"release {tag} is a draft or a pre-release")
    notes = document.get("body") if isinstance(document.get("body"), str) else ""
    notes = notes[:NOTES_MAX]
    return {
        "version": tag.removeprefix("v"),
        "tag": tag,
        "name": str(document.get("name") or tag),
        "url": str(document.get("html_url") or ""),
        "published_at": document.get("published_at")
        if isinstance(document.get("published_at"), str)
        else None,
        "notes": notes,
        "highlights": highlights(notes),
    }


def latest_release(
    api_url: str, repository: str, get: Callable[[str], tuple[int, bytes]] = _get
) -> dict:
    """The newest formal release of ``repository``; raises ``Unavailable``."""
    url = f"{api_url}/repos/{repository}/releases/latest"
    status, body = get(url)
    if status == 404:
        raise Unavailable(f"{repository} has no formal release")
    if status != 200:
        raise Unavailable(f"{api_url} answered {status} for {repository}")
    try:
        document = json.loads(body)
    except ValueError as error:
        raise Unavailable(f"{api_url} gave no JSON for {repository}") from error
    return parse_release(document)


# --- the pending update -------------------------------------------------------------


def pending(conn: Connection):
    """The pending update row, if any (draining or requested)."""
    return (
        conn.execute(
            select(t.service_updates)
            .where(t.service_updates.c.state.in_(PENDING))
            .order_by(t.service_updates.c.created_at.desc())
            .limit(1)
        )
        .mappings()
        .first()
    )


def dispatch_paused(conn: Connection) -> bool:
    """Queued runs wait while an update is pending."""
    return (
        conn.execute(
            select(t.service_updates.c.id).where(t.service_updates.c.state.in_(PENDING)).limit(1)
        ).first()
        is not None
    )


def refuse_while_pending(conn: Connection) -> None:
    """New imports, first preparations and evictions wait for a pending update: they
    would delay it, or be interrupted by it."""
    row = pending(conn)
    if row is not None:
        raise MaestroError(
            "UPDATE_PENDING",
            f"An update to {row['version']} is pending; this can start once it is done "
            "or cancelled",
            409,
        )


def _busy(conn: Connection) -> bool:
    runs = conn.execute(
        select(t.runs.c.id).where(t.runs.c.state.in_(ACTIVE_RUN_STATES)).limit(1)
    ).first()
    if runs is not None:
        return True
    operations = conn.execute(
        select(t.operations.c.id)
        .where(
            t.operations.c.status.in_(ACTIVE_OPERATION_STATUSES),
            t.operations.c.kind.not_in(UNINTERRUPTED_KINDS),
        )
        .limit(1)
    ).first()
    return operations is not None


def _in_flight(conn: Connection) -> dict:
    """What the pending update waits for, with who started it."""
    runs = conn.execute(
        select(
            t.runs.c.id,
            t.runs.c.name,
            t.runs.c.state,
            t.runs.c.started_at,
            t.users.c.id.label("owner_id"),
            t.users.c.username,
        )
        .join(t.users, t.users.c.id == t.runs.c.owner_id)
        .where(t.runs.c.state.in_(ACTIVE_RUN_STATES))
        .order_by(t.runs.c.started_at, t.runs.c.id)
        .limit(IN_FLIGHT_LISTED)
    ).all()
    operations = conn.execute(
        select(
            t.operations.c.id,
            t.operations.c.kind,
            t.operations.c.phase,
            t.operations.c.created_at,
            t.operations.c.owner_id,
            t.users.c.username,
        )
        .outerjoin(t.users, t.users.c.id == t.operations.c.owner_id)
        .where(
            t.operations.c.status.in_(ACTIVE_OPERATION_STATUSES),
            t.operations.c.kind.not_in(UNINTERRUPTED_KINDS),
        )
        .order_by(t.operations.c.created_at, t.operations.c.id)
        .limit(IN_FLIGHT_LISTED)
    ).all()
    run_count = conn.execute(
        select(func.count()).select_from(t.runs).where(t.runs.c.state.in_(ACTIVE_RUN_STATES))
    ).scalar()
    queued = conn.execute(
        select(func.count()).select_from(t.runs).where(t.runs.c.state == "queued")
    ).scalar()
    return {
        "runs": [
            {
                "id": r.id,
                "name": r.name,
                "state": r.state,
                "started_at": r.started_at,
                "owner": {"id": r.owner_id, "username": r.username},
            }
            for r in runs
        ],
        "operations": [
            {
                "id": o.id,
                "kind": o.kind,
                "phase": o.phase,
                "created_at": o.created_at,
                "owner": {"id": o.owner_id, "username": o.username} if o.owner_id else None,
            }
            for o in operations
        ],
        "active_runs": run_count,
        "queued_runs": queued,
    }


def _alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # the update task runs as root
        return True
    return True


class Updates:
    def __init__(
        self,
        db: Database,
        *,
        enabled: bool,
        repository: str,
        api_url: str,
        directory: Path,
        fetch: Callable[[], dict] | None = None,
        current: str = __version__,
        clock_of: Callable[[], float] = time.monotonic,
    ):
        self.db = db
        self.enabled = enabled
        self.repository = repository
        self.directory = directory
        self.fetch = fetch or (lambda: latest_release(api_url, repository))
        self.current = current
        self.clock = clock_of
        self.lock = threading.Lock()
        self.checked: dict | None = None
        self._last_attempt: float | None = None
        self._last_failed = False
        self._stopping = False
        self.wake: Callable[[], None] = lambda: None

    # --- checking -------------------------------------------------------------

    def _check(self) -> dict:
        at = clock.to_text(clock.now())
        try:
            latest, error = self.fetch(), None
        except Unavailable as failure:
            latest, error = None, str(failure)
        except Exception as failure:  # never let a check take the service down
            log.exception("update check failed")
            latest, error = None, f"{type(failure).__name__}: {failure}"
        with self.lock:
            self._last_attempt = self.clock()
            self._last_failed = error is not None
            if error is None or self.checked is None:
                self.checked = {"checked_at": at, "latest": latest, "error": error}
            else:  # keep what the last good check found, with the newer failure
                self.checked = {**self.checked, "error": error, "error_at": at}
        if error:
            log.warning("update check: %s", error)
        return self.checked

    def check(self, principal: Principal) -> dict:
        require_admin_session(principal)
        if not self.enabled:
            raise MaestroError(
                "UPDATE_CHECK_DISABLED", "Update checks are turned off in config.toml", 409
            )
        self._check()
        return self.status(principal)

    def due(self, started: float) -> bool:
        if not self.enabled:
            return False
        now = self.clock()
        with self.lock:
            last, failed = self._last_attempt, self._last_failed
        if last is None:
            return now - started >= FIRST_CHECK_DELAY_SEC
        return now - last >= (RETRY_AFTER_FAILURE_SEC if failed else CHECK_INTERVAL_SEC)

    # --- reading --------------------------------------------------------------

    def _status_file(self) -> dict | None:
        try:
            with (self.directory / STATUS_FILE).open("rb") as file:
                data = json.loads(file.read(64 * 1024))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _public(self, conn: Connection, row) -> dict:
        def user(user_id):
            found = conn.execute(
                select(t.users.c.id, t.users.c.username).where(t.users.c.id == user_id)
            ).first()
            return {"id": found.id, "username": found.username} if found else None

        release = row["release_json"] or {}
        progress = None
        if row["state"] == REQUESTED:
            status = self._status_file()
            if status and status.get("id") == row["id"]:
                progress = {
                    "state": str(status.get("state") or ""),
                    "phase": str(status.get("phase") or ""),
                    "started_at": status.get("started_at"),
                }
        return {
            "id": row["id"],
            "version": row["version"],
            "state": row["state"],
            "interrupt_running": row["interrupt_running"],
            "from_version": row["from_version"],
            "requested_by": user(row["requested_by"]),
            "finished_by": user(row["finished_by"]) if row["finished_by"] else None,
            "created_at": row["created_at"],
            "requested_at": row["requested_at"],
            "finished_at": row["finished_at"],
            "release": {
                "name": release.get("name") or row["version"],
                "url": release.get("url") or "",
                "published_at": release.get("published_at"),
            },
            "progress": progress,
            "result": row["result_json"],
        }

    def notice(self, conn: Connection) -> dict | None:
        """What every signed-in member sees while an update is pending."""
        row = pending(conn)
        if row is None:
            return None
        return {"state": row["state"], "version": row["version"], "since": row["created_at"]}

    def status(self, principal: Principal) -> dict:
        require_admin_session(principal)
        with self.lock:
            checked = dict(self.checked) if self.checked else None
        latest = checked["latest"] if checked else None
        with self.db.read() as conn:
            row = pending(conn)
            last = (
                conn.execute(
                    select(t.service_updates)
                    .where(t.service_updates.c.state.in_(FINISHED))
                    .order_by(t.service_updates.c.created_at.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            return {
                "enabled": self.enabled,
                "repository": self.repository,
                "current_version": self.current,
                "checked_at": checked["checked_at"] if checked else None,
                "check_error": checked.get("error") if checked else None,
                "latest": latest,
                "update_available": bool(
                    latest and versions.newer(latest["version"], self.current)
                ),
                "pending": self._public(conn, row) if row is not None else None,
                "last": self._public(conn, last) if last is not None else None,
                "in_flight": _in_flight(conn),
            }

    # --- asking ---------------------------------------------------------------

    def request(self, principal: Principal, version: str, interrupt_running: bool) -> dict:
        require_admin_session(principal)
        if not self.enabled:
            raise MaestroError(
                "UPDATE_CHECK_DISABLED", "Update checks are turned off in config.toml", 409
            )
        if not versions.FORMAL.fullmatch(version):
            raise MaestroError(
                "REQUEST_INVALID",
                "Only a formal release can be installed from the web pages",
                422,
                field_errors=[FieldError("/version", "NOT_FORMAL_VERSION")],
            )
        with self.lock:
            latest = (self.checked or {}).get("latest")
        if latest is None or latest["version"] != version:
            raise MaestroError(
                "UPDATE_TARGET_UNKNOWN",
                f"{version} is not the newest formal release found; check for updates first",
                409,
            )
        if not versions.newer(version, self.current):
            raise MaestroError(
                "UPDATE_NOT_NEWER", f"{version} is not newer than {self.current}", 409
            )
        at = clock.to_text(clock.now())
        with self.db.write() as conn:
            existing = pending(conn)
            if existing is not None:
                raise MaestroError(
                    "UPDATE_ALREADY_PENDING",
                    f"An update to {existing['version']} is already pending",
                    409,
                )
            update_id = new_id("upd")
            conn.execute(
                insert(t.service_updates).values(
                    id=update_id,
                    version=version,
                    from_version=self.current,
                    release_json={k: latest[k] for k in ("name", "url", "published_at")},
                    interrupt_running=bool(interrupt_running),
                    state=DRAINING,
                    requested_by=principal.user_id,
                    created_at=at,
                )
            )
            audit.record(
                conn,
                principal,
                "update.request",
                object_type="service_update",
                object_id=update_id,
                detail={"version": version, "interrupt_running": bool(interrupt_running)},
            )
        self.tick()
        return self.status(principal)

    def interrupt(self, principal: Principal) -> dict:
        """Stop waiting: in-flight work is interrupted as a restart would (scores already
        recorded stay)."""
        require_admin_session(principal)
        with self.db.write() as conn:
            row = pending(conn)
            if row is None:
                raise MaestroError("UPDATE_NOT_PENDING", "No update is pending", 409)
            if row["state"] == DRAINING and not row["interrupt_running"]:
                conn.execute(
                    update(t.service_updates)
                    .where(t.service_updates.c.id == row["id"])
                    .values(interrupt_running=True)
                )
                audit.record(
                    conn,
                    principal,
                    "update.interrupt",
                    object_type="service_update",
                    object_id=row["id"],
                )
        self.tick()
        return self.status(principal)

    def cancel(self, principal: Principal) -> dict:
        require_admin_session(principal)
        with self.db.write() as conn:
            row = pending(conn)
            if row is None:
                raise MaestroError("UPDATE_NOT_PENDING", "No update is pending", 409)
            if row["state"] == REQUESTED:
                try:
                    (self.directory / REQUEST_FILE).unlink()
                except FileNotFoundError:
                    # The update task has taken the request: it is past the point where
                    # the service could stop it, and reports its own result.
                    raise MaestroError(
                        "UPDATE_IN_PROGRESS",
                        "The update has started on the host and can no longer be cancelled",
                        409,
                    ) from None
            self._finish(conn, row, "cancelled", None, finished_by=principal.user_id)
            audit.record(
                conn,
                principal,
                "update.cancel",
                object_type="service_update",
                object_id=row["id"],
            )
        self.wake()
        return self.status(principal)

    # --- progress -------------------------------------------------------------

    def _finish(self, conn, row, state: str, result: dict | None, finished_by=None) -> None:
        conn.execute(
            update(t.service_updates)
            .where(t.service_updates.c.id == row["id"])
            .values(
                state=state,
                result_json=result,
                finished_at=clock.to_text(clock.now()),
                finished_by=finished_by,
            )
        )

    def _write_request(self, row) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        request = {
            "format": FORMAT,
            "id": row["id"],
            "version": row["version"],
            "interrupt_running": bool(row["interrupt_running"]),
            "requested_by": row["requested_by"],
            "requested_at": clock.to_text(clock.now()),
        }
        handle, staged = tempfile.mkstemp(prefix=".request-", dir=self.directory)
        try:
            with os.fdopen(handle, "w") as file:
                json.dump(request, file)
            os.replace(staged, self.directory / REQUEST_FILE)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(staged)
            raise

    def tick(self) -> None:
        """Move the pending update on: hand it to the host once drained, end it once the
        host reports a result."""
        with self.db.write() as conn:
            row = pending(conn)
            if row is None:
                return
            if row["state"] == DRAINING:
                if not row["interrupt_running"] and _busy(conn):
                    return
                self._write_request(row)
                conn.execute(
                    update(t.service_updates)
                    .where(t.service_updates.c.id == row["id"])
                    .values(state=REQUESTED, requested_at=clock.to_text(clock.now()))
                )
                log.warning("update to %s requested from the host", row["version"])
                return
            status = self._status_file()
            if not status or status.get("id") != row["id"]:
                return
            state = status.get("state")
            if state == "running" and not _alive(status.get("pid")):
                state = "failed"
                status["message"] = (
                    "the update task ended without reporting a result; "
                    "see the maestro-update unit in journald"
                )
            if state not in ("succeeded", "failed"):
                return
            result = {
                "message": str(status.get("message") or "")[:2000],
                "rolled_back": bool(status.get("rolled_back")),
                "to_build": status.get("to_build"),
                "from_build": status.get("from_build"),
            }
            self._finish(conn, row, state, result)
            log.warning("update to %s %s: %s", row["version"], state, result["message"])
        self.wake()

    async def run_forever(self) -> None:
        loop = asyncio.get_running_loop()
        started = self.clock()
        checking: asyncio.Future | None = None
        while not self._stopping:
            try:
                self.tick()
            except Exception:
                log.exception("update tick failed")
            if (checking is None or checking.done()) and self.due(started):
                checking = loop.run_in_executor(None, self._check)
            await asyncio.sleep(TICK_SEC)

    async def stop(self) -> None:
        self._stopping = True
