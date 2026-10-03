"""Episode planning seam; T02b supplies the M1 draw algorithm."""

from .config import Config
from .records import PlanRecord
from .types import OptionSet


def build_plan(config: Config, options: OptionSet, start: int = 0) -> list[PlanRecord]:
    """Build the episode plan using the option set and global starting index."""
    raise NotImplementedError("T02b")

