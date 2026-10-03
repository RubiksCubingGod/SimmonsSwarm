"""Role view seam; T02c supplies the visibility table and projection."""

from .config import Config
from .types import EpisodeState, Inputs, RoleView


def build_view(role: str, state: EpisodeState, config: Config, inputs: Inputs) -> RoleView:
    """Project episode state into the frozen view for ``role``."""
    raise NotImplementedError("T02c")

