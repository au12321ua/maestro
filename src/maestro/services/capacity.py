"""Execution capacity: slots, CPU, memory and provider concurrency.

A run reserves ``min(concurrency, trials) x per-trial resources`` as a whole when it
is claimed and keeps the reservation until its resources are confirmed released, so
a run whose cleanup failed still blocks capacity. The worker is the only claimer, so
checks and reservations happen in one write transaction against current totals.
"""

import os
from dataclasses import dataclass

from sqlalchemy import Connection, delete, or_, select

from maestro.core import clock
from maestro.core.errors import FieldError, MaestroError
from maestro.db import schema as t
from maestro.domain.states import CleanupState, PatPermission
from maestro.execution.materials import DEFAULT_CPUS, DEFAULT_MEMORY_MB
from maestro.services import audit
from maestro.services.principal import Principal, require_admin

PROVIDER_MAX = 1000


@dataclass(frozen=True)
class HostCapacity:
    slots: int
    cpus: float
    memory_mb: int

    @classmethod
    def detect(cls, slots: int, cpus: float = 0, memory_mb: int = 0) -> "HostCapacity":
        """Zero means the whole host; deployments set lower totals in ``config.toml``."""
        if not cpus:
            cpus = float(os.cpu_count() or 1)
        if not memory_mb:
            memory_mb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1 << 20)
        return cls(slots=max(1, slots), cpus=cpus, memory_mb=memory_mb)

    def public(self) -> dict:
        return {"slots": self.slots, "cpus": self.cpus, "memory_mb": self.memory_mb}


def provider_of(effective: dict) -> str | None:
    model = effective.get("model") or {}
    return model.get("provider")


def reservation(
    concurrency: int, definitions: list[dict], provider: str | None, trial_count: int
) -> dict:
    width = max(1, min(concurrency, trial_count))
    resources = [d.get("resources") or {} for d in definitions] or [{}]
    cpus = max(float(r.get("cpus") or DEFAULT_CPUS) for r in resources)
    memory = max(int(r.get("memory_mb") or DEFAULT_MEMORY_MB) for r in resources)
    return {
        "slots": width,
        "cpus": width * cpus,
        "memory_mb": width * memory,
        "provider": provider,
    }


def _holding():
    """Runs whose reservation still counts: claimed, or resources not confirmed released."""
    return select(t.runs.c.reservation_json).where(
        t.runs.c.reservation_json.is_not(None),
        or_(
            t.runs.c.lease_token.is_not(None),
            t.runs.c.cleanup_state.in_(
                [CleanupState.PENDING, CleanupState.RUNNING, CleanupState.FAILED]
            ),
        ),
    )


def in_use(conn: Connection) -> dict:
    used = {"slots": 0, "cpus": 0.0, "memory_mb": 0, "providers": {}}
    for (held,) in conn.execute(_holding()):
        if not held:
            continue
        used["slots"] += held["slots"]
        used["cpus"] += held["cpus"]
        used["memory_mb"] += held["memory_mb"]
        if held.get("provider"):
            providers = used["providers"]
            providers[held["provider"]] = providers.get(held["provider"], 0) + held["slots"]
    return used


def provider_limits(conn: Connection) -> dict[str, int]:
    return dict(
        conn.execute(
            select(t.provider_limits.c.provider, t.provider_limits.c.max_concurrent_trials)
        ).all()
    )


def never_fits(conn: Connection, capacity: HostCapacity, need: dict) -> str | None:
    """Reasons a run could not start even on an idle host: refuse it at acceptance."""
    if need["slots"] > capacity.slots:
        return "CAPACITY_SLOTS"
    if need["cpus"] > capacity.cpus:
        return "CAPACITY_CPU"
    if need["memory_mb"] > capacity.memory_mb:
        return "CAPACITY_MEMORY"
    limit = provider_limits(conn).get(need["provider"]) if need["provider"] else None
    if limit is not None and need["slots"] > limit:
        return "PROVIDER_LIMIT"
    return None


def waiting_reason(conn: Connection, capacity: HostCapacity, need: dict) -> str | None:
    used = in_use(conn)
    if used["slots"] + need["slots"] > capacity.slots:
        return "CAPACITY_SLOTS"
    if used["cpus"] + need["cpus"] > capacity.cpus + 1e-9:
        return "CAPACITY_CPU"
    if used["memory_mb"] + need["memory_mb"] > capacity.memory_mb:
        return "CAPACITY_MEMORY"
    if need["provider"]:
        limit = provider_limits(conn).get(need["provider"])
        held = used["providers"].get(need["provider"], 0)
        if limit is not None and held + need["slots"] > limit:
            return "PROVIDER_LIMIT"
    return None


def refuse_if_never_fits(conn: Connection, capacity: HostCapacity, need: dict) -> None:
    reason = never_fits(conn, capacity, need)
    if reason:
        raise MaestroError(
            "RUN_EXCEEDS_CAPACITY",
            "This run can never fit the configured execution capacity",
            422,
            stage="preflight",
            field_errors=[FieldError("/concurrency", reason)],
        )


class Capacity:
    """Admin view and provider limits; host totals come from deployment configuration."""

    def __init__(self, db, host: HostCapacity):
        self.db = db
        self.host = host

    def overview(self, principal: Principal) -> dict:
        require_admin(principal, PatPermission.OPERATIONS)
        with self.db.read() as conn:
            used = in_use(conn)
            limits = provider_limits(conn)
        providers = sorted(set(limits) | set(used["providers"]))
        return {
            "host": self.host.public(),
            "in_use": {k: used[k] for k in ("slots", "cpus", "memory_mb")},
            "providers": [
                {
                    "provider": p,
                    "max_concurrent_trials": limits.get(p),
                    "in_use": used["providers"].get(p, 0),
                }
                for p in providers
            ],
        }

    def set_provider_limit(self, principal: Principal, provider: str, limit: int | None) -> dict:
        require_admin(principal, PatPermission.OPERATIONS)
        if not provider or len(provider) > 64:
            raise MaestroError("CONFIG_VALIDATION_FAILED", "Invalid provider name", 422)
        if limit is not None and not 1 <= limit <= PROVIDER_MAX:
            raise MaestroError(
                "CONFIG_VALIDATION_FAILED",
                f"Limit must be between 1 and {PROVIDER_MAX}",
                422,
                field_errors=[FieldError("/max_concurrent_trials", "RANGE")],
            )
        with self.db.write() as conn:
            conn.execute(delete(t.provider_limits).where(t.provider_limits.c.provider == provider))
            if limit is not None:
                conn.execute(
                    t.provider_limits.insert().values(
                        provider=provider,
                        max_concurrent_trials=limit,
                        updated_by=principal.user_id,
                        updated_at=clock.to_text(clock.now()),
                    )
                )
            audit.record(
                conn,
                principal,
                "capacity.provider_limit",
                object_type="provider",
                object_id=None,
                detail={"provider": provider, "max_concurrent_trials": limit},
            )
        return self.overview(principal)
