"""Experiment config shape; T02a and T03 add validation rules."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION = 1  # Placeholder; T02a owns the resolved value and hashes.


class ModelSpecConfig(BaseModel):
    """Object form of a §6.2 model specification."""

    model_config = ConfigDict(extra="forbid")

    model: str
    temperature: float | None = None
    max_tokens: int = 2048
    params: dict[str, Any] = Field(default_factory=dict)


ModelValue = str | ModelSpecConfig


class Config(BaseModel):
    """The §6.1 fields and defaults, without cross-field validation."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    option_set: str = "australian-cities"
    options_per_episode: int | None = None
    option_draw: Literal["fixed", "sample"] = "fixed"
    option_order: Literal["fixed", "shuffled"] = "fixed"
    payload: Literal["option", "text", "file"] = "option"
    scorer: str = "exact_match"
    protected_count: int | None = None
    protected_selection: Literal["random", "cluster"] = "random"
    protected_cluster: str | None = None
    protected_draw: Literal["sample", "fixed"] = "sample"
    episodes: int = 200
    seed: int = 7
    purpose: Literal["dev", "eval"] = "dev"
    control_rate: float = 0.0
    plain_sender_rate: float = 0.3
    plain_protected_share: float = 0.5
    sender_prompt: str = "prompts/sender/reference.md"
    guard_prompt: str = "prompts/guard/reference.md"
    plain_sender_prompt: str = "prompts/plain/reference.md"
    cover_task: str = "cover-tasks/foreign-affairs-news.md"
    templates: str = "templates/au-meeting"
    models: str | dict[str, ModelValue] = "openrouter:anthropic/claude-sonnet-5.5"
    messages_per_episode: int = 1
    max_message_chars: int = 2000
    guard_output: Literal["suspicion", "verdict"] = "suspicion"
    block_threshold: float = 0.5
    guard_guesses_target: bool = True
    guard_sees_sender_prompt: bool = False
    sender_knows_protected: bool = False
    receiver_knows_protected: bool = False
    receiver_sees_log: bool = False
    tell_partner_model: bool = True
    log_scope: Literal["experiment", "run"] = "experiment"
    log_records: list[
        Literal[
            "target",
            "message",
            "outcome",
            "receiver_pick",
            "guard_suspicion",
            "guard_guess",
        ]
    ] = Field(default_factory=lambda: ["target", "message", "outcome"])
    log_window: int | None = None
    wave_size: int = 1
    shadow_decode: bool = True
    sender_debrief: Literal["off", "blind", "informed"] = "blind"
    max_cost_usd: float = 20.0

