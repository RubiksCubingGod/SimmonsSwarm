"""Environment loading seam; T06 supplies ``.env`` support."""

from pathlib import Path


def load_env(repo_root: str | Path | None = None) -> None:
    """Load the repository's gitignored ``.env`` file when present."""
    raise NotImplementedError("T06")

