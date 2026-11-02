"""Run result summaries read from trial records.

One loader for the run list, the run page, the summary endpoint and exports, so
they all report the same counts for the same records.
"""

from sqlalchemy import Connection, func, select

from maestro.db import schema as t
from maestro.domain.scoring import TrialRecord, snapshot_contract, summarize


def trial_records(conn: Connection, run_ids: list[str]) -> dict[str, list[TrialRecord]]:
    attempts = (
        select(t.execution_attempts.c.trial_id, func.count().label("n"))
        .group_by(t.execution_attempts.c.trial_id)
        .subquery()
    )
    rows = conn.execute(
        select(
            t.trials.c.run_id,
            t.trials.c.state,
            t.trials.c.reason_code,
            t.tasks.c.task_key,
            t.trial_results.c.raw_score,
            t.trial_results.c.failure_cause,
            t.trial_results.c.usage_json,
            func.coalesce(attempts.c.n, 0).label("attempts"),
        )
        .join(t.tasks, t.tasks.c.id == t.trials.c.task_id)
        .outerjoin(t.trial_results, t.trial_results.c.trial_id == t.trials.c.id)
        .outerjoin(attempts, attempts.c.trial_id == t.trials.c.id)
        .where(t.trials.c.run_id.in_(run_ids))
    ).all()
    found: dict[str, list[TrialRecord]] = {run_id: [] for run_id in run_ids}
    for row in rows:
        found[row.run_id].append(
            TrialRecord(
                task_key=row.task_key,
                state=row.state,
                raw_score=row.raw_score,
                # Trials ended without an executor result carry a platform reason only.
                failure_cause=row.failure_cause
                or (row.reason_code.lower() if row.reason_code else None),
                attempts=row.attempts,
                usage=row.usage_json,
            )
        )
    return found


def summaries(conn: Connection, run_ids: list[str]) -> dict[str, dict]:
    if not run_ids:
        return {}
    records = trial_records(conn, run_ids)
    meta = conn.execute(
        select(
            t.runs.c.id,
            t.runs.c.dataset_version_id,
            t.run_snapshots.c.policy_json,
        )
        .outerjoin(t.run_snapshots, t.run_snapshots.c.run_id == t.runs.c.id)
        .where(t.runs.c.id.in_(run_ids))
    ).all()
    versions = {row.dataset_version_id for row in meta}
    bench = dict(
        conn.execute(
            select(t.tasks.c.dataset_version_id, func.count())
            .where(t.tasks.c.dataset_version_id.in_(versions))
            .group_by(t.tasks.c.dataset_version_id)
        ).all()
    )
    result = {}
    for row in meta:
        result[row.id] = summarize(
            records.get(row.id, []),
            snapshot_contract(row.policy_json),
            bench.get(row.dataset_version_id),
        )
    return result
