"""Structured event append seam; T02f supplies the implementation."""

from pathlib import Path
from typing import Any


def emit(
    event: str,
    *,
    run_id: str,
    path: str | Path,
    level: str = "info",
    **fields: Any,
) -> None:
    """Append one structured run event to the event log."""
    raise NotImplementedError("T02f")

