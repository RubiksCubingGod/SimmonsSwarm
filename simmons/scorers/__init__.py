"""Scorer registry."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from ..types import Scorer

S = TypeVar("S", bound=type[Scorer])
SCORERS: dict[str, type[Scorer]] = {}


def register_scorer(name: str) -> Callable[[S], S]:
    def register(scorer_type: S) -> S:
        SCORERS[name] = scorer_type
        return scorer_type

    return register


def get_scorer(name: str) -> type[Scorer]:
    try:
        return SCORERS[name]
    except KeyError as exc:
        raise KeyError(f"unknown scorer: {name}") from exc


def scorer_names() -> tuple[str, ...]:
    return tuple(sorted(SCORERS))

