"""Team policy revisions (03 section 2, U-21): defaults and hard limits.

Administrators change the policy in a web session; every change is a new revision and
the newest is in force. Before the first change, revision 0 (the built-in values in
``domain.limits``) is in force. A run records the revision its configuration used, and
tightening a limit never rewrites accepted runs (03 section 3 rule 7).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, func, insert, select

from maestro.core import clock
from maestro.core.errors import FieldError, MaestroError
from maestro.core.ids import new_id
from maestro.db import schema as t
from maestro.db.engine import Database
from maestro.domain import limits
from maestro.services import audit
from maestro.services.principal import Principal, require_admin_session


@dataclass(frozen=True)
class Setting:
    default: int
    low: int
    high: int
    group: str
    description: str


def _s(name: str, low: int, high: int, group: str, description: str) -> Setting:
    return Setting(getattr(limits, name.upper()), low, high, group, description)


SETTINGS: dict[str, Setting] = {
    "repetitions_default": _s("repetitions_default", 1, 1000, "runs", "Trials per task"),
    "repetitions_max": _s("repetitions_max", 1, 1000, "runs", "Most trials per task"),
    "run_default_concurrency": _s(
        "run_default_concurrency", 1, 1000, "runs", "Trials of a run at once"
    ),
    "run_max_concurrency": _s(
        "run_max_concurrency", 1, 1000, "runs", "Most trials of a run at once"
    ),
    "run_deadline_hours": _s(
        "run_deadline_hours", 1, 8760, "runs", "Total time of a run, from acceptance"
    ),
    "run_deadline_hours_max": _s(
        "run_deadline_hours_max", 1, 8760, "runs", "Longest total time a run may ask for"
    ),
    "run_max_tasks": _s("run_max_tasks", 1, 100000, "runs", "Most tasks in a run"),
    "run_max_trials": _s("run_max_trials", 1, 1000000, "runs", "Most trials in a run"),
    "trial_cpus_max": _s("trial_cpus_max", 1, 1024, "runs", "Most CPUs of one trial"),
    "trial_memory_mb_max": _s(
        "trial_memory_mb_max", 128, 4 * 1024 * 1024, "runs", "Most memory (MB) of one trial"
    ),
    "service_cpus_default": _s(
        "service_cpus_default", 1, 1024, "runs", "CPUs of a Compose service without its own limit"
    ),
    "service_memory_mb_default": _s(
        "service_memory_mb_default",
        16,
        4 * 1024 * 1024,
        "runs",
        "Memory (MB) of a Compose service without its own limit",
    ),
    "agent_timeout_sec_max": _s(
        "agent_timeout_sec_max", 1, 7 * 24 * 3600, "runs", "Longest agent time of one trial"
    ),
    "bootstrap_timeout_sec": _s(
        "bootstrap_timeout_sec", 1, 7 * 24 * 3600, "runs", "Attachment install time of one trial"
    ),
    "bootstrap_timeout_sec_max": _s(
        "bootstrap_timeout_sec_max", 1, 7 * 24 * 3600, "runs", "Longest attachment install time"
    ),
    "run_max_attachments": _s("run_max_attachments", 0, 64, "runs", "Most attachments in a run"),
    "batch_max_runs": _s("batch_max_runs", 1, 10000, "runs", "Most runs in one batch"),
    "batch_max_trials": _s("batch_max_trials", 1, 1000000, "runs", "Most trials in one batch"),
    "pat_default_days": _s("pat_default_days", 1, 3650, "access", "Token validity (days)"),
    "pat_max_days": _s("pat_max_days", 1, 3650, "access", "Longest token validity (days)"),
    "pat_default_run_limit": _s(
        "pat_default_run_limit", 0, 1000000, "access", "Runs a token may submit"
    ),
    "pat_max_run_limit": _s(
        "pat_max_run_limit", 0, 1000000, "access", "Most runs a token may submit"
    ),
    "invitation_hours": _s("invitation_hours", 1, 720, "access", "Invitation validity (hours)"),
    "login_attempts_per_minute": _s(
        "login_attempts_per_minute", 1, 1000, "access", "Failed logins per account a minute"
    ),
    "artifact_ttl_days": _s("artifact_ttl_days", 1, 3650, "retention", "Raw artifacts kept (days)"),
    "export_ttl_hours": _s("export_ttl_hours", 1, 720, "retention", "Exports kept (hours)"),
    "export_max_runs": _s("export_max_runs", 1, 10000, "retention", "Most runs in one export"),
    "export_max_active_per_user": _s(
        "export_max_active_per_user", 1, 100, "retention", "Exports a user may have in progress"
    ),
}
# (default, limit): the default may not exceed the limit.
PAIRS = (
    ("repetitions_default", "repetitions_max"),
    ("run_default_concurrency", "run_max_concurrency"),
    ("run_deadline_hours", "run_deadline_hours_max"),
    ("bootstrap_timeout_sec", "bootstrap_timeout_sec_max"),
    ("pat_default_days", "pat_max_days"),
    ("pat_default_run_limit", "pat_max_run_limit"),
)


@dataclass(frozen=True)
class Policy:
    revision: int
    values: dict

    def __getattr__(self, name: str) -> int:
        try:
            return self.values[name]
        except KeyError:
            raise AttributeError(name) from None

    def public(self, extra: dict | None = None) -> dict:
        return {
            "revision": self.revision,
            "values": dict(self.values),
            **(extra or {}),
        }


BUILT_IN = Policy(0, {name: s.default for name, s in SETTINGS.items()})


def current(conn: Connection) -> Policy:
    row = (
        conn.execute(select(t.team_policies).order_by(t.team_policies.c.revision.desc()).limit(1))
        .mappings()
        .first()
    )
    if row is None:
        return BUILT_IN
    # A setting added after the revision was saved takes its built-in value.
    return Policy(row["revision"], {**BUILT_IN.values, **row["values_json"]})


def check(values: dict) -> dict:
    errors = []
    for name, value in values.items():
        setting = SETTINGS.get(name)
        if setting is None:
            errors.append(FieldError(f"/values/{name}", "UNKNOWN"))
        elif type(value) is not int or not setting.low <= value <= setting.high:
            errors.append(
                FieldError(
                    f"/values/{name}",
                    "RANGE",
                    f"an integer from {setting.low} to {setting.high}",
                    limit=setting.high,
                )
            )
    if not errors:
        for default, limit in PAIRS:
            if values[default] > values[limit]:
                errors.append(FieldError(f"/values/{default}", "ABOVE_LIMIT", f"at most {limit}"))
    if errors:
        raise MaestroError("POLICY_INVALID", "Invalid team policy", 422, field_errors=errors)
    return values


def describe() -> list[dict]:
    return [
        {
            "name": name,
            "group": s.group,
            "description": s.description,
            "built_in": s.default,
            "min": s.low,
            "max": s.high,
        }
        for name, s in SETTINGS.items()
    ]


class Policies:
    def __init__(self, db: Database):
        self.db = db

    def current(self) -> Policy:
        with self.db.read() as conn:
            return current(conn)

    def get(self, principal: Principal) -> dict:
        """Everyone may read the policy in force; capabilities report it too."""
        return self.current().public({"settings": describe()})

    def history(self, principal: Principal) -> list[dict]:
        require_admin_session(principal)
        with self.db.read() as conn:
            rows = conn.execute(
                select(t.team_policies, t.users.c.username)
                .join(t.users, t.users.c.id == t.team_policies.c.created_by)
                .order_by(t.team_policies.c.revision.desc())
            ).mappings()
            return [
                {
                    "revision": r["revision"],
                    "values": r["values_json"],
                    "created_by": r["created_by"],
                    "created_by_username": r["username"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]

    def update(self, principal: Principal, changes: dict, expected_revision: int) -> dict:
        """Change some settings; the others keep their values. ``expected_revision`` is
        the revision the administrator edited, so two editors cannot overwrite each
        other silently."""
        require_admin_session(principal)
        if not isinstance(changes, dict) or not changes:
            raise MaestroError(
                "POLICY_INVALID",
                "Name at least one setting to change",
                422,
                field_errors=[FieldError("/values", "REQUIRED")],
            )
        with self.db.write() as conn:
            now = current(conn)
            if now.revision != expected_revision:
                raise MaestroError(
                    "POLICY_REVISION_CHANGED",
                    f"The policy is now at revision {now.revision}; reload and edit again",
                    409,
                )
            values = check({**now.values, **changes})
            revision = (
                conn.execute(
                    select(func.coalesce(func.max(t.team_policies.c.revision), 0))
                ).scalar()
                + 1
            )
            conn.execute(
                insert(t.team_policies).values(
                    id=new_id("pol"),
                    revision=revision,
                    values_json=values,
                    created_by=principal.user_id,
                    created_at=clock.to_text(clock.now()),
                )
            )
            changed = {k: v for k, v in values.items() if now.values.get(k) != v}
            audit.record(
                conn,
                principal,
                "policy.update",
                object_type="team_policy",
                object_id=None,
                detail={"revision": revision, "changed": changed},
            )
        return self.get(principal)
