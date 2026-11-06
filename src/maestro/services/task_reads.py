"""Permanent summaries of task content delivered to authenticated readers."""

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from maestro.core import clock
from maestro.core.errors import FieldError, MaestroError
from maestro.db import schema as t
from maestro.services import task_identity
from maestro.services.principal import Channel, Principal


def record(db, principal: Principal, task_ids, category: str) -> None:
    task_ids = sorted(set(task_ids))
    if not task_ids:
        return
    # The web is one channel per account, however many times it logs in: rows stay
    # bounded by tasks x readers x categories. Each PAT is its own channel.
    if principal.channel == Channel.PAT and principal.pat_id:
        channel_id = principal.pat_id
    elif principal.channel == Channel.WEB_SESSION:
        channel_id = "web"
    else:
        raise MaestroError("READ_RECORD_FAILED", "A reader channel is required", 503)
    with db.write() as conn:
        at = clock.to_text(clock.now())
        for task_id in task_ids:
            statement = insert(t.task_reads).values(
                task_id=task_id,
                reader_id=principal.user_id,
                channel=principal.channel.value,
                channel_id=channel_id,
                category=category,
                first_read_at=at,
                last_read_at=at,
                count=1,
            )
            conn.execute(
                statement.on_conflict_do_update(
                    index_elements=["task_id", "reader_id", "channel", "channel_id", "category"],
                    set_={"last_read_at": at, "count": t.task_reads.c.count + 1},
                )
            )


def by_task(conn, task_ids, relationships=None) -> dict[str, list[dict]]:
    relationships = (
        relationships if relationships is not None else task_identity.related(conn, task_ids)
    )
    origin_ids = {origin for origins in relationships.values() for origin in origins}
    rows = (
        conn.execute(
            select(
                t.task_reads,
                t.users.c.username.label("reader_username"),
                t.tasks.c.dataset_version_id,
            )
            .join(t.users, t.users.c.id == t.task_reads.c.reader_id)
            .join(t.tasks, t.tasks.c.id == t.task_reads.c.task_id)
            .where(t.task_reads.c.task_id.in_(origin_ids))
            .order_by(
                t.task_reads.c.task_id,
                t.task_reads.c.reader_id,
                t.task_reads.c.channel,
                t.task_reads.c.channel_id,
                t.task_reads.c.category,
            )
        )
        .mappings()
        .all()
    )
    return {
        target: [
            {**row, "content_changed": origins[row["task_id"]]}
            for row in rows
            if row["task_id"] in origins
        ]
        for target, origins in relationships.items()
    }


def summaries(conn, task_ids) -> list[dict]:
    # A batch returns the union of original rows. If content matches any requested
    # identity, that row is unchanged relative to the batch.
    result = {}
    for rows in by_task(conn, task_ids).values():
        for row in rows:
            key = tuple(
                row[field]
                for field in ("task_id", "reader_id", "channel", "channel_id", "category")
            )
            if key not in result or not row["content_changed"]:
                result[key] = row
    return list(result.values())


def query(db, task_ids: list[str]) -> list[dict]:
    with db.read() as conn:
        found = set(conn.execute(select(t.tasks.c.id).where(t.tasks.c.id.in_(task_ids))).scalars())
        missing = set(task_ids) - found
        if missing:
            raise MaestroError(
                "NOT_FOUND",
                "Task not found",
                404,
                field_errors=[FieldError("/task_ids", "NOT_FOUND", x) for x in sorted(missing)],
            )
        return summaries(conn, task_ids)
