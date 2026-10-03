"""Payload registry."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from ..types import Payload

P = TypeVar("P", bound=type[Payload])
PAYLOADS: dict[str, type[Payload]] = {}


def register_payload(name: str) -> Callable[[P], P]:
    def register(payload_type: P) -> P:
        PAYLOADS[name] = payload_type
        return payload_type

    return register


def get_payload(name: str) -> type[Payload]:
    try:
        return PAYLOADS[name]
    except KeyError as exc:
        raise KeyError(f"unknown payload: {name}") from exc


def payload_names() -> tuple[str, ...]:
    return tuple(sorted(PAYLOADS))

