"""Personal access tokens.

Created, listed and revoked only by their owner in a web session. The raw value
is returned once; only its digest and a short display prefix are stored.
``accepted_runs`` changes only inside the run acceptance transaction.
"""

from sqlalchemy import insert, select, update

from maestro.core import clock
from maestro.core.errors import FieldError, MaestroError, not_found
from maestro.core.ids import new_id
from maestro.db import schema as t
from maestro.db.engine import Database
from maestro.domain.states import PatScope
from maestro.security import tokens
from maestro.services import audit, deleted, policy
from maestro.services.principal import Principal, require_web_session


def public_pat(row) -> dict:
    now = clock.to_text(clock.now())
    if row["revoked_at"]:
        state = "revoked"
    elif row["expires_at"] <= now:
        state = "expired"
    else:
        state = "active"
    return {
        "id": row["id"],
        "name": row["name"],
        "scope": row["scope"],
        "token_prefix": row["token_prefix"],
        "state": state,
        "expires_at": row["expires_at"],
        "run_limit": row["run_limit"],
        "accepted_runs": row["accepted_runs"],
        "remaining_runs": row["run_limit"] - row["accepted_runs"],
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
        "revoked_at": row["revoked_at"],
        "revision": row["revision"],
    }


class Pats:
    def __init__(self, db: Database):
        self.db = db
        self.policies = policy.Policies(db)

    def create(
        self,
        principal: Principal,
        name: str,
        scope: PatScope,
        expires_in_days: int | None,
        run_limit: int | None,
    ) -> dict:
        require_web_session(principal)
        team = self.policies.current()
        days = team.pat_default_days if expires_in_days is None else expires_in_days
        run_limit = team.pat_default_run_limit if run_limit is None else run_limit
        errors = []
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 128:
            errors.append(FieldError("/name", "LENGTH", "1-128 characters"))
        if not 1 <= days <= team.pat_max_days:
            errors.append(FieldError("/expires_in_days", "LIMIT_EXCEEDED", limit=team.pat_max_days))
        if not 0 <= run_limit <= team.pat_max_run_limit:
            errors.append(FieldError("/run_limit", "LIMIT_EXCEEDED", limit=team.pat_max_run_limit))
        if errors:
            raise MaestroError("PAT_INVALID", "Invalid token settings", 422, field_errors=errors)
        token = tokens.generate("pat")
        now = clock.now()
        pat_id = new_id("pat")
        with self.db.write() as conn:
            conn.execute(
                insert(t.pats).values(
                    id=pat_id,
                    owner_id=principal.user_id,
                    name=name.strip(),
                    token_hash=tokens.digest("pat", token),
                    token_prefix=token[: tokens.PAT_DISPLAY_PREFIX],
                    scope=scope,
                    expires_at=clock.to_text(now + clock.timedelta(days=days)),
                    run_limit=run_limit,
                    accepted_runs=0,
                    revision=1,
                    created_at=clock.to_text(now),
                )
            )
            audit.record(
                conn,
                principal,
                "pat.create",
                object_type="pat",
                object_id=pat_id,
                detail={"scope": scope.value, "expires_in_days": days, "run_limit": run_limit},
            )
            row = conn.execute(select(t.pats).where(t.pats.c.id == pat_id)).mappings().one()
        return {**public_pat(row), "token": token}

    def list_own(self, principal: Principal) -> list[dict]:
        require_web_session(principal)
        with self.db.read() as conn:
            rows = conn.execute(
                select(t.pats)
                .where(t.pats.c.owner_id == principal.user_id, deleted.visible(t.pats.c.id, "pat"))
                .order_by(t.pats.c.created_at.desc())
            )
            return [public_pat(r) for r in rows.mappings().all()]

    def revoke(self, principal: Principal, pat_id: str) -> dict:
        """Revocation blocks later calls; accepted runs continue."""
        require_web_session(principal)
        with self.db.write() as conn:
            row = (
                conn.execute(
                    select(t.pats).where(
                        t.pats.c.id == pat_id, t.pats.c.owner_id == principal.user_id
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise not_found("Access token")
            if row["revoked_at"] is None:
                conn.execute(
                    update(t.pats)
                    .where(t.pats.c.id == pat_id)
                    .values(revoked_at=clock.to_text(clock.now()), revision=row["revision"] + 1)
                )
                audit.record(conn, principal, "pat.revoke", object_type="pat", object_id=pat_id)
            row = conn.execute(select(t.pats).where(t.pats.c.id == pat_id)).mappings().one()
        return public_pat(row)
