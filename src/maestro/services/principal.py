"""Authenticated principals and role, channel, permission and ownership checks.

Effective permission = user role ∩ granted permission ∩ allowed action ∩ ownership.
A web session holds every permission its role allows. A PAT holds only the
permissions it was granted, still capped by its owner's current role. Some actions
(PATs, members, credentials and the master key, permanent deletion) are never
reachable through a PAT: a leaked token must not be able to mint tokens, take over
accounts, move keys or destroy data for good.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from maestro.core.errors import MaestroError
from maestro.domain.states import ADMIN_PERMISSIONS, PatPermission, Role


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
    pat_permissions: frozenset[PatPermission] = field(default_factory=frozenset)

    @property
    def is_admin_session(self) -> bool:
        """Administrator in a web session: the only holder of session-only abilities."""
        return self.role == Role.ADMIN and self.channel == Channel.WEB_SESSION

    def has(self, permission: PatPermission) -> bool:
        if permission in ADMIN_PERMISSIONS and self.role != Role.ADMIN:
            return False
        if self.channel == Channel.WEB_SESSION:
            return True
        if self.channel == Channel.PAT:
            return permission in self.pat_permissions
        # The service acting for an accepted run: experiment work, never administration.
        return permission == PatPermission.EXPERIMENT

    @property
    def can_override(self) -> bool:
        """May act on other members' objects (cancel, bin, see their exports)."""
        return self.has(PatPermission.OVERRIDE)

    @property
    def idempotency_principal(self) -> str:
        return self.user_id


def require_admin_session(p: Principal) -> None:
    """Session-only administration: no PAT reaches it, whatever it was granted."""
    if p.channel != Channel.WEB_SESSION:
        raise MaestroError(
            "ADMIN_SESSION_REQUIRED", "This action requires an administrator web session", 403
        )
    if p.role != Role.ADMIN:
        raise MaestroError("FORBIDDEN", "Administrator role required", 403)


def require_admin(p: Principal, permission: PatPermission) -> None:
    """Administration a PAT may do when it was granted ``permission``."""
    if p.role != Role.ADMIN:
        raise MaestroError("FORBIDDEN", "Administrator role required", 403)
    if not p.has(permission):
        raise MaestroError(
            "PAT_PERMISSION_INSUFFICIENT",
            f"This access token lacks the '{permission.value}' permission",
            403,
        )


def require_web_session(p: Principal) -> None:
    """Own-account actions such as PAT management; a PAT can never manage PATs."""
    if p.channel != Channel.WEB_SESSION:
        raise MaestroError("WEB_SESSION_REQUIRED", "This action requires a web session", 403)


def require_experiment(p: Principal) -> None:
    if not p.has(PatPermission.EXPERIMENT):
        raise MaestroError(
            "PAT_PERMISSION_INSUFFICIENT",
            "This access token lacks the 'experiment' permission",
            403,
        )


def require_content_upload(p: Principal) -> None:
    """Shared file transport serves attachment publishers and dataset administrators."""
    if p.has(PatPermission.DATASETS):
        return
    require_experiment(p)


def can_manage_owned(p: Principal, owner_id: str) -> bool:
    """Own objects need the experiment permission; others' objects need override."""
    if owner_id == p.user_id:
        return p.has(PatPermission.EXPERIMENT)
    return p.can_override


def require_manage_owned(p: Principal, owner_id: str) -> None:
    if owner_id == p.user_id:
        require_experiment(p)
    elif not p.can_override:
        raise MaestroError("FORBIDDEN", "Only the owner or an administrator may do this", 403)
