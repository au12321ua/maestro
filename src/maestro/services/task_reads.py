"""Permanent summaries of task content delivered to authenticated readers."""

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from maestro.core import clock
from maestro.core.errors import FieldError, MaestroError
from maestro.db import schema as t
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


def summaries(conn, task_ids) -> list[dict]:
    rows = conn.execute(
        select(t.task_reads, t.users.c.username.label("reader_username"))
        .join(t.users, t.users.c.id == t.task_reads.c.reader_id)
        .where(t.task_reads.c.task_id.in_(task_ids))
        .order_by(
            t.task_reads.c.task_id,
            t.task_reads.c.reader_id,
            t.task_reads.c.channel,
            t.task_reads.c.channel_id,
            t.task_reads.c.category,
        )
    ).mappings()
    return [dict(row) for row in rows]


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
