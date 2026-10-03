"""Provider registry shared by adapter modules."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from ..types import Provider

P = TypeVar("P", bound=type[Provider])
PROVIDERS: dict[str, type[Provider]] = {}


def register_provider(name: str) -> Callable[[P], P]:
    def register(provider_type: P) -> P:
        PROVIDERS[name] = provider_type
        return provider_type

    return register


def get_provider(name: str) -> type[Provider]:
    try:
        return PROVIDERS[name]
    except KeyError as exc:
        raise KeyError(f"unknown provider: {name}") from exc


def provider_names() -> tuple[str, ...]:
    return tuple(sorted(PROVIDERS))

