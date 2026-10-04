"""Frozen contracts shared by providers, roles, episodes, and experiments."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from random import Random
from typing import Any, Protocol, TypeAlias


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str
    temperature: float | None
    max_tokens: int
    params: Mapping[str, Any]


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class FilePart:
    name: str
    media_type: str
    data: bytes


Part: TypeAlias = TextPart | FilePart


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True)
class PriorTurn:
    """A prior tool-use turn replayed inside the sender's exit interview.

    ``assistant`` is the provider message exactly as returned, including opaque
    ``reasoning_details``. ``tool_result`` is the rendered debrief prompt.
    """

    tool: ToolSpec
    assistant: Mapping[str, Any]
    tool_call_id: str
    tool_result: tuple[Part, ...]


@dataclass(frozen=True)
class RoleRequest:
    request_id: str
    role: str
    model: ModelSpec
    system: str
    user: tuple[Part, ...]
    tool: ToolSpec
    prior: PriorTurn | None = None
    attempt: int = 1


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class RoleResponse:
    tool_input: Mapping[str, Any] | None
    text: str | None
    thinking: str | None
    reasoning_details: tuple[Mapping[str, Any], ...] | None
    parse_error: str | None
    usage: Usage
    cost_usd: float | None
    generation_id: str | None
    served_by: str | None
    model_reported: str
    finish_reason: str
    tool_choice_mode: str
    latency_ms: int
    raw: Mapping[str, Any]
    assistant_message: Mapping[str, Any] | None = None
    tool_call_id: str | None = None


class TransientError(Exception):
    """Provider failure that the episode loop may retry with backoff."""

    def __init__(
        self,
        message: str = "",
        retry_after_s: float | None = None,
        *,
        network_down: bool = False,
    ):
        super().__init__(message)
        self.retry_after_s = retry_after_s
        self.network_down = network_down


class PermanentError(Exception):
    """Provider failure that should fail this call without retrying."""


class BudgetError(Exception):
    """Provider refused a call for budget reasons."""


class BreakerTripped(Exception):
    """The local spend breaker refused a reservation."""


@dataclass(frozen=True)
class ProviderStatus:
    provider: str
    key_present: bool
    detail: str


class Provider(Protocol):
    name: str

    def complete(self, request: RoleRequest) -> RoleResponse: ...

    def build_body(self, request: RoleRequest) -> Mapping[str, Any]: ...

    def check(self) -> ProviderStatus: ...


@dataclass(frozen=True)
class Target:
    payload: str
    value: str
    index: int | None


@dataclass(frozen=True)
class Pick:
    payload: str
    value: str
    index: int | None


@dataclass(frozen=True)
class Score:
    value: float
    hit: bool
    detail: Mapping[str, Any] | None


class Scorer(Protocol):
    name: str
    payloads: frozenset[str]
    needs_model: bool

    def score(
        self, target: Target, pick: Pick, judge: JudgeCaller | None
    ) -> Score: ...


@dataclass(frozen=True)
class PlanContext:
    episode: int
    kind: str
    options: tuple[str, ...]
    protected: frozenset[int]


class Payload(Protocol):
    name: str

    def draw_target(self, rng: Random, plan_ctx: PlanContext, kind: str) -> Target: ...

    def target_section(self, target: Target) -> str: ...

    def receiver_tool(self, view: ReceiverView) -> ToolSpec: ...

    def parse_pick(self, tool_input: Mapping[str, Any], view: ReceiverView) -> Pick: ...

    def guess_schema(self, view: GuardView) -> Mapping[str, Any] | None: ...


@dataclass(frozen=True)
class ModelInfo:
    slug: str
    context_length: int
    supports_tools: bool
    endpoint_prices: tuple[tuple[str, float, float], ...]
    supported_parameters: frozenset[str] = frozenset()
    # Per-endpoint (host tag, supported parameters); supported_parameters is
    # their intersection across every endpoint.
    endpoint_parameters: tuple[tuple[str, frozenset[str]], ...] = ()


class Catalog(Protocol):
    def lookup(self, slug: str) -> ModelInfo | None: ...

    def age_seconds(self) -> float | None: ...


@dataclass(frozen=True)
class Reservation:
    attempt_id: str
    usd: float
    request_sha256: str


class Breaker(Protocol):
    def reserve(
        self,
        attempt_id: str,
        body: Mapping[str, Any],
        spec: ModelSpec,
        *,
        request_hash: str | None = None,
    ) -> Reservation: ...

    def settle(self, reservation: Reservation, response: RoleResponse) -> None: ...

    def unresolved(self, reservation: Reservation, reason: str) -> None: ...

    @property
    def tripped(self) -> str | None: ...

    def spent_usd(self) -> float: ...


@dataclass(frozen=True)
class OptionSet:
    options: tuple[str, ...]
    clusters: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    name: str | None = None
    description: str = ""


@dataclass(frozen=True)
class Inputs:
    """Loaded immutable experiment inputs consumed by roles and the runner."""

    option_set: OptionSet
    sender_prompt: str
    guard_prompt: str
    plain_sender_prompt: str
    cover_task: str
    templates: Mapping[str, str]


@dataclass(frozen=True)
class EpisodeState:
    """Role-visible episode facts; builders project these into typed views."""

    episode: int
    kind: str
    options: tuple[str, ...]
    target: Target
    protected: frozenset[int] | None
    message: str | None = None
    attempt_log: tuple[Mapping[str, Any], ...] = ()
    guard_suspicion: float | None = None
    guard_guess: Pick | None = None
    receiver_pick: Pick | None = None
    outcome: str | None = None
    sender_response: RoleResponse | None = None


@dataclass(frozen=True)
class EpisodeContext:
    """Dependencies fixed for one episode, passed through the frozen seam."""

    config: Any
    inputs: Inputs
    providers: Mapping[str, Provider]
    payload: Payload
    scorer: Scorer
    run_id: str = ""
    resolved_models: Mapping[str, ModelSpec | None] = field(default_factory=dict)
    breaker: Breaker | None = None
    request_log_path: str | None = None
    calls_log_path: str | None = None
    events_log_path: str | None = None
    wave: int = 1
    attempt_log: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class SenderView:
    """Send-call view; contains only the S variables allowed by §11.1."""

    role: str
    episode: int
    cover_task: str
    options: tuple[str, ...]
    option_count: int
    protected_count: int | None
    target: Target
    protected_section: str
    log_section: str
    partner_section: str
    max_message_chars: int
    rendered_sections: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PlainSenderView:
    """Plain-sender view; no P₁, protected list, partner model, or log."""

    role: str
    episode: int
    cover_task: str
    options: tuple[str, ...]
    option_count: int
    protected_count: int | None
    plain_target_section: str
    target: Target
    max_message_chars: int
    rendered_sections: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class GuardView:
    """Guard view; deliberately has no target or sender attempt log."""

    role: str
    episode: int
    cover_task: str
    options: tuple[str, ...]
    option_count: int
    protected_count: int | None
    protected_section: str
    message: str
    sender_prompt_section: str
    rendered_sections: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ReceiverView:
    """Receiver view; deliberately has no target or sender prompt."""

    role: str
    episode: int
    cover_task: str
    options: tuple[str, ...]
    option_count: int
    protected_count: int | None
    protected_section: str
    log_section: str
    partner_section: str
    message: str
    rendered_sections: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class JudgeView:
    """Judge view; contains only the target and the receiver's pick."""

    role: str
    episode: int
    target: Target
    pick: Pick
    rendered_sections: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SenderDebriefView:
    """Sixth view for the post-outcome interview; SenderView stays send-only."""

    role: str
    episode: int
    cover_task: str
    options: tuple[str, ...]
    option_count: int
    protected_count: int | None
    target: Target
    protected_section: str
    log_section: str
    partner_section: str
    max_message_chars: int
    debrief_outcome_section: str
    rendered_sections: Mapping[str, str] = field(default_factory=dict)


RoleView: TypeAlias = (
    SenderView
    | PlainSenderView
    | GuardView
    | ReceiverView
    | JudgeView
    | SenderDebriefView
)

JudgeCaller: TypeAlias = Callable[[JudgeView], Score]
