"""Strict rendering from a role's typed view only."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from string import Template
from typing import Any

from .types import Pick, RoleView, Target


def _as_text(name: str, value: Any) -> str:
    if isinstance(value, (Target, Pick)):
        return value.value
    if name == "options" and isinstance(value, tuple):
        return "\n".join(f"{index}. {option}" for index, option in enumerate(value, start=1))
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    raise TypeError(f"template variable {name!r} has unsupported value {type(value).__name__}")


def render(template: str, view: RoleView) -> str:
    """Render a strict ``string.Template`` using only fields on ``view``.

    ``role`` and ``episode`` are harness metadata, not prompt variables.
    Optional public counts are absent when undefined, so a template that uses
    ``$protected_count`` without a watchlist raises ``KeyError`` as validation
    expects. ``safe_substitute`` is deliberately not used.
    """
    if not is_dataclass(view) or isinstance(view, type):
        raise TypeError("render requires a typed role view")

    declared = {field.name for field in fields(view)}
    provided_sections = dict(view.rendered_sections)
    invalid_sections = set(provided_sections) - (declared - {"role", "episode", "rendered_sections"})
    if invalid_sections:
        bad = ", ".join(sorted(invalid_sections))
        raise ValueError(f"rendered sections exceed the {view.role} view: {bad}")

    namespace: dict[str, str] = {}
    for field in fields(view):
        name = field.name
        if name in {"role", "episode", "rendered_sections"}:
            continue
        value = getattr(view, name)
        if name == "protected_count" and value is None:
            continue
        if name in provided_sections:
            value = provided_sections[name]
        namespace[name] = _as_text(name, value)

    return Template(template).substitute(namespace)
