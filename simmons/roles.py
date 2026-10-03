"""Tool and request seams; T02c supplies role-specific behavior."""

from collections.abc import Mapping
from typing import Any

from .config import Config
from .types import EpisodeState, Inputs, ModelSpec, RoleRequest, RoleView, ToolSpec


def tool_for(role: str, config: Config, view: RoleView) -> ToolSpec:
    """Return the configured structured-output tool for a role."""
    raise NotImplementedError("T02c")


def validate(role: str, tool_input: Mapping[str, Any], view: RoleView) -> Mapping[str, Any]:
    """Validate and normalize one tool result against its role contract."""
    raise NotImplementedError("T02c")


def request_for(
    role: str,
    state: EpisodeState,
    config: Config,
    inputs: Inputs,
    model: ModelSpec,
) -> RoleRequest:
    """Build the complete provider request from a role view and resolved model."""
    raise NotImplementedError("T02c")

