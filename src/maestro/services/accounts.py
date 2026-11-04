"""Accounts, one-time setup, invitations, web sessions and authentication.

Only admin and member roles exist. The first administrator is created through a
one-time setup token issued by ``maestro init``; afterwards the setup entry stays
closed because an administrator exists, regardless of restarts.
"""

import os
import re
import threading
import time
from collections import deque
from pathlib import Path

from sqlalchemy import Connection, func, insert, select, update

from maestro.core import clock
from maestro.core.errors import FieldError, MaestroError, not_found
from maestro.core.ids import new_id
from maestro.db import schema as t
from maestro.db.engine import Database
from maestro.domain import limits
from maestro.domain.states import PatPermission, Role, UserStatus
from maestro.security import passwords, tokens
from maestro.services import audit, deleted, policy
from maestro.services.principal import Channel, Principal, require_admin_session

USERNAME = re.compile(r"[a-z][a-z0-9_.-]{1,31}")
_TOUCH_SECONDS = 60


def check_username(value: object) -> str:
    if not isinstance(value, str) or not USERNAME.fullmatch(value):
        raise MaestroError(
            "USERNAME_INVALID",
            "Username must be 2-32 lowercase letters, digits, '.', '_' or '-'",
            422,
            field_errors=[FieldError("/username", "PATTERN")],
        )
    return value


def check_display_name(value: object, fallback: str) -> str:
    if value is None or value == "":
        return fallback
    if not isinstance(value, str) or len(value) > 128 or not value.isprintable():
        raise MaestroError(
            "DISPLAY_NAME_INVALID",
            "Display name must be up to 128 printable characters",
            422,
            field_errors=[FieldError("/display_name", "INVALID")],
        )
    return value.strip() or fallback


def public_user(row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "status": row["status"],
        "revision": row["revision"],
        "created_at": row["created_at"],
        "disabled_at": row["disabled_at"],
    }


class RateLimiter:
    """In-process sliding windows per key: failed logins and password changes, and every
    invitation or setup attempt. Keys come from callers, so memory is bounded: a key with
    no event in the last minute is forgotten and only ``max_keys`` are ever kept."""

    def __init__(self, per_minute: int, max_keys: int = 4096):
        self.per_minute = per_minute
        self.max_keys = max_keys
        self.hits: dict[str, deque] = {}  # least recently recorded key first
        self.lock = threading.Lock()

    def _recent(self, key: str, now: float) -> deque:
        while self.hits:  # keys are ordered by their newest event: expired ones lead
            oldest = next(iter(self.hits))
            if now - self.hits[oldest][-1] <= 60:
                break
            del self.hits[oldest]
        window = self.hits.get(key)
        if window is None:
            return deque()
        while window and now - window[0] > 60:
            window.popleft()
        return window

    def _refuse_if_full(self, key: str, limit: int, now: float) -> deque:
        window = self._recent(key, now)
        if len(window) >= limit:
            raise MaestroError(
                "RATE_LIMITED",
                "Too many attempts; wait a minute",
                429,
                retryable=True,
                headers={"Retry-After": "60"},
            )
        return window

    def _add(self, key: str, window: deque, now: float) -> None:
        self.hits.pop(key, None)
        if len(self.hits) >= self.max_keys:
            del self.hits[next(iter(self.hits))]
        window.append(now)
        self.hits[key] = window

    def check(self, key: str, limit: int | None = None) -> None:
        """Refuse when the key already has ``limit`` events; records nothing."""
        with self.lock:
            self._refuse_if_full(key, limit or self.per_minute, time.monotonic())

    def record(self, key: str) -> None:
        with self.lock:
            now = time.monotonic()
            self._add(key, self._recent(key, now), now)

    def hit(self, key: str) -> None:
        """Count this attempt, whatever its outcome."""
        with self.lock:
            now = time.monotonic()
            self._add(key, self._refuse_if_full(key, self.per_minute, now), now)


# Failed logins one address may cause in a minute, as a multiple of the per-account limit:
# several colleagues behind one NAT address must not lock each other out.
SOURCE_FAILURE_FACTOR = 5


