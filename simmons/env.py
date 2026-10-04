"""Small, dependency-free loader for the repository's gitignored ``.env``."""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

DISABLE_DOTENV_ENV = "SIMMONS_DISABLE_DOTENV"
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _dotenv_value(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[0] in "'\"":
        try:
            parsed = shlex.split(value, comments=False, posix=True)
        except ValueError:
            return value[1:]
        return parsed[0] if parsed else ""
    # Dotenv comments start after whitespace, leaving URL fragments intact.
    return re.split(r"\s+#", value, maxsplit=1)[0].rstrip()


def load_env(repo_root: str | Path | None = None) -> None:
    """Load ``<repo_root>/.env`` without overriding the process environment.

    The default root is the parent of the ``simmons`` package. Tests and
    isolated callers can disable dotenv loading with
    ``SIMMONS_DISABLE_DOTENV=1``.
    """
    if os.environ.get(DISABLE_DOTENV_ENV) == "1":
        return

    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[1]
    dotenv = root / ".env"
    try:
        lines = dotenv.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return

    for line in lines:
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        if entry.startswith("export "):
            entry = entry[7:].lstrip()
        key, separator, raw_value = entry.partition("=")
        key = key.strip()
        if not separator or not _KEY.fullmatch(key):
            continue
        os.environ.setdefault(key, _dotenv_value(raw_value))
