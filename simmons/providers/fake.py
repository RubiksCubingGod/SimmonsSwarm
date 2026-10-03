"""Fake provider seam; T02d supplies deterministic fake responses."""

from collections.abc import Mapping
from typing import Any

from .base import register_provider
from ..types import ProviderStatus, RoleRequest, RoleResponse


@register_provider("fake")
class FakeProvider:
    name = "fake"

    def __init__(self, **params: Any) -> None:
        raise NotImplementedError("T02d")

    def complete(self, request: RoleRequest) -> RoleResponse:
        raise NotImplementedError("T02d")

    def build_body(self, request: RoleRequest) -> Mapping[str, Any]:
        raise NotImplementedError("T02d")

    def check(self) -> ProviderStatus:
        raise NotImplementedError("T02d")

