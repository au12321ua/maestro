"""Experiment plans: saved partial configuration requests.

The creator edits a plan (each change is a new revision); everyone reads and copies it;
the creator or an administrator archives it, which only stops new use. A request names
a plan with ``plan_id``: its own fields win, the plan fills in the rest, and the fields
the plan supplied are reported with the plan as their source (author ->
team -> plan -> override). A run records the plan revision and keeps its own values, so
later edits change nothing for it.
"""

from __future__ import annotations

from sqlalchemy import Connection, insert, select, update

from maestro.core import clock
from maestro.core.errors import FieldError, MaestroError, not_found
from maestro.core.ids import new_id
from maestro.db import schema as t
from maestro.db.engine import Database
from maestro.services import audit, deleted
from maestro.services.configuration import NESTED_FIELDS, REQUEST_FIELDS, check_template
from maestro.services.principal import (
    Principal,
    can_manage_owned,
    require_experiment,
    require_manage_owned,
)


def merge(template: dict, body: dict) -> tuple[dict, list[str]]:
    """The request with the plan's fields where the body has none, and which fields
    (JSON pointers) the plan supplied."""
    merged = {k: v for k, v in body.items() if k not in REQUEST_FIELDS}
    supplied = []
    for key in REQUEST_FIELDS:
        own, planned = body.get(key), template.get(key)
        if key in NESTED_FIELDS:
            own = {k: v for k, v in (own or {}).items() if v is not None}
            planned = planned or {}
            supplied += [f"/{key}/{k}" for k in planned if k not in own]
            if own or planned:
                merged[key] = {**planned, **own}
        elif own is not None:
            merged[key] = own
        elif planned is not None:
            merged[key] = planned
            supplied.append(f"/{key}")
    return merged, supplied


def _check_text(body: dict, partial: bool) -> dict:
    errors = []
    found = {}
    if "name" in body or not partial:
        name = body.get("name")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 128:
            errors.append(FieldError("/name", "LENGTH", "1-128 characters"))
        else:
            found["name"] = name.strip()
    if "description" in body or not partial:
        description = body.get("description") or ""
        if not isinstance(description, str) or len(description) > 1024:
            errors.append(FieldError("/description", "LENGTH", "at most 1024 characters"))
        else:
            found["description"] = description
    if errors:
        raise MaestroError("PLAN_INVALID", "Invalid experiment plan", 422, field_errors=errors)
    return found


