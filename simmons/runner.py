"""Run orchestration seam; T02f supplies the walking skeleton."""

from typing import Any

from .config import Config


def run(
    config: Config,
    *,
    fake: bool = False,
    scratch: bool = False,
    episodes: int | None = None,
    seed: int | None = None,
    max_cost: float | None = None,
    json_output: bool = False,
) -> dict[str, Any]:
    """Run an experiment and return its machine-readable result."""
    raise NotImplementedError("T02f")

