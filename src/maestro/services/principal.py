"""Authenticated principals and role, channel and ownership permissions.

Effective permission = user role ∩ PAT scope ∩ allowed action ∩ ownership.
Administrative actions also require ``auth_channel == web_session``.
"""

from dataclasses import dataclass
from enum import StrEnum

from maestro.core.errors import MaestroError
from maestro.domain.states import PatScope, Role


class Channel(StrEnum):
    WEB_SESSION = "web_session"
    PAT = "pat"
    SYSTEM = "system"


@dataclass(frozen=True)
class Principal:
    user_id: str
    username: str
    role: Role
    channel: Channel
    session_id: str | None = None
    pat_id: str | None = None
    pat_scope: PatScope | None = None

    @property
    def is_admin_session(self) -> bool:
        return self.role == Role.ADMIN and self.channel == Channel.WEB_SESSION

    @property
    def idempotency_principal(self) -> str:
        return self.user_id


def require_admin_session(p: Principal) -> None:
    if p.channel != Channel.WEB_SESSION:
        raise MaestroError(
            "ADMIN_SESSION_REQUIRED", "Administrative actions require a web session", 403
        )
    if p.role != Role.ADMIN:
        raise MaestroError("FORBIDDEN", "Administrator role required", 403)


def require_web_session(p: Principal) -> None:
    """Own-account actions such as PAT management; a PAT can never manage PATs."""
    if p.channel != Channel.WEB_SESSION:
        raise MaestroError("WEB_SESSION_REQUIRED", "This action requires a web session", 403)


def require_experiment(p: Principal) -> None:
    if p.channel == Channel.PAT and p.pat_scope != PatScope.EXPERIMENT:
        raise MaestroError(
            "PAT_SCOPE_INSUFFICIENT", "A read token cannot perform experiment actions", 403
        )


def can_manage_owned(p: Principal, owner_id: str) -> bool:
    """Own object through web or experiment PAT; admin override only via web session."""
    if p.channel == Channel.PAT and p.pat_scope != PatScope.EXPERIMENT:
        return False
    return owner_id == p.user_id or p.is_admin_session


def require_manage_owned(p: Principal, owner_id: str) -> None:
    require_experiment(p)
    if not can_manage_owned(p, owner_id):
        raise MaestroError("FORBIDDEN", "Only the owner or an administrator may do this", 403)
