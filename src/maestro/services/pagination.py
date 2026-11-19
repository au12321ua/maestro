"""Stable ``(created_at, id)`` cursors bound to the filter that produced them."""

import base64
import json

from sqlalchemy import Column, Select, and_, or_

from maestro.core.digests import digest_json
from maestro.core.errors import MaestroError
from maestro.domain import limits


def check_limit(limit: int | None) -> int:
    limit = limits.PAGE_DEFAULT if limit is None else limit
    if not 1 <= limit <= limits.PAGE_MAX:
        raise MaestroError("INVALID_LIMIT", f"limit must be 1-{limits.PAGE_MAX}", 400)
    return limit


def matching(text: str, *columns):
    """Search literal text across the fields that users see, including ids."""
    return or_(*(column.icontains(text, autoescape=True) for column in columns))


def encode(created_at: str, item_id: str, filters: dict) -> str:
    raw = json.dumps([created_at, item_id, digest_json(filters)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode(cursor: str, filters: dict) -> tuple[str, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        created_at, item_id, digest = json.loads(raw)
        if not isinstance(created_at, str) or not isinstance(item_id, str):
            raise TypeError
    except (ValueError, TypeError):
        raise MaestroError("INVALID_CURSOR", "Malformed cursor", 400) from None
    if digest != digest_json(filters):
        raise MaestroError("CURSOR_FILTER_MISMATCH", "Filters changed; restart pagination", 400)
    return created_at, item_id


def page(
    query: Select, created: Column, ident: Column, cursor: str | None, limit: int, filters: dict
) -> Select:
    """Newest first."""
    if cursor:
        at, item = decode(cursor, filters)
        query = query.where(or_(created < at, and_(created == at, ident < item)))
    return query.order_by(created.desc(), ident.desc()).limit(limit + 1)


def split(rows: list, limit: int, created_key: str, filters: dict) -> tuple[list, str | None]:
    if len(rows) <= limit:
        return rows, None
    rows = rows[:limit]
    last = rows[-1]
    return rows, encode(last[created_key], last["id"], filters)
