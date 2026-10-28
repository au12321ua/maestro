"""Two-step deletion and the recycle bin (07 section 8, U-08 / U-19).

Deleting puts an object in the recycle bin: it leaves lists and selectors, cannot
be newly referenced, and existing references still resolve and show "deleted".
Only administrators see the bin, restore from it, or purge. Purging removes the
object's content and files for good; its row stays as the deletion receipt so
references read "permanently deleted" instead of dangling. The master key is the
one thing that cannot be deleted. The bin never empties itself.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from sqlalchemy import Connection, delete, func, insert, or_, select, update

from maestro.boundaries.environment import PreparedOutput
from maestro.core import clock
from maestro.core.errors import MaestroError, not_found
from maestro.core.ids import new_id
from maestro.db import schema as t
from maestro.db.engine import Database
from maestro.domain.states import RUN_TERMINAL, Role, UserStatus
from maestro.services import audit, deleted, pagination
from maestro.services.collections import drop_purged_run
from maestro.services.principal import (
    Principal,
    require_admin_session,
    require_manage_owned,
    require_web_session,
)

log = logging.getLogger(__name__)
FAILED_KEYS_SHOWN = 20


@dataclass
class Purged:
    """What a purge removed, and work to do once its transaction committed. Each step
    does as much as it can and returns what it could not do (merged into the receipt's
    detail), or nothing when all went well."""

    detail: dict = field(default_factory=dict)
    after_commit: list[Callable[[], dict | None]] = field(default_factory=list)


def _active_runs(conn: Connection, *where) -> list[str]:
    return list(
        conn.execute(
            select(t.runs.c.id).where(
                t.runs.c.state.not_in([s.value for s in RUN_TERMINAL]), *where
            )
        ).scalars()
    )


def _refuse_active(runs: list[str], code: str, message: str, action: str) -> None:
    if runs:
        raise MaestroError(
            code,
            message,
            409,
            suggested_actions=[{"type": action, "run_ids": runs[:20]}],
        )


class Handler:
    object_type: str
    table = None

    def load(self, conn: Connection, object_id: str):
        row = conn.execute(select(self.table).where(self.table.c.id == object_id)).mappings()
        row = row.first()
        if row is None:
            raise not_found(self.object_type.replace("_", " ").capitalize())
        return row

    def label(self, conn: Connection, row) -> str:
        return row["id"]

    def owner(self, row) -> str | None:
        return None

    def authorize(self, principal: Principal, row) -> None:
        require_admin_session(principal)

    def check_delete(self, conn: Connection, principal: Principal, row) -> None:
        pass

    def on_delete(self, conn: Connection, principal: Principal, row) -> dict:
        return {}

    def check_restore(self, conn: Connection, row) -> None:
        pass

    def on_restore(self, conn: Connection, row, detail: dict) -> None:
        pass

    def purge(self, conn: Connection, row, service: Recycle, actor_id: str) -> Purged:
        return Purged()


class RunHandler(Handler):
    object_type = "run"
    table = t.runs

    def label(self, conn, row):
        return row["name"]

    def owner(self, row):
        return row["owner_id"]

    def authorize(self, principal, row):
        # Members may bin their own runs; the bin itself is administrators' (U-19).
        require_manage_owned(principal, row["owner_id"])

    def check_delete(self, conn, principal, row):
        if row["state"] not in RUN_TERMINAL:
            raise MaestroError(
                "RUN_ACTIVE",
                "Queued or running runs cannot be deleted; cancel it or wait for it to end",
                409,
                suggested_actions=[{"type": "cancel_run", "run_id": row["id"]}],
            )

    def purge(self, conn, row, service, actor_id):
        run_id = row["id"]
        artifacts = conn.execute(
            select(t.artifacts.c.id, t.artifacts.c.storage_key).where(
                t.artifacts.c.run_id == run_id
            )
        ).all()
        ids = [a.id for a in artifacts]
        trials = select(t.trials.c.id).where(t.trials.c.run_id == run_id)
        attempts = select(t.execution_attempts.c.id).where(
            t.execution_attempts.c.trial_id.in_(trials)
        )
        counts = {
            "trials": conn.execute(select(func.count()).select_from(trials.subquery())).scalar(),
            "artifacts": len(ids),
        }
        conn.execute(delete(t.artifact_pins).where(t.artifact_pins.c.artifact_id.in_(ids)))
        conn.execute(delete(t.artifacts).where(t.artifacts.c.run_id == run_id))
        conn.execute(delete(t.preparation_waiters).where(t.preparation_waiters.c.run_id == run_id))
        conn.execute(delete(t.trial_results).where(t.trial_results.c.trial_id.in_(trials)))
        conn.execute(delete(t.execution_attempts).where(t.execution_attempts.c.id.in_(attempts)))
        conn.execute(delete(t.work_items).where(t.work_items.c.trial_id.in_(trials)))
        conn.execute(delete(t.trials).where(t.trials.c.run_id == run_id))
        conn.execute(delete(t.run_snapshots).where(t.run_snapshots.c.run_id == run_id))
        drop_purged_run(conn, run_id, actor_id)
        # Artifacts of this run already in the bin are gone with it.
        service.mark_purged(conn, "artifact", ids, actor_id)
        keys = [a.storage_key for a in artifacts]
        return Purged(
            counts,
            [lambda: service.remove_files(keys), lambda: service.expire_exports({run_id})],
        )


class ArtifactHandler(Handler):
    object_type = "artifact"
    table = t.artifacts

    def label(self, conn, row):
        return row["relative_path"]

    def check_delete(self, conn, principal, row):
        state = conn.execute(select(t.runs.c.state).where(t.runs.c.id == row["run_id"])).scalar()
        if state not in RUN_TERMINAL:
            raise MaestroError(
                "RUN_ACTIVE", "Artifacts of a queued or running run cannot be deleted", 409
            )

    def purge(self, conn, row, service, actor_id):
        conn.execute(delete(t.artifact_pins).where(t.artifact_pins.c.artifact_id == row["id"]))
        conn.execute(
            update(t.artifacts)
            .where(t.artifacts.c.id == row["id"])
            .values(status="deleted", status_reason="DELETED")
        )
        key, run_id = row["storage_key"], row["run_id"]
        return Purged(
            {"size_bytes": row["size_bytes"]},
            [lambda: service.remove_files([key]), lambda: service.expire_exports({run_id})],
        )


def _versions_in_use(conn: Connection, version_ids: list[str]) -> list[str]:
    return _active_runs(conn, t.runs.c.dataset_version_id.in_(version_ids))


class DatasetVersionHandler(Handler):
    object_type = "dataset_version"
    table = t.dataset_versions

    def label(self, conn, row):
        dataset = conn.execute(
            select(t.datasets.c.namespace, t.datasets.c.name).where(
                t.datasets.c.id == row["dataset_id"]
            )
        ).one()
        return f"{dataset.namespace}/{dataset.name}@{row['upstream_ref']}"

    def check_restore(self, conn, row):
        if deleted.state(conn, "dataset", row["dataset_id"]):
            raise MaestroError(
                "PARENT_DELETED", "Restore the dataset first; this version belongs to it", 409
            )

    def purge(self, conn, row, service, actor_id):
        return purge_versions(conn, [row["id"]], service)


def purge_versions(conn: Connection, version_ids: list[str], service: Recycle) -> Purged:
    """Remove task definitions, admissions and task materials no remaining version uses.

    Runs keep their snapshots and trial records; tasks keep their key for them.
    """
    _refuse_active(
        _versions_in_use(conn, version_ids),
        "DATASET_VERSION_IN_USE",
        "Queued or running runs still use this dataset version",
        "cancel_runs",
    )
    tasks = conn.execute(
        select(t.tasks.c.id, t.tasks.c.content_digest, t.tasks.c.spec_digest).where(
            t.tasks.c.dataset_version_id.in_(version_ids)
        )
    ).all()
    remaining = (
        select(t.tasks)
        .where(
            t.tasks.c.dataset_version_id.not_in(version_ids),
            t.tasks.c.dataset_version_id.not_in(
                select(t.deletions.c.object_id).where(
                    t.deletions.c.object_type == "dataset_version",
                    t.deletions.c.state == deleted.PURGED,
                )
            ),
        )
        .subquery()
    )
    still_used = set(conn.execute(select(remaining.c.content_digest)).scalars())
    specs_used = set(conn.execute(select(remaining.c.spec_digest)).scalars())
    task_ids = [x.id for x in tasks]
    conn.execute(delete(t.task_admissions).where(t.task_admissions.c.task_id.in_(task_ids)))
    conn.execute(update(t.tasks).where(t.tasks.c.id.in_(task_ids)).values(definition_json={}))
    conn.execute(
        update(t.dataset_versions)
        .where(t.dataset_versions.c.id.in_(version_ids))
        .values(manifest_json={})
    )
    # Preparation caches, their images and recoverable originals go with the last
    # version that uses the same spec (U-19); shared content stays.
    specs = sorted({x.spec_digest for x in tasks if x.spec_digest} - specs_used)
    preparations = conn.execute(
        select(
            t.preparations.c.id, t.preparations.c.spec_digest, t.preparations.c.output_json
        ).where(t.preparations.c.spec_digest.in_(specs))
    ).all()
    preparation_ids = [p.id for p in preparations]
    conn.execute(
        delete(t.preparation_pins).where(t.preparation_pins.c.preparation_id.in_(preparation_ids))
    )
    conn.execute(delete(t.preparations).where(t.preparations.c.id.in_(preparation_ids)))
    outputs = [(p.spec_digest, p.output_json) for p in preparations if p.output_json]
    released = sorted({x.content_digest for x in tasks} - still_used)
    return Purged(
        {
            "tasks": len(task_ids),
            "released_task_contents": len(released),
            "released_preparations": len(preparation_ids),
        },
        [
            lambda: service.remove_task_materials(released),
            lambda: service.discard_preparations(outputs),
        ],
    )


class DatasetHandler(Handler):
    object_type = "dataset"
    table = t.datasets

    def label(self, conn, row):
        return f"{row['namespace']}/{row['name']}"

    def purge(self, conn, row, service, actor_id):
        versions = list(
            conn.execute(
                select(t.dataset_versions.c.id).where(
                    t.dataset_versions.c.dataset_id == row["id"],
                    t.dataset_versions.c.id.not_in(
                        select(t.deletions.c.object_id).where(
                            t.deletions.c.object_type == "dataset_version",
                            t.deletions.c.state == deleted.PURGED,
                        )
                    ),
                )
            ).scalars()
        )
        result = purge_versions(conn, versions, service) if versions else Purged()
        conn.execute(update(t.datasets).where(t.datasets.c.id == row["id"]).values(description=""))
        service.mark_purged(conn, "dataset_version", versions, actor_id, add_receipts=True)
        result.detail["versions"] = len(versions)
        return result


class CredentialHandler(Handler):
    object_type = "credential"
    table = t.credentials

    def label(self, conn, row):
        return row["name"]

    def check_delete(self, conn, principal, row):
        _refuse_active(
            credential_users(conn, row["id"]),
            "CREDENTIAL_IN_USE",
            "Queued or running runs use this credential; disable it, then cancel them",
            "disable_credential",
        )

    def purge(self, conn, row, service, actor_id):
        # The encrypted value goes; the name is freed for a replacement credential.
        conn.execute(
            update(t.credentials)
            .where(t.credentials.c.id == row["id"])
            .values(
                nonce=b"",
                encrypted_payload=b"",
                enabled=False,
                name=f"deleted-{row['id']}",
                description="",
            )
        )
        return Purged()


def credential_users(conn: Connection, credential_id: str) -> list[str]:
    """Active runs whose snapshot binds this credential. Snapshots list bound
    credentials under provenance ``credential_ids`` (model binding arrives in M5)."""
    rows = conn.execute(
        select(t.runs.c.id, t.run_snapshots.c.provenance_json)
        .join(t.run_snapshots, t.run_snapshots.c.run_id == t.runs.c.id)
        .where(t.runs.c.state.not_in([s.value for s in RUN_TERMINAL]))
    ).all()
    return [
        r.id for r in rows if credential_id in (r.provenance_json or {}).get("credential_ids", [])
    ]


class UserHandler(Handler):
    object_type = "user"
    table = t.users

    def label(self, conn, row):
        return row["username"]

    def owner(self, row):
        return row["id"]

    def check_delete(self, conn, principal, row):
        if row["id"] == principal.user_id:
            raise MaestroError("CANNOT_DELETE_SELF", "You cannot delete your own account", 409)
        if row["role"] == Role.ADMIN:
            others = conn.execute(
                select(func.count()).where(
                    t.users.c.role == Role.ADMIN,
                    t.users.c.status == UserStatus.ACTIVE,
                    t.users.c.id != row["id"],
                    deleted.visible(t.users.c.id, "user"),
                )
            ).scalar()
            if not others:
                raise MaestroError(
                    "LAST_ADMIN", "The last active administrator cannot be deleted", 409
                )
        _refuse_active(
            _active_runs(conn, t.runs.c.owner_id == row["id"]),
            "ACCOUNT_HAS_ACTIVE_RUNS",
            "This account still has queued or running runs; cancel them first",
            "cancel_runs",
        )

    def on_delete(self, conn, principal, row):
        """Sessions and PATs stop working at once (U-19); so do the invitations the
        account issued, which would otherwise create accounts in its name (an admin
        invitation among them) and the ones addressed to it (password resets)."""
        at = clock.to_text(clock.now())
        conn.execute(
            update(t.sessions)
            .where(t.sessions.c.user_id == row["id"], t.sessions.c.revoked_at.is_(None))
            .values(revoked_at=at)
        )
        revoked = conn.execute(
            update(t.pats)
            .where(t.pats.c.owner_id == row["id"], t.pats.c.revoked_at.is_(None))
            .values(revoked_at=at, revision=t.pats.c.revision + 1)
        ).rowcount
        invitations = conn.execute(
            update(t.invitations)
            .where(
                or_(
                    t.invitations.c.target_user_id == row["id"],
                    t.invitations.c.created_by == row["id"],
                ),
                t.invitations.c.used_at.is_(None),
                t.invitations.c.revoked_at.is_(None),
            )
            .values(revoked_at=at)
        ).rowcount
        conn.execute(
            update(t.users)
            .where(t.users.c.id == row["id"])
            .values(
                status=UserStatus.DISABLED,
                disabled_at=row["disabled_at"] or at,
                revision=row["revision"] + 1,
                updated_at=at,
            )
        )
        return {
            "previous_status": row["status"],
            "revoked_pats": revoked,
            "revoked_invitations": invitations,
        }

    def on_restore(self, conn, row, detail):
        # Revoked sessions and PATs stay revoked; the account can sign in again.
        if detail.get("previous_status") == UserStatus.ACTIVE:
            conn.execute(
                update(t.users)
                .where(t.users.c.id == row["id"])
                .values(
                    status=UserStatus.ACTIVE,
                    disabled_at=None,
                    revision=row["revision"] + 1,
                    updated_at=clock.to_text(clock.now()),
                )
            )

    def purge(self, conn, row, service, actor_id):
        # The username stays reserved so history never points at someone else.
        conn.execute(
            update(t.users).where(t.users.c.id == row["id"]).values(password_hash="!purged")
        )
        conn.execute(delete(t.sessions).where(t.sessions.c.user_id == row["id"]))
        return Purged()


class PatHandler(Handler):
    object_type = "pat"
    table = t.pats

    def label(self, conn, row):
        return row["name"]

    def owner(self, row):
        return row["owner_id"]

    def authorize(self, principal, row):
        require_web_session(principal)
        if row["owner_id"] != principal.user_id:
            require_admin_session(principal)

    def on_delete(self, conn, principal, row):
        if row["revoked_at"] is None:
            conn.execute(
                update(t.pats)
                .where(t.pats.c.id == row["id"])
                .values(revoked_at=clock.to_text(clock.now()), revision=row["revision"] + 1)
            )
        return {}

    def purge(self, conn, row, service, actor_id):
        conn.execute(
            update(t.pats).where(t.pats.c.id == row["id"]).values(token_hash=f"purged:{row['id']}")
        )
        return Purged()


class ModelConfigHandler(Handler):
    """Accepted runs keep a copy of what they used; deleting stops new choices."""

    object_type = "model_config"
    table = t.model_configs

    def label(self, conn, row):
        return row["name"]

    def check_delete(self, conn, principal, row):
        _refuse_active(
            _active_runs(conn, t.runs.c.model_config_id == row["id"]),
            "MODEL_CONFIG_IN_USE",
            "Queued or running runs use this model configuration; cancel them or wait",
            "cancel_runs",
        )

    def purge(self, conn, row, service, actor_id):
        # Endpoints and parameters go; the row stays as the receipt runs point at.
        count = conn.execute(
            delete(t.model_revisions).where(t.model_revisions.c.model_config_id == row["id"])
        ).rowcount
        conn.execute(
            update(t.model_configs)
            .where(t.model_configs.c.id == row["id"])
            .values(description="", enabled=False)
        )
        return Purged(detail={"revisions": count})


class HarnessHandler(Handler):
    object_type = "harness_version"
    table = t.harness_versions

    def label(self, conn, row):
        return f"{row['name']} {row['version']}"

    def check_delete(self, conn, principal, row):
        _refuse_active(
            _active_runs(conn, t.runs.c.harness_version_id == row["id"]),
            "HARNESS_IN_USE",
            "Queued or running runs use this harness version; cancel them or wait",
            "cancel_runs",
        )

    def purge(self, conn, row, service, actor_id):
        conn.execute(
            update(t.harness_versions)
            .where(t.harness_versions.c.id == row["id"])
            .values(description="", enabled=False)
        )
        return Purged()


class AttachmentHandler(Handler):
    """Accepted runs keep the version's digest; purging removes the files, so a rerun of
    such a run is refused rather than loading other content."""

    object_type = "attachment_version"
    table = t.attachment_versions

    def label(self, conn, row):
        return f"{row['name']} {row['version']}"

    def owner(self, row):
        return row["owner_id"]

    def authorize(self, principal, row):
        require_manage_owned(principal, row["owner_id"])

    def check_delete(self, conn, principal, row):
        _refuse_active(
            _active_runs(
                conn,
                t.runs.c.id.in_(
                    select(t.run_attachments.c.run_id).where(
                        t.run_attachments.c.attachment_version_id == row["id"]
                    )
                ),
            ),
            "ATTACHMENT_IN_USE",
            "Queued or running runs use this attachment; cancel them or wait",
            "cancel_runs",
        )

    def purge(self, conn, row, service, actor_id):
        conn.execute(
            update(t.attachment_versions)
            .where(t.attachment_versions.c.id == row["id"])
            .values(description="", files_json=[], enabled=False)
        )
        digest = row["content_digest"]
        return Purged(
            {"files": row["file_count"]},
            [lambda: service.remove_attachment_files(digest)],
        )


class PlanHandler(Handler):
    """Runs keep their own values; purging empties the template, the row stays."""

    object_type = "experiment_plan"
    table = t.experiment_plans

    def label(self, conn, row):
        return row["name"]

    def owner(self, row):
        return row["owner_id"]

    def authorize(self, principal, row):
        require_manage_owned(principal, row["owner_id"])

    def purge(self, conn, row, service, actor_id):
        conn.execute(
            update(t.experiment_plans)
            .where(t.experiment_plans.c.id == row["id"])
            .values(template_json={}, description="")
        )
        return Purged()


class CollectionHandler(Handler):
    """Snapshots keep their own copies; purging empties the member list, the row stays."""

    object_type = "collection"
    table = t.collections

    def label(self, conn, row):
        return row["name"]

    def owner(self, row):
        return row["owner_id"]

    def authorize(self, principal, row):
        require_manage_owned(principal, row["owner_id"])

    def purge(self, conn, row, service, actor_id):
        removed = conn.execute(
            delete(t.collection_members).where(t.collection_members.c.collection_id == row["id"])
        ).rowcount
        return Purged({"runs": removed})


class SnapshotHandler(Handler):
    object_type = "analysis_snapshot"
    table = t.analysis_snapshots

    def label(self, conn, row):
        return row["name"]

    def owner(self, row):
        return row["owner_id"]

    def authorize(self, principal, row):
        require_manage_owned(principal, row["owner_id"])

    def purge(self, conn, row, service, actor_id):
        notes = conn.execute(
            delete(t.snapshot_notes).where(t.snapshot_notes.c.snapshot_id == row["id"])
        ).rowcount
        conn.execute(
            update(t.analysis_snapshots)
            .where(t.analysis_snapshots.c.id == row["id"])
            .values(runs_json=[], result_json={}, settings_json={}, spec_json=None)
        )
        return Purged({"notes": notes})


HANDLERS: dict[str, Handler] = {
    h.object_type: h
    for h in (
        RunHandler(),
        ArtifactHandler(),
        DatasetVersionHandler(),
        DatasetHandler(),
        CredentialHandler(),
        UserHandler(),
        PatHandler(),
        ModelConfigHandler(),
        HarnessHandler(),
        AttachmentHandler(),
        PlanHandler(),
        CollectionHandler(),
        SnapshotHandler(),
    )
}


def public_entry(row) -> dict:
    return {
        "id": row["id"],
        "object_type": row["object_type"],
        "object_id": row["object_id"],
        "label": row["label"],
        "owner_id": row["owner_id"],
        "owner_username": row.get("owner_username"),
        "state": row["state"],
        "deleted_by": row["deleted_by"],
        "deleted_by_username": row.get("deleted_by_username"),
        "deleted_at": row["deleted_at"],
        "purged_by": row["purged_by"],
        "purged_at": row["purged_at"],
        "detail": row["detail_json"],
    }


class Recycle:
    def __init__(
        self, db: Database, store, materials=None, exports=None, environment=None, attachments=None
    ):
        self.db = db
        self.store = store
        self.materials = materials
        self.exports = exports
        self.environment = environment
        self.attachments = attachments

    @staticmethod
    def _handler(object_type: str) -> Handler:
        handler = HANDLERS.get(object_type)
        if handler is None:
            raise MaestroError("UNKNOWN_OBJECT_TYPE", f"Cannot delete {object_type!r}", 422)
        return handler

    def _entry(self, conn: Connection, *where):
        owner = t.users.alias("owner")
        actor = t.users.alias("actor")
        return (
            conn.execute(
                select(
                    t.deletions,
                    owner.c.username.label("owner_username"),
                    actor.c.username.label("deleted_by_username"),
                )
                .outerjoin(owner, owner.c.id == t.deletions.c.owner_id)
                .join(actor, actor.c.id == t.deletions.c.deleted_by)
                .where(*where)
            )
            .mappings()
            .first()
        )

    # --- step one ----------------------------------------------------------------

    def delete(self, principal: Principal, object_type: str, object_id: str) -> dict:
        """Idempotent: deleting a deleted object returns its existing entry."""
        handler = self._handler(object_type)
        with self.db.write() as conn:
            row = handler.load(conn, object_id)
            handler.authorize(principal, row)
            existing = self._entry(
                conn,
                t.deletions.c.object_type == object_type,
                t.deletions.c.object_id == object_id,
            )
            if existing:
                return public_entry(existing)
            handler.check_delete(conn, principal, row)
            detail = handler.on_delete(conn, principal, row)
            entry_id = new_id("del")
            conn.execute(
                insert(t.deletions).values(
                    id=entry_id,
                    object_type=object_type,
                    object_id=object_id,
                    label=handler.label(conn, row)[:256],
                    owner_id=handler.owner(row),
                    state=deleted.RECYCLED,
                    deleted_by=principal.user_id,
                    deleted_at=clock.to_text(clock.now()),
                    detail_json=detail,
                )
            )
            audit.record(
                conn,
                principal,
                f"{object_type}.delete",
                object_type=object_type,
                object_id=object_id,
            )
            return public_entry(self._entry(conn, t.deletions.c.id == entry_id))

    # --- the bin (administrators only) ---------------------------------------------

    def list(
        self,
        principal: Principal,
        state: str,
        object_type: str | None,
        cursor: str | None,
        limit: int | None,
    ):
        require_admin_session(principal)
        if state not in {deleted.RECYCLED, deleted.PURGED}:
            raise MaestroError("INVALID_STATE", "state must be recycled or purged", 400)
        limit = pagination.check_limit(limit)
        filters = {"state": state, "object_type": object_type}
        owner = t.users.alias("owner")
        actor = t.users.alias("actor")
        query = (
            select(
                t.deletions,
                owner.c.username.label("owner_username"),
                actor.c.username.label("deleted_by_username"),
            )
            .outerjoin(owner, owner.c.id == t.deletions.c.owner_id)
            .join(actor, actor.c.id == t.deletions.c.deleted_by)
            .where(t.deletions.c.state == state)
        )
        if object_type:
            query = query.where(t.deletions.c.object_type == object_type)
        query = pagination.page(
            query, t.deletions.c.deleted_at, t.deletions.c.id, cursor, limit, filters
        )
        with self.db.read() as conn:
            rows = list(conn.execute(query).mappings().all())
        rows, next_cursor = pagination.split(rows, limit, "deleted_at", filters)
        return [public_entry(r) for r in rows], next_cursor

    def _bin_entry(self, conn: Connection, entry_id: str):
        entry = self._entry(conn, t.deletions.c.id == entry_id)
        if entry is None:
            raise not_found("Recycle bin entry")
        return entry

    def restore(self, principal: Principal, entry_id: str) -> dict:
        require_admin_session(principal)
        with self.db.write() as conn:
            entry = self._bin_entry(conn, entry_id)
            if entry["state"] != deleted.RECYCLED:
                raise MaestroError(
                    "ALREADY_PURGED", "Permanently deleted objects cannot be restored", 409
                )
            handler = self._handler(entry["object_type"])
            row = handler.load(conn, entry["object_id"])
            handler.check_restore(conn, row)
            handler.on_restore(conn, row, entry["detail_json"] or {})
            conn.execute(delete(t.deletions).where(t.deletions.c.id == entry_id))
            audit.record(
                conn,
                principal,
                f"{entry['object_type']}.restore",
                object_type=entry["object_type"],
                object_id=entry["object_id"],
            )
            return {**public_entry(entry), "state": "restored"}

    def purge(self, principal: Principal, entry_id: str, confirm_object_id: str) -> dict:
        """Irreversible; the caller repeats the object id to confirm."""
        require_admin_session(principal)
        with self.db.write() as conn:
            entry = self._bin_entry(conn, entry_id)
            if entry["state"] == deleted.PURGED:
                return public_entry(entry)
            if confirm_object_id != entry["object_id"]:
                raise MaestroError(
                    "PURGE_NOT_CONFIRMED", "Repeat the object id to confirm the purge", 422
                )
            handler = self._handler(entry["object_type"])
            row = handler.load(conn, entry["object_id"])
            purged = handler.purge(conn, row, self, principal.user_id)
            conn.execute(
                update(t.deletions)
                .where(t.deletions.c.id == entry_id)
                .values(
                    state=deleted.PURGED,
                    purged_by=principal.user_id,
                    purged_at=clock.to_text(clock.now()),
                    detail_json={**(entry["detail_json"] or {}), "purged": purged.detail},
                )
            )
            audit.record(
                conn,
                principal,
                f"{entry['object_type']}.purge",
                object_type=entry["object_type"],
                object_id=entry["object_id"],
                detail=purged.detail,
            )
            result = public_entry(self._entry(conn, t.deletions.c.id == entry_id))
        leftovers: dict = {}
        for step in purged.after_commit:
            try:
                leftovers.update(step() or {})
            except Exception:
                # Records are already gone; the sweep of unindexed files (and start-up
                # reconciliation) picks up what is left.
                log.exception("purge cleanup of %s failed", entry_id)
                leftovers["cleanup_errors"] = leftovers.get("cleanup_errors", 0) + 1
        if leftovers:
            # The receipt says so: "purged" does not mean every file is gone yet.
            with self.db.write() as conn:
                detail = entry["detail_json"] or {}
                conn.execute(
                    update(t.deletions)
                    .where(t.deletions.c.id == entry_id)
                    .values(detail_json={**detail, "purged": {**purged.detail, **leftovers}})
                )
                result = public_entry(self._entry(conn, t.deletions.c.id == entry_id))
        return result

    # --- helpers for handlers ------------------------------------------------------

    def mark_purged(
        self,
        conn: Connection,
        object_type: str,
        ids: list[str],
        actor_id: str,
        add_receipts: bool = False,
    ) -> None:
        """Bin entries purged along with their parent; with ``add_receipts`` objects
        that were not in the bin get a purged receipt too."""
        if not ids:
            return
        at = clock.to_text(clock.now())
        conn.execute(
            update(t.deletions)
            .where(
                t.deletions.c.object_type == object_type,
                t.deletions.c.object_id.in_(ids),
                t.deletions.c.state == deleted.RECYCLED,
            )
            .values(state=deleted.PURGED, purged_at=at, purged_by=actor_id)
        )
        if not add_receipts:
            return
        present = set(
            conn.execute(
                select(t.deletions.c.object_id).where(
                    t.deletions.c.object_type == object_type, t.deletions.c.object_id.in_(ids)
                )
            ).scalars()
        )
        handler = self._handler(object_type)
        for object_id in set(ids) - present:
            row = handler.load(conn, object_id)
            conn.execute(
                insert(t.deletions).values(
                    id=new_id("del"),
                    object_type=object_type,
                    object_id=object_id,
                    label=handler.label(conn, row)[:256],
                    owner_id=handler.owner(row),
                    state=deleted.PURGED,
                    deleted_by=actor_id,
                    deleted_at=at,
                    purged_by=actor_id,
                    purged_at=at,
                    detail_json={"with": "dataset"},
                )
            )

    def remove_files(self, keys: list[str]) -> dict | None:
        """Try every key; a failure does not skip the rest. The failures are logged and
        returned for the receipt; the sweep of unindexed files retries them later."""
        failed = []
        for key in keys:
            try:
                self.store.delete(key)
            except Exception:
                log.exception("file %s could not be removed after a purge", key)
                failed.append(key)
        if failed:
            return {"files_failed": len(failed), "failed_keys": failed[:FAILED_KEYS_SHOWN]}
        return None

    def remove_task_materials(self, digests: list[str]) -> dict | None:
        failed = 0
        for digest in digests if self.materials else []:
            try:
                self.materials.remove(digest)
            except Exception:
                log.exception("task material %s could not be removed after a purge", digest)
                failed += 1
        return {"materials_failed": failed} if failed else None

    def remove_attachment_files(self, content_digest: str) -> dict | None:
        """Unless another version still holds the same content. Checked and moved away
        under the write lock, so a publish of this content in between keeps its files."""
        if self.attachments is None:
            return None
        try:
            with self.db.write() as conn:
                used = conn.execute(
                    select(t.attachment_versions.c.id).where(
                        t.attachment_versions.c.content_digest == content_digest,
                        t.attachment_versions.c.id.not_in(
                            select(t.deletions.c.object_id).where(
                                t.deletions.c.object_type == "attachment_version",
                                t.deletions.c.state == deleted.PURGED,
                            )
                        ),
                    )
                ).first()
                # Only the rename happens under the lock; deleting can take a while.
                retired = self.attachments.retire(content_digest) if used is None else None
            if retired is not None:
                shutil.rmtree(retired, ignore_errors=True)
        except Exception:
            log.exception("attachment files %s could not be removed", content_digest)
            return {"attachment_files_failed": content_digest}
        return None

    def discard_preparations(self, outputs: list[tuple[str, dict]]) -> dict | None:
        """Local images and their originals; runs from a request thread after commit.
        A spec that a new import prepared again meanwhile has a live row: keep it.
        Each image is tried on its own; the failures are logged and returned."""
        if self.environment is None or not outputs:
            return None
        with self.db.read() as conn:
            live = set(
                conn.execute(
                    select(t.preparations.c.spec_digest).where(
                        t.preparations.c.spec_digest.in_([spec for spec, _ in outputs])
                    )
                ).scalars()
            )

        async def discard() -> int:
            failed = 0
            for spec, output in outputs:
                if spec in live:
                    continue
                try:
                    await self.environment.discard(PreparedOutput.from_json(output))
                except Exception:
                    log.exception("prepared image of %s could not be discarded after a purge", spec)
                    failed += 1
            return failed

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            failed = asyncio.run(discard())
        else:
            with ThreadPoolExecutor(1) as pool:  # called on a loop thread: run beside it
                failed = pool.submit(asyncio.run, discard()).result()
        return {"preparations_failed": failed} if failed else None

    def expire_exports(self, run_ids: set[str]) -> None:
        if self.exports:
            self.exports.expire(run_ids)
