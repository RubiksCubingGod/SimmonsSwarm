"""Input loading seam; T02a supplies the implementation."""

from pathlib import Path

from .config import Config
from .types import Inputs


def load(config: Config) -> Inputs:
    """Load and validate options, prompts, cover task, and templates."""
    raise NotImplementedError("T02a")


def snapshot(inputs: Inputs, destination: str | Path) -> Path:
    """Copy the exact loaded inputs into a run's ``inputs/`` directory."""
    raise NotImplementedError("T02a")