class Plans:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _query():
        return select(t.experiment_plans, t.users.c.username).join(
            t.users, t.users.c.id == t.experiment_plans.c.owner_id
        )

    @staticmethod
    def _public(row, principal: Principal | None = None) -> dict:
        allowed = ["view", "use", "copy"] if row["archived_at"] is None else ["view", "copy"]
        if principal is not None and principal.user_id == row["owner_id"]:
            allowed.append("edit")
        if principal is not None and can_manage_owned(principal, row["owner_id"]):
            allowed += ["archive", "delete"]
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "owner_id": row["owner_id"],
            "owner_username": row["username"],
            "revision": row["revision"],
            "template": row["template_json"],
            "copied_from": row["copied_from"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "archived_at": row["archived_at"],
            "allowed_actions": allowed,
        }

    def _row(self, conn: Connection, plan_id: str):
        row = conn.execute(self._query().where(t.experiment_plans.c.id == plan_id)).mappings()
        row = row.first()
        if row is None:
            raise not_found("Experiment plan")
        return row

    def list(self, principal: Principal, include_archived: bool = False) -> list[dict]:
        query = self._query().where(deleted.visible(t.experiment_plans.c.id, "experiment_plan"))
        if not include_archived:
            query = query.where(t.experiment_plans.c.archived_at.is_(None))
        query = query.order_by(t.experiment_plans.c.name, t.experiment_plans.c.created_at)
        with self.db.read() as conn:
            return [self._public(r, principal) for r in conn.execute(query).mappings()]

    def get(self, principal: Principal, plan_id: str) -> dict:
        with self.db.read() as conn:
            row = self._row(conn, plan_id)
            deleted.refuse_purged(conn, "experiment_plan", plan_id, "Experiment plan")
            return {
                **self._public(row, principal),
                "deletion": deleted.state(conn, "experiment_plan", plan_id),
            }

    def _insert(self, conn, principal, text: dict, template: dict, copied_from=None) -> str:
        plan_id = new_id("pln")
        at = clock.to_text(clock.now())
        conn.execute(
            insert(t.experiment_plans).values(
                id=plan_id,
                owner_id=principal.user_id,
                name=text["name"],
                description=text["description"],
                revision=1,
                template_json=template,
                copied_from=copied_from,
                created_at=at,
                updated_at=at,
            )
        )
        audit.record(
            conn,
            principal,
            "plan.copy" if copied_from else "plan.create",
            object_type="experiment_plan",
            object_id=plan_id,
            detail={"name": text["name"], **({"copied_from": copied_from} if copied_from else {})},
        )
        return plan_id

    def create(self, principal: Principal, body: dict) -> dict:
        require_experiment(principal)
        text = _check_text(body, partial=False)
        template = check_template(body.get("template") or {})
        with self.db.write() as conn:
            plan_id = self._insert(conn, principal, text, template)
        return self.get(principal, plan_id)

    def update(self, principal: Principal, plan_id: str, body: dict) -> dict:
        """The creator only: administrators archive plans but do not edit them."""
        require_experiment(principal)
        text = _check_text(body, partial=True)
        template = check_template(body["template"]) if "template" in body else None
        with self.db.write() as conn:
            row = self._row(conn, plan_id)
            if row["owner_id"] != principal.user_id:
                raise MaestroError(
                    "FORBIDDEN", "Only the creator edits a plan; copy it to change it", 403
                )
            if deleted.state(conn, "experiment_plan", plan_id):
                raise MaestroError("PLAN_DELETED", "Experiment plan was deleted", 409)
            if body.get("expected_revision") != row["revision"]:
                raise MaestroError(
                    "PLAN_REVISION_CHANGED",
                    "The plan changed since it was read; reload it",
                    409,
                    field_errors=[FieldError("/expected_revision", "STALE")],
                )
            values = {**text, "revision": row["revision"] + 1}
            if template is not None:
                values["template_json"] = template
            conn.execute(
                update(t.experiment_plans)
                .where(t.experiment_plans.c.id == plan_id)
                .values(**values, updated_at=clock.to_text(clock.now()))
            )
            audit.record(
                conn,
                principal,
                "plan.update",
                object_type="experiment_plan",
                object_id=plan_id,
                detail={"revision": values["revision"]},
            )
        return self.get(principal, plan_id)

    def copy(self, principal: Principal, plan_id: str, body: dict) -> dict:
        require_experiment(principal)
        with self.db.write() as conn:
            row = self._row(conn, plan_id)
            deleted.refuse_purged(conn, "experiment_plan", plan_id, "Experiment plan")
            text = _check_text(
                {
                    "name": body.get("name") or f"{row['name']} (copy)",
                    "description": row["description"],
                },
                partial=False,
            )
            new = self._insert(conn, principal, text, row["template_json"], copied_from=plan_id)
        return self.get(principal, new)

    def set_archived(self, principal: Principal, plan_id: str, archived: bool) -> dict:
        with self.db.write() as conn:
            row = self._row(conn, plan_id)
            require_manage_owned(principal, row["owner_id"])
            if (row["archived_at"] is not None) != archived:
                conn.execute(
                    update(t.experiment_plans)
                    .where(t.experiment_plans.c.id == plan_id)
                    .values(archived_at=clock.to_text(clock.now()) if archived else None)
                )
                audit.record(
                    conn,
                    principal,
                    "plan.archive" if archived else "plan.unarchive",
                    object_type="experiment_plan",
                    object_id=plan_id,
                )
        return self.get(principal, plan_id)

    @staticmethod
    def apply(conn: Connection, body: dict) -> dict:
        """The request with its plan applied; the plan reference travels in ``plan``."""
        body = {k: v for k, v in body.items() if k != "plan"}
        plan_id = body.pop("plan_id", None)
        expected = body.pop("plan_revision", None)
        if plan_id is None:
            return body
        row = (
            conn.execute(select(t.experiment_plans).where(t.experiment_plans.c.id == plan_id))
            .mappings()
            .first()
        )
        path = [FieldError("/plan_id", "UNAVAILABLE")]
        if row is None:
            raise not_found("Experiment plan")
        if deleted.state(conn, "experiment_plan", plan_id):
            raise MaestroError(
                "PLAN_DELETED", "Experiment plan was deleted", 409, field_errors=path
            )
        if row["archived_at"] is not None:
            raise MaestroError(
                "PLAN_ARCHIVED", "Experiment plan is archived", 409, field_errors=path
            )
        if expected is not None and expected != row["revision"]:
            raise MaestroError(
                "PLAN_REVISION_CHANGED",
                f"The plan is at revision {row['revision']}, not {expected}",
                409,
                field_errors=[FieldError("/plan_revision", "STALE")],
            )
        merged, supplied = merge(row["template_json"], body)
        merged["plan"] = {"plan_id": plan_id, "revision": row["revision"], "fields": supplied}
        return merged
