"""Append structured, prompt-free run events."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .records import append_jsonl


def emit(
    event: str,
    *,
    run_id: str,
    path: str | Path,
    level: str = "info",
    **fields: Any,
) -> None:
    """Append one structured run event to the event log.

    Callers should reference provider requests by hash; event records must not
    contain rendered prompts or provider secrets.
    """
    record = {
        "schema": "simmons.event/1",
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "level": level,
        "event": event,
        "run_id": run_id,
        **fields,
    }
    append_jsonl(path, record)
