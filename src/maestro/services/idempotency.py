"""Idempotency records scoped by principal + action + key.

Callers look up and store inside the same write transaction that creates the
resource, so a retried request either sees the stored response or does the work
exactly once.
"""

from dataclasses import dataclass

from sqlalchemy import Connection, delete, insert, select

from maestro.core import clock
from maestro.core.digests import digest_json
from maestro.core.errors import FieldError, MaestroError
from maestro.core.ids import new_id
from maestro.db import schema as t
from maestro.domain import limits

KEY_MAX = 128


@dataclass
class Stored:
    status_code: int
    response: dict
    record_id: str


def check_key(key: object) -> str:
    if (
        not isinstance(key, str)
        or not 8 <= len(key) <= KEY_MAX
        or not key.isascii()
        or not key.isprintable()
    ):
        raise MaestroError(
            "IDEMPOTENCY_KEY_INVALID",
            f"Idempotency-Key must be 8-{KEY_MAX} printable ASCII characters",
            400,
            field_errors=[FieldError("Idempotency-Key", "INVALID")],
        )
    return key


def request_hash(body: dict) -> str:
    return digest_json(body)


def lookup(conn: Connection, principal_id: str, action: str, key: str, req_hash: str):
    row = (
        conn.execute(
            select(t.idempotency_records).where(
                t.idempotency_records.c.principal_id == principal_id,
                t.idempotency_records.c.action == action,
                t.idempotency_records.c.key == key,
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    if row["expires_at"] <= clock.to_text(clock.now()):
        # Expired keys may create new work; drop the old record.
        conn.execute(delete(t.idempotency_records).where(t.idempotency_records.c.id == row["id"]))
        return None
    if row["request_hash"] != req_hash:
        raise MaestroError(
            "IDEMPOTENCY_CONFLICT",
            "This Idempotency-Key was already used with a different request",
            409,
            suggested_actions=[{"type": "use_new_idempotency_key"}],
        )
    return Stored(row["status_code"], row["response_json"], row["id"])


def store(
    conn: Connection,
    principal_id: str,
    action: str,
    key: str,
    req_hash: str,
    status_code: int,
    response: dict,
) -> str:
    now = clock.now()
    record_id = new_id("idem")
    conn.execute(
        insert(t.idempotency_records).values(
            id=record_id,
            principal_id=principal_id,
            action=action,
            key=key,
            request_hash=req_hash,
            status_code=status_code,
            response_json=response,
            created_at=clock.to_text(now),
            expires_at=clock.to_text(now + clock.timedelta(days=limits.IDEMPOTENCY_DAYS)),
        )
    )
    return record_id
