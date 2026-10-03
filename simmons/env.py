"""Environment loading seam; T06 supplies ``.env`` support."""

from pathlib import Path
import os

DISABLE_DOTENV_ENV = "SIMMONS_DISABLE_DOTENV"


def load_env(repo_root: str | Path | None = None) -> None:
    """Load the repository's gitignored ``.env`` file when present."""
    if os.environ.get(DISABLE_DOTENV_ENV) == "1":
        return
    raise NotImplementedError("T06")
