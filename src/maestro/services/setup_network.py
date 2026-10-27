"""Setup network revisions (N-08; ``maestro.domain.setup_network`` says what they hold).

Administrators save a whole new revision in a web session; the newest is in force.
Every signed-in principal may read every revision: attempts and preparations name the
revision they used, and nothing in one may be a credential.
"""

from __future__ import annotations

from sqlalchemy import Connection, func, insert, select

from maestro.core import clock
from maestro.core.errors import FieldError, MaestroError
from maestro.core.ids import new_id
from maestro.db import schema as t
from maestro.db.engine import Database
from maestro.domain import setup_network
from maestro.domain.setup_network import BUILT_IN, NOTE_MAX, SetupNetwork
from maestro.services import audit
from maestro.services.principal import Principal, require_admin_session


def _value(network: SetupNetwork) -> dict:
    return {
        "proxy": network.proxy,
        "no_proxy": network.no_proxy,
        "env": dict(network.env),
        "harness_setup_timeout_sec": network.harness_setup_timeout_sec,
    }


def _from_row(row) -> SetupNetwork:
    value = row["value_json"]
    return SetupNetwork(
        row["revision"],
        value.get("proxy", ""),
        value.get("no_proxy", ""),
        dict(value.get("env") or {}),
        value.get("harness_setup_timeout_sec"),
    )


def current(conn: Connection) -> SetupNetwork:
    row = (
        conn.execute(select(t.setup_networks).order_by(t.setup_networks.c.revision.desc()).limit(1))
        .mappings()
        .first()
    )
    return BUILT_IN if row is None else _from_row(row)


def _public(network: SetupNetwork, row=None) -> dict:
    return {
        "revision": network.revision,
        **_value(network),
        "note": row["note"] if row is not None else "",
        "created_by_username": row["username"] if row is not None else None,
        "created_at": row["created_at"] if row is not None else None,
        "warnings": network.warnings(),
    }


class SetupNetworks:
    def __init__(self, db: Database):
        self.db = db

    def current(self) -> SetupNetwork:
        with self.db.read() as conn:
            return current(conn)

    def _rows(self, conn, revision: int | None = None):
        query = (
            select(t.setup_networks, t.users.c.username)
            .join(t.users, t.users.c.id == t.setup_networks.c.created_by)
            .order_by(t.setup_networks.c.revision.desc())
        )
        if revision is not None:
            query = query.where(t.setup_networks.c.revision == revision)
        return conn.execute(query).mappings().all()

    def get(self, principal: Principal) -> dict:
        with self.db.read() as conn:
            rows = self._rows(conn)
        return _public(_from_row(rows[0]), rows[0]) if rows else _public(BUILT_IN)

    def history(self, principal: Principal) -> list[dict]:
        """Newest first, ending with revision 0 (nothing set)."""
        with self.db.read() as conn:
            rows = self._rows(conn)
        return [_public(_from_row(r), r) for r in rows] + [_public(BUILT_IN)]

    def update(self, principal: Principal, value: dict, note: str, expected_revision: int) -> dict:
        """Save a new revision. ``expected_revision`` is the one the administrator
        edited, so two editors cannot overwrite each other silently."""
        require_admin_session(principal)
        if not isinstance(note, str) or len(note) > NOTE_MAX:
            raise MaestroError(
                "SETUP_NETWORK_INVALID",
                "Invalid setup network",
                422,
                field_errors=[FieldError("/note", "TOO_LONG", f"at most {NOTE_MAX} characters")],
            )
        with self.db.write() as conn:
            now = current(conn)
            if now.revision != expected_revision:
                raise MaestroError(
                    "SETUP_NETWORK_REVISION_CHANGED",
                    f"The setup network is now at revision {now.revision}; reload and edit again",
                    409,
                )
            revision = (
                conn.execute(
                    select(func.coalesce(func.max(t.setup_networks.c.revision), 0))
                ).scalar()
                + 1
            )
            network = setup_network.check(value, revision)
            conn.execute(
                insert(t.setup_networks).values(
                    id=new_id("snw"),
                    revision=revision,
                    value_json=_value(network),
                    note=note,
                    created_by=principal.user_id,
                    created_at=clock.to_text(clock.now()),
                )
            )
            audit.record(
                conn,
                principal,
                "setup_network.update",
                object_type="setup_network",
                object_id=None,
                detail={"revision": revision, **_value(network)},
            )
        return self.get(principal)
