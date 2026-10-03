"""OpenRouter provider seam; T06 supplies the adapter."""

from collections.abc import Mapping
from typing import Any

from .base import register_provider
from ..types import ProviderStatus, RoleRequest, RoleResponse


@register_provider("openrouter")
class OpenRouterProvider:
    name = "openrouter"

    def __init__(self, **params: Any) -> None:
        raise NotImplementedError("T06")

    def complete(self, request: RoleRequest) -> RoleResponse:
        raise NotImplementedError("T06")

    def build_body(self, request: RoleRequest) -> Mapping[str, Any]:
        raise NotImplementedError("T06")

    def check(self) -> ProviderStatus:
        raise NotImplementedError("T06")

