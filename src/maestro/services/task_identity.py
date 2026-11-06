"""Query-time task relationships within a dataset; stored references never move."""

from sqlalchemy import and_, or_, select

from maestro.db import schema as t
from maestro.services.dataset_versions import not_purged


def related(conn, task_ids) -> dict[str, dict[str, bool]]:
    """For each requested task, map recorded task ids to whether content changed.

    Own references remain readable after purge. Other versions contribute until
    physically purged, including while recycled.
    """
    target = t.tasks.alias("target_task")
    target_version = t.dataset_versions.alias("target_version")
    origin_version = t.dataset_versions.alias("origin_version")
    query = (
        select(
            target.c.id.label("target_id"),
            t.tasks.c.id,
            (target.c.content_digest != t.tasks.c.content_digest).label("content_changed"),
        )
        .select_from(target)
        .join(target_version, target_version.c.id == target.c.dataset_version_id)
        .join(origin_version, origin_version.c.dataset_id == target_version.c.dataset_id)
        .join(
            t.tasks,
            and_(
                t.tasks.c.dataset_version_id == origin_version.c.id,
                t.tasks.c.task_key == target.c.task_key,
            ),
        )
        .where(
            target.c.id.in_(task_ids),
            or_(
                t.tasks.c.id == target.c.id,
                and_(
                    not_purged(origin_version.c.id),
                    not_purged(origin_version.c.dataset_id, "dataset"),
                ),
            ),
        )
    )
    result = {task_id: {} for task_id in task_ids}
    for target_id, origin_id, changed in conn.execute(query):
        result[target_id][origin_id] = changed
    return result
