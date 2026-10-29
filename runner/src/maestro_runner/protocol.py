"""Runner protocol stream (15 section 5). Import before Harbor.

Importing this module keeps a private duplicate of the original stdout for NDJSON
events and points fd 1 at stderr, so Harbor, rich, docker compose and any child
process can only write diagnostics, never protocol lines.
"""

import json
import os
from datetime import UTC, datetime

PROTOCOL_VERSION = 2

_stream = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
os.dup2(2, 1)
_sequence = 0
run_id: str | None = None
on_lost = None  # called once when the worker stops reading: the Runner then cancels
_lost = False


def now() -> str:
    return datetime.now(UTC).isoformat()


def emit(kind: str, /, **payload) -> None:
    """One line per event; sequence numbers are gap-free so the worker can detect loss."""
    global _sequence
    _sequence += 1
    envelope = {
        "protocol_version": PROTOCOL_VERSION,
        "run_id": run_id,
        "sequence": _sequence,
        "type": kind,
        "at": now(),
        "payload": payload,
    }
    if _lost:
        return
    try:
        _stream.write(json.dumps(envelope, separators=(",", ":"), ensure_ascii=False) + "\n")
    except (BrokenPipeError, ValueError):
        _channel_lost()


def _channel_lost() -> None:
    global _lost
    _lost = True
    if on_lost is not None:
        on_lost()


def lost() -> bool:
    return _lost
