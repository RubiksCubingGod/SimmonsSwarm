"""Catalog seam; T07 supplies cached model lookup behavior."""

from pathlib import Path

from .types import Catalog, ModelInfo


def load_catalog(path: str | Path | None = None) -> Catalog:
    """Load a cached catalog without network access."""
    raise NotImplementedError("T07")


__all__ = ["Catalog", "ModelInfo", "load_catalog"]

