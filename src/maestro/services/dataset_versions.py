"""Immutable upload metadata, checked under the database write lock."""

from sqlalchemy import select

from maestro.core.errors import FieldError, MaestroError
from maestro.db import schema as t
from maestro.execution.attachments import LABEL
from maestro.services import deleted


def not_purged(column, kind="dataset_version"):
    return column.not_in(
        select(t.deletions.c.object_id).where(
            t.deletions.c.object_type == kind, t.deletions.c.state == deleted.PURGED
        )
    )


def check_upload_metadata(conn, dataset_id, label, based_on, content_digest=None):
    if label is not None and not (isinstance(label, str) and LABEL.fullmatch(label)):
        raise MaestroError(
            "DATASET_UPLOAD_INVALID",
            "A label starts with a letter or digit and contains letters, digits and ._+- (at most 64)",
            422,
            field_errors=[FieldError("/label", "PATTERN")],
        )
    if based_on is not None:
        base = conn.execute(
            select(t.dataset_versions.c.id)
            .join(t.datasets, t.datasets.c.id == t.dataset_versions.c.dataset_id)
            .where(
                t.dataset_versions.c.id == based_on,
                t.dataset_versions.c.dataset_id == dataset_id,
                t.datasets.c.source_kind == "upload",
                deleted.version_visible(t.dataset_versions.c.id),
            )
        ).scalar()
        if base is None:
            raise MaestroError(
                "DATASET_UPLOAD_INVALID",
                "The base must be an undeleted version of this upload dataset",
                422,
                field_errors=[FieldError("/based_on", "INVALID")],
            )
    if label is not None:
        taken = conn.execute(
            select(t.dataset_versions.c.resolved_digest).where(
                t.dataset_versions.c.dataset_id == dataset_id,
                t.dataset_versions.c.label == label,
                not_purged(t.dataset_versions.c.id),
            )
        ).scalar()
        if taken is not None and taken != content_digest:
            raise MaestroError(
                "DATASET_LABEL_TAKEN",
                "Another version holds this label; labels never move",
                409,
                field_errors=[FieldError("/label", "TAKEN")],
            )
