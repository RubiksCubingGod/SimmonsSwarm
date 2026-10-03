"""Strict role-template rendering seam; T02c supplies the implementation."""

from .types import RoleView


def render(template: str, view: RoleView) -> str:
    """Render a strict string.Template using only fields present on ``view``."""
    raise NotImplementedError("T02c")

