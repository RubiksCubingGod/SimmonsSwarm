"""Single-episode lifecycle seam; T02f supplies the implementation."""

from .records import EpisodeRecord, PlanRecord
from .types import EpisodeContext


def run_episode(line: PlanRecord, ctx: EpisodeContext) -> EpisodeRecord:
    """Run one planned line using the immutable episode context."""
    raise NotImplementedError("T02f")