class Accounts:
    def __init__(self, db: Database, setup_token_file: Path, session_hours: int):
        self.db = db
        self.setup_token_file = setup_token_file
        self.session_hours = session_hours
        self.limiter = RateLimiter(limits.LOGIN_ATTEMPTS_PER_MINUTE)
        self.policies = policy.Policies(db)

    # --- setup -------------------------------------------------------------

    def needs_setup(self) -> bool:
        with self.db.read() as conn:
            return not conn.execute(
                select(func.count()).select_from(t.users).where(t.users.c.role == Role.ADMIN)
            ).scalar()

    def issue_setup_token(self) -> str | None:
        """Called by ``maestro init``. Returns None once an administrator exists."""
        if not self.needs_setup():
            return None
        token = tokens.generate("setup")
        path = self.setup_token_file
        temporary = path.with_name(path.name + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(tokens.digest("setup", token) + "\n")
        os.replace(temporary, path)
        return token

    def _setup_digest(self) -> str | None:
        try:
            return self.setup_token_file.read_text().strip() or None
        except OSError:
            return None

    def initialize_admin(
        self, setup_token: str, username: str, display_name: str | None, password: str, source: str
    ) -> tuple[dict, str]:
        self.limiter.hit(f"setup:{source}")
        username = check_username(username)
        display_name = check_display_name(display_name, username)
        password_hash = passwords.hash_password(password)
        expected = self._setup_digest()
        if (
            expected is None
            or not tokens.well_formed("setup", setup_token)
            or not tokens.same(expected, tokens.digest("setup", setup_token))
        ):
            raise MaestroError("SETUP_TOKEN_INVALID", "Setup token is invalid or used", 403)
        with self.db.write() as conn:
            admins = conn.execute(
                select(func.count()).select_from(t.users).where(t.users.c.role == Role.ADMIN)
            ).scalar()
            if admins:
                raise MaestroError("SETUP_CLOSED", "Instance is already initialized", 409)
            user_id = self._insert_user(
                conn, username, display_name, Role.ADMIN, password_hash, None
            )
            audit.record(
                conn,
                None,
                "setup.initialize_admin",
                object_type="user",
                object_id=user_id,
                actor_user_id=user_id,
            )
            user = self._user(conn, user_id)
            session_token = self._open_session(conn, user_id)
        self.setup_token_file.unlink(missing_ok=True)
        return public_user(user), session_token

    # --- users -------------------------------------------------------------

    @staticmethod
    def _user(conn: Connection, user_id: str):
        row = conn.execute(select(t.users).where(t.users.c.id == user_id)).mappings().first()
        if row is None:
            raise not_found("User")
        return row

    @staticmethod
    def _refuse_deleted(conn: Connection, user_id: str) -> None:
        if deleted.state(conn, "user", user_id):
            raise MaestroError(
                "USER_DELETED", "Account was deleted; restore it from the recycle bin", 409
            )

    @staticmethod
    def _insert_user(conn, username, display_name, role, password_hash, created_by) -> str:
        if conn.execute(select(t.users.c.id).where(t.users.c.username == username)).first():
            raise MaestroError(
                "USERNAME_TAKEN",
                "Username already exists",
                409,
                field_errors=[FieldError("/username", "TAKEN")],
            )
        user_id = new_id("usr")
        at = clock.to_text(clock.now())
        conn.execute(
            insert(t.users).values(
                id=user_id,
                username=username,
                display_name=display_name,
                role=role,
                status=UserStatus.ACTIVE,
                password_hash=password_hash,
                revision=1,
                created_by=created_by,
                created_at=at,
                updated_at=at,
            )
        )
        return user_id

    def list_members(self, principal: Principal) -> list[dict]:
        require_admin_session(principal)
        with self.db.read() as conn:
            rows = (
                conn.execute(
                    select(t.users)
                    .where(deleted.visible(t.users.c.id, "user"))
                    .order_by(t.users.c.created_at)
                )
                .mappings()
                .all()
            )
        return [public_user(r) for r in rows]

    @staticmethod
    def _refuse_stale_admin(conn: Connection, principal: Principal) -> None:
        """The principal was read before this write lock: the acting account may have been
        disabled or demoted since, and must not still be able to act (or lock out the
        others, so that no active administrator remains)."""
        row = conn.execute(
            select(t.users.c.role, t.users.c.status).where(t.users.c.id == principal.user_id)
        ).first()
        if row is None or row.status != UserStatus.ACTIVE:
            raise MaestroError("UNAUTHENTICATED", "Session expired; sign in again", 401)
        if row.role != Role.ADMIN:
            raise MaestroError("FORBIDDEN", "Administrator role required", 403)

    def set_status(
        self, principal: Principal, user_id: str, status: UserStatus, expected_revision: int
    ) -> dict:
        require_admin_session(principal)
        with self.db.write() as conn:
            self._refuse_stale_admin(conn, principal)
            user = self._user(conn, user_id)
            self._refuse_deleted(conn, user_id)
            if user["revision"] != expected_revision:
                raise MaestroError("REVISION_CONFLICT", "Account changed; refresh", 409)
            if status == user["status"]:
                return public_user(user)
            if status == UserStatus.DISABLED:
                if user_id == principal.user_id:
                    raise MaestroError("CANNOT_DISABLE_SELF", "You cannot disable yourself", 409)
                at = clock.to_text(clock.now())
                # Disabling stops later calls immediately: revoke sessions now; PAT
                # checks also require an active owner.
                conn.execute(
                    update(t.sessions)
                    .where(t.sessions.c.user_id == user_id, t.sessions.c.revoked_at.is_(None))
                    .values(revoked_at=at)
                )
            else:
                at = None
            conn.execute(
                update(t.users)
                .where(t.users.c.id == user_id)
                .values(
                    status=status,
                    disabled_at=at,
                    revision=user["revision"] + 1,
                    updated_at=clock.to_text(clock.now()),
                )
            )
            audit.record(
                conn,
                principal,
                f"user.{'disable' if at else 'enable'}",
                object_type="user",
                object_id=user_id,
            )
            return public_user(self._user(conn, user_id))

    def change_password(self, principal: Principal, current: str, new: str) -> None:
        if principal.channel != Channel.WEB_SESSION:
            raise MaestroError("WEB_SESSION_REQUIRED", "This action requires a web session", 403)
        # A stolen session must not be able to guess the current password at will.
        attempts = f"password:{principal.user_id}"
        self.limiter.check(attempts)
        new_hash = passwords.hash_password(new)
        with self.db.read() as conn:
            stored = self._user(conn, principal.user_id)["password_hash"]
        if not passwords.verify(stored, current):
            self.limiter.record(attempts)
            raise MaestroError(
                "PASSWORD_INCORRECT",
                "Current password is incorrect",
                422,
                field_errors=[FieldError("/current_password", "INCORRECT")],
            )
        with self.db.write() as conn:
            user = self._user(conn, principal.user_id)
            at = clock.to_text(clock.now())
            conn.execute(
                update(t.users)
                .where(t.users.c.id == user["id"])
                .values(password_hash=new_hash, revision=user["revision"] + 1, updated_at=at)
            )
            # Other sessions end; the current one stays usable.
            conn.execute(
                update(t.sessions)
                .where(
                    t.sessions.c.user_id == user["id"],
                    t.sessions.c.id != principal.session_id,
                    t.sessions.c.revoked_at.is_(None),
                )
                .values(revoked_at=at)
            )
            audit.record(
                conn, principal, "user.change_password", object_type="user", object_id=user["id"]
            )

    # --- invitations ---------------------------------------------------------

    def create_invitation(
        self,
        principal: Principal,
        role: Role,
        note: str,
        hours: int | None = None,
        target_user_id: str | None = None,
    ) -> dict:
        require_admin_session(principal)
        hours = hours or self.policies.current().invitation_hours
        if not 1 <= hours <= 24 * 30:
            raise MaestroError("INVITATION_TTL_INVALID", "Validity must be 1-720 hours", 422)
        if not isinstance(note, str) or len(note) > 256:
            raise MaestroError("NOTE_INVALID", "Note must be at most 256 characters", 422)
        token = tokens.generate("invitation")
        now = clock.now()
        inv_id = new_id("inv")
        with self.db.write() as conn:
            self._refuse_stale_admin(conn, principal)
            if target_user_id is not None:
                target = self._user(conn, target_user_id)
                self._refuse_deleted(conn, target_user_id)
                role = Role(target["role"])
            conn.execute(
                insert(t.invitations).values(
                    id=inv_id,
                    token_hash=tokens.digest("invitation", token),
                    role=role,
                    note=note,
                    target_user_id=target_user_id,
                    created_by=principal.user_id,
                    created_at=clock.to_text(now),
                    expires_at=clock.to_text(now + clock.timedelta(hours=hours)),
                )
            )
            audit.record(
                conn,
                principal,
                "invitation.create" if target_user_id is None else "user.issue_password_reset",
                object_type="invitation",
                object_id=inv_id,
                detail={"role": role.value, "target_user_id": target_user_id},
            )
            row = conn.execute(select(t.invitations).where(t.invitations.c.id == inv_id))
            return {**self._public_invitation(row.mappings().one()), "token": token}

    @staticmethod
    def _public_invitation(row) -> dict:
        now = clock.to_text(clock.now())
        if row["used_at"]:
            state = "used"
        elif row["revoked_at"]:
            state = "revoked"
        elif row["expires_at"] <= now:
            state = "expired"
        else:
            state = "pending"
        return {
            "id": row["id"],
            "role": row["role"],
            "note": row["note"],
            "kind": "password_reset" if row["target_user_id"] else "invitation",
            "target_user_id": row["target_user_id"],
            "state": state,
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "used_at": row["used_at"],
            "used_by": row["used_by"],
        }

    def list_invitations(self, principal: Principal) -> list[dict]:
        require_admin_session(principal)
        with self.db.read() as conn:
            rows = conn.execute(select(t.invitations).order_by(t.invitations.c.created_at.desc()))
            return [self._public_invitation(r) for r in rows.mappings().all()]

    def revoke_invitation(self, principal: Principal, inv_id: str) -> dict:
        require_admin_session(principal)
        with self.db.write() as conn:
            self._refuse_stale_admin(conn, principal)
            row = conn.execute(select(t.invitations).where(t.invitations.c.id == inv_id))
            row = row.mappings().first()
            if row is None:
                raise not_found("Invitation")
            if not row["used_at"] and not row["revoked_at"]:
                conn.execute(
                    update(t.invitations)
                    .where(t.invitations.c.id == inv_id)
                    .values(revoked_at=clock.to_text(clock.now()))
                )
                audit.record(
                    conn, principal, "invitation.revoke", object_type="invitation", object_id=inv_id
                )
            row = conn.execute(select(t.invitations).where(t.invitations.c.id == inv_id))
            return self._public_invitation(row.mappings().one())

    def _pending_invitation(self, conn: Connection, token: str):
        row = None
        if tokens.well_formed("invitation", token):
            row = (
                conn.execute(
                    select(t.invitations).where(
                        t.invitations.c.token_hash == tokens.digest("invitation", token)
                    )
                )
                .mappings()
                .first()
            )
        if row is None or self._public_invitation(row)["state"] != "pending":
            raise MaestroError("INVITATION_INVALID", "Invitation is invalid, used or expired", 410)
        # The link carries its issuer's authority: once that administrator is disabled or
        # demoted, nothing they issued may still create accounts or reset passwords.
        issuer = conn.execute(
            select(t.users.c.role, t.users.c.status).where(t.users.c.id == row["created_by"])
        ).first()
        if issuer is None or issuer.status != UserStatus.ACTIVE or issuer.role != Role.ADMIN:
            raise MaestroError("INVITATION_INVALID", "Invitation is invalid, used or expired", 410)
        return row

    def inspect_invitation(self, token: str, source: str) -> dict:
        self.limiter.hit(f"invite:{source}")
        with self.db.read() as conn:
            row = self._pending_invitation(conn, token)
            username = None
            if row["target_user_id"]:
                username = self._user(conn, row["target_user_id"])["username"]
            return {
                "kind": "password_reset" if row["target_user_id"] else "invitation",
                "role": row["role"],
                "username": username,
                "expires_at": row["expires_at"],
            }

    def accept_invitation(
        self,
        token: str,
        username: str | None,
        display_name: str | None,
        password: str,
        source: str,
    ) -> tuple[dict, str]:
        self.limiter.hit(f"invite:{source}")
        password_hash = passwords.hash_password(password)
        with self.db.write() as conn:
            row = self._pending_invitation(conn, token)
            at = clock.to_text(clock.now())
            if row["target_user_id"]:
                user = self._user(conn, row["target_user_id"])
                if user["status"] != UserStatus.ACTIVE:
                    raise MaestroError("ACCOUNT_DISABLED", "Account is disabled", 403)
                user_id = user["id"]
                conn.execute(
                    update(t.users)
                    .where(t.users.c.id == user_id)
                    .values(
                        password_hash=password_hash, revision=user["revision"] + 1, updated_at=at
                    )
                )
                conn.execute(
                    update(t.sessions)
                    .where(t.sessions.c.user_id == user_id, t.sessions.c.revoked_at.is_(None))
                    .values(revoked_at=at)
                )
                # A reset answers a lost or leaked password: tokens issued under the old
                # credentials end with the sessions (a self-service change keeps them).
                detail = {
                    "revoked_pats": conn.execute(
                        update(t.pats)
                        .where(t.pats.c.owner_id == user_id, t.pats.c.revoked_at.is_(None))
                        .values(revoked_at=at, revision=t.pats.c.revision + 1)
                    ).rowcount
                }
                action = "user.reset_password"
            else:
                detail = None
                username = check_username(username)
                user_id = self._insert_user(
                    conn,
                    username,
                    check_display_name(display_name, username),
                    Role(row["role"]),
                    password_hash,
                    row["created_by"],
                )
                action = "invitation.accept"
            conn.execute(
                update(t.invitations)
                .where(t.invitations.c.id == row["id"])
                .values(used_at=at, used_by=user_id)
            )
            audit.record(
                conn,
                None,
                action,
                object_type="invitation",
                object_id=row["id"],
                detail=detail,
                actor_user_id=user_id,
            )
            session_token = self._open_session(conn, user_id)
            return public_user(self._user(conn, user_id)), session_token

    # --- sessions ------------------------------------------------------------

    def _open_session(self, conn: Connection, user_id: str) -> str:
        token = tokens.generate("session")
        now = clock.now()
        conn.execute(
            insert(t.sessions).values(
                id=new_id("ses"),
                token_hash=tokens.digest("session", token),
                user_id=user_id,
                created_at=clock.to_text(now),
                expires_at=clock.to_text(now + clock.timedelta(hours=self.session_hours)),
                last_seen_at=clock.to_text(now),
            )
        )
        return token

    def login(self, username: str, password: str, source: str) -> tuple[dict, str]:
        # Only failures count. The account bucket includes the address, so a stranger
        # guessing from elsewhere cannot lock the real owner out.
        by_source, by_account = f"login:{source}", f"login-user:{username}@{source}"
        # The team policy sets the limit; a change applies from the next attempt.
        self.limiter.per_minute = self.policies.current().login_attempts_per_minute
        self.limiter.check(by_source, self.limiter.per_minute * SOURCE_FAILURE_FACTOR)
        self.limiter.check(by_account)
        with self.db.read() as conn:
            row = (
                conn.execute(select(t.users).where(t.users.c.username == username))
                .mappings()
                .first()
            )
        ok = passwords.verify(row["password_hash"] if row else None, password)
        if not ok or row["status"] != UserStatus.ACTIVE:
            self.limiter.record(by_source)
            self.limiter.record(by_account)
            with self.db.write() as conn:
                audit.record(
                    conn,
                    None,
                    "session.login",
                    outcome="failed",
                    object_type="user",
                    object_id=row["id"] if row else None,
                    actor_user_id=row["id"] if row else None,
                    detail={"reason": "disabled" if ok else "bad_credentials"},
                )
            raise MaestroError("LOGIN_FAILED", "Incorrect username or password", 401)
        with self.db.write() as conn:
            if passwords.needs_rehash(row["password_hash"]):
                conn.execute(
                    update(t.users)
                    .where(t.users.c.id == row["id"])
                    .values(password_hash=passwords.hash_password(password))
                )
            token = self._open_session(conn, row["id"])
            audit.record(
                conn,
                None,
                "session.login",
                object_type="user",
                object_id=row["id"],
                actor_user_id=row["id"],
            )
        return public_user(row), token

    def logout(self, principal: Principal) -> None:
        if principal.session_id is None:
            return
        with self.db.write() as conn:
            conn.execute(
                update(t.sessions)
                .where(t.sessions.c.id == principal.session_id)
                .values(revoked_at=clock.to_text(clock.now()))
            )

    # --- authentication ------------------------------------------------------

    def authenticate_session(self, token: str) -> Principal:
        if not tokens.well_formed("session", token):
            raise MaestroError("UNAUTHENTICATED", "Sign in required", 401)
        now = clock.to_text(clock.now())
        with self.db.read() as conn:
            row = (
                conn.execute(
                    select(t.sessions, t.users.c.username, t.users.c.role, t.users.c.status)
                    .join(t.users, t.users.c.id == t.sessions.c.user_id)
                    .where(t.sessions.c.token_hash == tokens.digest("session", token))
                )
                .mappings()
                .first()
            )
        if (
            row is None
            or row["revoked_at"]
            or row["expires_at"] <= now
            or row["status"] != UserStatus.ACTIVE
        ):
            raise MaestroError("UNAUTHENTICATED", "Session expired; sign in again", 401)
        if self._stale(row["last_seen_at"]):
            with self.db.write() as conn:
                conn.execute(
                    update(t.sessions).where(t.sessions.c.id == row["id"]).values(last_seen_at=now)
                )
        return Principal(
            user_id=row["user_id"],
            username=row["username"],
            role=Role(row["role"]),
            channel=Channel.WEB_SESSION,
            session_id=row["id"],
        )

    def authenticate_pat(self, token: str) -> Principal:
        if not tokens.well_formed("pat", token):
            raise MaestroError("UNAUTHENTICATED", "Invalid access token", 401)
        now = clock.to_text(clock.now())
        with self.db.read() as conn:
            row = (
                conn.execute(
                    select(t.pats, t.users.c.username, t.users.c.role, t.users.c.status)
                    .join(t.users, t.users.c.id == t.pats.c.owner_id)
                    .where(t.pats.c.token_hash == tokens.digest("pat", token))
                )
                .mappings()
                .first()
            )
        if row is None or row["status"] != UserStatus.ACTIVE:
            raise MaestroError("UNAUTHENTICATED", "Invalid access token", 401)
        if row["revoked_at"]:
            raise MaestroError("PAT_REVOKED", "Access token was revoked", 401)
        if row["expires_at"] <= now:
            raise MaestroError("PAT_EXPIRED", "Access token expired", 401)
        if self._stale(row["last_used_at"]):
            with self.db.write() as conn:
                conn.execute(
                    update(t.pats).where(t.pats.c.id == row["id"]).values(last_used_at=now)
                )
        return Principal(
            user_id=row["owner_id"],
            username=row["username"],
            role=Role(row["role"]),
            channel=Channel.PAT,
            pat_id=row["id"],
            pat_permissions=frozenset(PatPermission(x) for x in row["permissions_json"]),
        )

    @staticmethod
    def _stale(last: str | None) -> bool:
        if last is None:
            return True
        return (clock.now() - clock.from_text(last)).total_seconds() > _TOUCH_SECONDS

    def me(self, principal: Principal) -> dict:
        with self.db.read() as conn:
            user = public_user(self._user(conn, principal.user_id))
        return {
            **user,
            "auth_channel": principal.channel.value,
            "pat_id": principal.pat_id,
            "pat_permissions": (
                sorted(principal.pat_permissions) if principal.channel == Channel.PAT else None
            ),
        }
