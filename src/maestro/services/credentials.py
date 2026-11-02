"""Team credentials: write-only values bound by explicit reference.

Values are AES-GCM encrypted under the active master-key generation with the
credential id and revision as associated data. No API returns a value or any
value-derived digest; replacing a value creates a new revision.
"""

from __future__ import annotations

import re

from sqlalchemy import insert, select, update

from maestro.core import clock
from maestro.core.errors import FieldError, MaestroError, not_found
from maestro.core.ids import new_id
from maestro.db import schema as t
from maestro.db.engine import Database
from maestro.security import master_key
from maestro.security.master_key import MasterKeys
from maestro.services import audit, deleted
from maestro.services.principal import Principal, require_admin_session

KINDS = ("model_api_key", "hub_token", "generic_secret")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
VALUE_MAX = 16384


def check_value(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= VALUE_MAX
        or any(c.isspace() or ord(c) < 32 for c in value)
    ):
        raise MaestroError(
            "CREDENTIAL_VALUE_INVALID",
            "A nonempty single-line value is required",
            422,
            field_errors=[FieldError("/value", "INVALID")],
        )
    return value


def public_credential(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "provider": row["provider"],
        "description": row["description"],
        "enabled": row["enabled"],
        "revision": row["revision"],
        "key_version": row["key_version"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class Credentials:
    def __init__(self, db: Database, keys: MasterKeys):
        self.db = db
        self.keys = keys

    def list(self, principal: Principal) -> list[dict]:
        require_admin_session(principal)
        with self.db.read() as conn:
            rows = conn.execute(
                select(t.credentials)
                .where(deleted.visible(t.credentials.c.id, "credential"))
                .order_by(t.credentials.c.name)
            )
            return [public_credential(r) for r in rows.mappings().all()]

    def directory(self, principal: Principal) -> list[dict]:
        """Names of the team's usable credentials, for anyone signed in: a member
        or an agent needs them to reference a secret parameter. Never values or anything
        derived from them, and nothing about who made or changed them."""
        with self.db.read() as conn:
            rows = conn.execute(
                select(t.credentials)
                .where(
                    deleted.visible(t.credentials.c.id, "credential"),
                    t.credentials.c.enabled.is_(True),
                )
                .order_by(t.credentials.c.name)
            ).mappings()
            return [
                {k: r[k] for k in ("id", "name", "kind", "provider", "description")} for r in rows
            ]

    def create(
        self, principal: Principal, name: str, kind: str, provider: str, description: str, value
    ) -> dict:
        require_admin_session(principal)
        errors = []
        if not isinstance(name, str) or not NAME.fullmatch(name):
            errors.append(FieldError("/name", "PATTERN"))
        if kind not in KINDS:
            errors.append(FieldError("/kind", "ENUM"))
        if not isinstance(provider, str) or not 1 <= len(provider) <= 64:
            errors.append(FieldError("/provider", "LENGTH"))
        if not isinstance(description, str) or len(description) > 512:
            errors.append(FieldError("/description", "LENGTH"))
        if errors:
            raise MaestroError("CREDENTIAL_INVALID", "Invalid credential", 422, field_errors=errors)
        value = check_value(value)
        cred_id = new_id("cred")
        at = clock.to_text(clock.now())
        with self.db.write() as conn:
            if conn.execute(select(t.credentials.c.id).where(t.credentials.c.name == name)).first():
                raise MaestroError(
                    "CREDENTIAL_NAME_TAKEN",
                    "Name already used",
                    409,
                    field_errors=[FieldError("/name", "TAKEN")],
                )
            generation, key = self.keys.current(conn)
            nonce, ciphertext = master_key.encrypt(
                key, value, master_key.credential_aad(cred_id, 1)
            )
            conn.execute(
                insert(t.credentials).values(
                    id=cred_id,
                    name=name,
                    kind=kind,
                    provider=provider,
                    description=description,
                    nonce=nonce,
                    encrypted_payload=ciphertext,
                    key_version=generation,
                    enabled=True,
                    revision=1,
                    created_by=principal.user_id,
                    created_at=at,
                    updated_at=at,
                )
            )
            audit.record(
                conn,
                principal,
                "credential.create",
                object_type="credential",
                object_id=cred_id,
                detail={"kind": kind, "provider": provider},
            )
            return public_credential(self._row(conn, cred_id))

    @staticmethod
    def _row(conn, cred_id):
        row = conn.execute(select(t.credentials).where(t.credentials.c.id == cred_id)).mappings()
        row = row.first()
        if row is None:
            raise not_found("Credential")
        return row

    @classmethod
    def _live(cls, conn, cred_id):
        """A deleted credential cannot be changed, enabled or used."""
        row = cls._row(conn, cred_id)
        if deleted.state(conn, "credential", cred_id):
            raise MaestroError("CREDENTIAL_DELETED", "Credential was deleted", 409)
        return row

    def replace_value(self, principal: Principal, cred_id: str, value, expected_revision: int):
        require_admin_session(principal)
        value = check_value(value)
        with self.db.write() as conn:
            row = self._live(conn, cred_id)
            if row["revision"] != expected_revision:
                raise MaestroError("REVISION_CONFLICT", "Credential changed; refresh", 409)
            revision = row["revision"] + 1
            generation, key = self.keys.current(conn)
            nonce, ciphertext = master_key.encrypt(
                key, value, master_key.credential_aad(cred_id, revision)
            )
            conn.execute(
                update(t.credentials)
                .where(t.credentials.c.id == cred_id)
                .values(
                    nonce=nonce,
                    encrypted_payload=ciphertext,
                    key_version=generation,
                    revision=revision,
                    updated_at=clock.to_text(clock.now()),
                )
            )
            audit.record(
                conn,
                principal,
                "credential.rotate_value",
                object_type="credential",
                object_id=cred_id,
                detail={"revision": revision},
            )
            return public_credential(self._row(conn, cred_id))

    def set_enabled(self, principal: Principal, cred_id: str, enabled: bool, expected_revision):
        require_admin_session(principal)
        with self.db.write() as conn:
            row = self._live(conn, cred_id)
            if row["revision"] != expected_revision:
                raise MaestroError("REVISION_CONFLICT", "Credential changed; refresh", 409)
            if row["enabled"] != enabled:
                # The revision is bound into the ciphertext, so the bump re-encrypts the
                # same value under the new revision in this transaction.
                revision = row["revision"] + 1
                value = master_key.decrypt(
                    self.keys.key_for(conn, row["key_version"]),
                    row["nonce"],
                    row["encrypted_payload"],
                    master_key.credential_aad(cred_id, row["revision"]),
                )
                generation, key = self.keys.current(conn)
                nonce, ciphertext = master_key.encrypt(
                    key, value, master_key.credential_aad(cred_id, revision)
                )
                conn.execute(
                    update(t.credentials)
                    .where(t.credentials.c.id == cred_id)
                    .values(
                        nonce=nonce,
                        encrypted_payload=ciphertext,
                        key_version=generation,
                        enabled=enabled,
                        revision=revision,
                        updated_at=clock.to_text(clock.now()),
                    )
                )
                audit.record(
                    conn,
                    principal,
                    f"credential.{'enable' if enabled else 'disable'}",
                    object_type="credential",
                    object_id=cred_id,
                )
            return public_credential(self._row(conn, cred_id))

    def resolve_value(self, cred_id: str) -> tuple[str, int]:
        """Internal use by executors only; never exposed through the API."""
        with self.db.read() as conn:
            row = self._live(conn, cred_id)
            if not row["enabled"]:
                raise MaestroError("CREDENTIAL_DISABLED", "Credential is disabled", 409)
            key = self.keys.key_for(conn, row["key_version"])
            value = master_key.decrypt(
                key,
                row["nonce"],
                row["encrypted_payload"],
                master_key.credential_aad(cred_id, row["revision"]),
            )
        return value, row["revision"]

    def master_status(self, principal: Principal) -> dict:
        require_admin_session(principal)
        return self.keys.status()

    def rotate_master(self, principal: Principal, expected_generation: int) -> dict:
        require_admin_session(principal)

        def on_commit(conn, generation, count):
            audit.record(
                conn,
                principal,
                "master_key.rotate",
                object_type="master_key",
                detail={"generation": generation, "reencrypted": count},
            )

        return self.keys.rotate(expected_generation, on_commit)


def credential_problem(conn, cred_id: str, kinds: tuple[str, ...]) -> str | None:
    """Why a credential cannot be bound or used now, or None. Bindings keep only the
    reference, so this is checked when configuring and again when accepting a run."""
    row = conn.execute(
        select(t.credentials.c.kind, t.credentials.c.enabled).where(t.credentials.c.id == cred_id)
    ).first()
    if row is None:
        return "NOT_FOUND"
    if deleted.state(conn, "credential", cred_id):
        return "DELETED"
    if row.kind not in kinds:
        return "WRONG_KIND"
    if not row.enabled:
        return "DISABLED"
    return None
