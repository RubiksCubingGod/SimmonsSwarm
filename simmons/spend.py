"""Per-run spend estimation, reservation, and journal accounting."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from string import Template
from threading import RLock
from typing import Any

from .config import Config, ModelSpecConfig
from .contracts import EXPLAIN_MESSAGE_INPUT_SCHEMA, SEND_MESSAGE_INPUT_SCHEMA
from .records import PlanRecord, append_jsonl, canonical_json, read_jsonl, request_sha256
from .types import Breaker as BreakerProtocol
from .types import BreakerTripped, Catalog, ModelSpec, Reservation, RoleResponse

RUN_CEILING_USD = 20.0
_DEFAULT_MODEL = "openrouter:anthropic/claude-sonnet-5.5"
_ROLE_OUTPUT_TOKENS = {
    "sender": 300,
    "plain_sender": 300,
    "guard": 200,
    "receiver": 100,
    "judge": 100,
    "sender_debrief": 400,
}
_LOG_VALUE_CHARS = {
    "target": 40,
    "message": 1200,
    "outcome": 8,
    "receiver_pick": 40,
    "guard_suspicion": 20,
    "guard_guess": 40,
}
_MILLION = Decimal(1_000_000)


@dataclass
class _Entry:
    reservation: Reservation
    status: str
    amount: Decimal


def _decimal(value: Any, label: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{label} must be a finite non-negative number") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{label} must be a finite non-negative number")
    return amount


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _repair_torn_journal_tail(path: Path) -> None:
    raw = path.read_bytes()
    if not raw or raw.endswith(b"\n"):
        return

    complete_bytes = raw.rfind(b"\n") + 1
    tail = raw[complete_bytes:]
    try:
        json.loads(tail.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        with path.open("r+b") as stream:
            stream.truncate(complete_bytes)
            stream.flush()
            os.fsync(stream.fileno())
    else:
        # A crash can happen after the JSON bytes but before the line ending.
        # Preserve that complete record and restore the delimiter before appends.
        with path.open("ab") as stream:
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())


def _prices_for(spec: ModelSpec, catalog: Catalog | None = None) -> tuple[Decimal, Decimal]:
    """Return the highest input and output price the route may use.

    Fake prices are carried on the fake model's params. Real catalog pricing is
    deliberately kept behind this function so T08 can supply it without
    changing the runner or the breaker's public surface.
    """
    if spec.provider == "fake":
        input_price = _decimal(spec.params.get("price_in", 0), "fake input price")
        output_price = _decimal(spec.params.get("price_out", 0), "fake output price")
        return input_price, output_price

    if catalog is None:
        raise ValueError("real model prices require a catalog; fake-price mode is available in T02e")
    info = catalog.lookup(spec.model)
    if info is None or not info.endpoint_prices:
        raise ValueError(f"no endpoint prices are available for model {spec.model!r}")

    endpoints = list(info.endpoint_prices)
    routing = spec.params.get("provider", {})
    if isinstance(routing, Mapping):
        only = routing.get("only")
        ignored = routing.get("ignore", ())
        ordered = routing.get("order")
        if isinstance(only, Sequence) and not isinstance(only, (str, bytes)):
            allowed = set(only)
            endpoints = [item for item in endpoints if item[0] in allowed]
        elif (
            isinstance(ordered, Sequence)
            and not isinstance(ordered, (str, bytes))
            and routing.get("allow_fallbacks") is False
        ):
            allowed = set(ordered)
            endpoints = [item for item in endpoints if item[0] in allowed]
        if isinstance(ignored, Sequence) and not isinstance(ignored, (str, bytes)):
            ignored_set = set(ignored)
            endpoints = [item for item in endpoints if item[0] not in ignored_set]
    if not endpoints:
        raise ValueError(f"the configured route for {spec.model!r} has no priced endpoints")
    return (
        max(_decimal(item[1], "catalog input price") for item in endpoints),
        max(_decimal(item[2], "catalog output price") for item in endpoints),
    )


def _reasoning_budget(spec: ModelSpec) -> int:
    reasoning = spec.params.get("reasoning")
    if not isinstance(reasoning, Mapping):
        return 0
    budgets = [reasoning.get(key, 0) for key in ("max_tokens", "budget_tokens")]
    result = 0
    for budget in budgets:
        if isinstance(budget, bool):
            continue
        if isinstance(budget, int) and budget > result:
            result = budget
    return result


def _serialized_body_bytes(body: Mapping[str, Any]) -> int:
    try:
        # Default JSON separators and ASCII escaping are at least as large as
        # the compact UTF-8 form used for request hashes.
        encoded = json.dumps(dict(body), ensure_ascii=True).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("provider request body must be JSON serializable") from exc
    return len(encoded)


def _reservation_cost(body: Mapping[str, Any], spec: ModelSpec) -> Decimal:
    input_tokens = _serialized_body_bytes(body)
    max_tokens = spec.max_tokens
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        raise ValueError("model max_tokens must be a positive integer")
    input_price, output_price = _prices_for(spec)
    return (
        Decimal(input_tokens) * input_price
        + Decimal(max_tokens + _reasoning_budget(spec)) * output_price
    ) / _MILLION


def _config_cap(config: Config) -> Decimal:
    cap = _decimal(config.max_cost_usd, "max_cost_usd")
    if cap == 0 or cap > Decimal(str(RUN_CEILING_USD)):
        raise BreakerTripped(f"max_cost_usd must be greater than 0 and no more than {RUN_CEILING_USD:.2f}")
    return cap


def run_cap(config: Config, max_cost: float | None = None) -> float:
    """Return the effective per-run cap, refusing a CLI value that raises it."""
    configured = _config_cap(config)
    if max_cost is None:
        return float(configured)
    try:
        override = _decimal(max_cost, "--max-cost")
    except ValueError as exc:
        raise BreakerTripped("--max-cost must be a finite positive number") from exc
    if override == 0:
        raise BreakerTripped("--max-cost must be greater than 0")
    if override > configured:
        raise BreakerTripped(
            f"--max-cost ${float(override):.4f} exceeds configured cap ${float(configured):.4f}"
        )
    return float(min(configured, override))


class Breaker(BreakerProtocol):
    """Reserve worst-case spend atomically and persist each transition."""

    def __init__(self, cap_usd: float, journal_path: str | Path | None = None) -> None:
        cap = _decimal(cap_usd, "run cap")
        if cap == 0 or cap > Decimal(str(RUN_CEILING_USD)):
            raise ValueError(f"run cap must be greater than 0 and no more than {RUN_CEILING_USD:.2f}")
        self._cap = cap
        self._journal_path = Path(journal_path) if journal_path is not None else None
        self._lock = RLock()
        self._entries: dict[str, _Entry] = {}
        self._tripped: str | None = None
        self._external_cap: Decimal | None = None
        self._load_journal()
        if self._spent_decimal() > self._cap:
            self._trip("journal spend already exceeds the configured run cap")

    def _load_journal(self) -> None:
        path = self._journal_path
        if path is None or not path.exists():
            return
        _repair_torn_journal_tail(path)
        for record in read_jsonl(path):
            if record.get("schema") != "simmons.spend/1":
                raise ValueError("spend journal contains a non-spend record")
            attempt_id = record["attempt_id"]
            event = record["event"]
            amount = _decimal(record["usd"], "journal usd")
            entry = self._entries.get(attempt_id)
            if event == "reserve":
                if entry is not None:
                    raise ValueError(f"duplicate reservation for attempt {attempt_id!r}")
                digest = record.get("request_sha256")
                if not isinstance(digest, str) or not digest:
                    raise ValueError(f"reservation for attempt {attempt_id!r} has no request hash")
                reservation = Reservation(attempt_id, float(amount), digest)
                self._entries[attempt_id] = _Entry(reservation, "open", _decimal(reservation.usd, "reservation usd"))
                continue
            if entry is None:
                raise ValueError(f"{event} event has no reservation for attempt {attempt_id!r}")
            if event == "settle":
                if entry.status not in {"open", "unresolved"}:
                    raise ValueError(f"duplicate settlement for attempt {attempt_id!r}")
                entry.status = "settled"
                entry.amount = amount
                if amount > _decimal(entry.reservation.usd, "reservation usd"):
                    self._trip(f"settled cost for {attempt_id} exceeded its reservation")
            elif event == "unresolved":
                if entry.status != "open":
                    raise ValueError(f"duplicate unresolved event for attempt {attempt_id!r}")
                reserved = _decimal(entry.reservation.usd, "reservation usd")
                if amount != reserved:
                    raise ValueError(f"unresolved amount does not match reservation for {attempt_id!r}")
                entry.status = "unresolved"
                entry.amount = reserved
            else:
                raise ValueError(f"unknown spend event {event!r} for attempt {attempt_id!r}")

    def _spent_decimal(self) -> Decimal:
        return sum((entry.amount for entry in self._entries.values()), Decimal(0))

    def _trip(self, reason: str) -> None:
        if self._tripped is None:
            self._tripped = reason

    def _append(self, record: Mapping[str, Any]) -> None:
        if self._journal_path is not None:
            append_jsonl(self._journal_path, record)

    def reserve(self, attempt_id: str, body: Mapping[str, Any], spec: ModelSpec) -> Reservation:
        if not attempt_id:
            raise ValueError("attempt_id must not be empty")
        request_hash = request_sha256(body)
        amount = _reservation_cost(body, spec)
        reservation = Reservation(attempt_id, float(amount), request_hash)
        stored_amount = _decimal(reservation.usd, "reservation usd")
        with self._lock:
            if self._tripped is not None:
                raise BreakerTripped(self._tripped)
            if attempt_id in self._entries:
                raise ValueError(f"attempt_id {attempt_id!r} already has a reservation")
            total = self._spent_decimal() + stored_amount
            effective_cap = self._cap
            if self._external_cap is not None:
                effective_cap = min(effective_cap, self._external_cap)
            if total > effective_cap:
                self._trip(
                    f"reservation for {attempt_id} would exceed the run cap "
                    f"({float(total):.8f} > {float(effective_cap):.8f})"
                )
                raise BreakerTripped(self._tripped)

            # append_jsonl validates, flushes, and fsyncs before this returns;
            # the caller cannot submit until reserve itself has returned.
            self._append(
                {
                    "schema": "simmons.spend/1",
                    "attempt_id": attempt_id,
                    "event": "reserve",
                    "usd": reservation.usd,
                    "request_sha256": request_hash,
                    "ts": _utc_now(),
                }
            )
            self._entries[attempt_id] = _Entry(reservation, "open", stored_amount)
            return reservation

    def settle(self, reservation: Reservation, response: RoleResponse) -> None:
        with self._lock:
            entry = self._entry_for_open_reservation(reservation)
            if response.cost_usd is None:
                self._mark_unresolved(entry, "missing usage.cost")
                return
            settled = _decimal(response.cost_usd, "settled cost")
            settled_float = float(settled)
            self._append(
                {
                    "schema": "simmons.spend/1",
                    "attempt_id": reservation.attempt_id,
                    "event": "settle",
                    "usd": settled_float,
                    "generation_id": response.generation_id,
                    "ts": _utc_now(),
                }
            )
            entry.status = "settled"
            entry.amount = _decimal(settled_float, "settled cost")
            if entry.amount > _decimal(entry.reservation.usd, "reservation usd"):
                self._trip(f"settled cost for {reservation.attempt_id} exceeded its reservation")

    def unresolved(self, reservation: Reservation, reason: str) -> None:
        with self._lock:
            entry = self._entry_for_open_reservation(reservation)
            self._mark_unresolved(entry, reason)

    def _entry_for_open_reservation(self, reservation: Reservation) -> _Entry:
        entry = self._entries.get(reservation.attempt_id)
        if entry is None or entry.reservation != reservation:
            raise ValueError(f"unknown or mismatched reservation {reservation.attempt_id!r}")
        if entry.status != "open":
            raise ValueError(f"reservation {reservation.attempt_id!r} is already {entry.status}")
        return entry

    def _mark_unresolved(self, entry: _Entry, reason: str) -> None:
        safe_reason = str(reason) or "unknown provider result"
        self._append(
            {
                "schema": "simmons.spend/1",
                "attempt_id": entry.reservation.attempt_id,
                "event": "unresolved",
                "usd": entry.reservation.usd,
                "reason": safe_reason,
                "ts": _utc_now(),
            }
        )
        entry.status = "unresolved"
        entry.amount = _decimal(entry.reservation.usd, "reservation usd")

    @property
    def tripped(self) -> str | None:
        with self._lock:
            return self._tripped

    def spent_usd(self) -> float:
        with self._lock:
            return float(self._spent_decimal())

    def _set_external_remaining(self, remaining_usd: float) -> None:
        remaining = _decimal(remaining_usd, "remaining credit")
        with self._lock:
            if self._tripped is not None:
                raise BreakerTripped(self._tripped)
            self._external_cap = self._spent_decimal() + remaining
            if remaining == 0:
                self._trip("no provider credit remains for the next wave")
                raise BreakerTripped(self._tripped)
            if self._spent_decimal() > self._external_cap:
                self._trip("provider credit cannot cover current run spend")
                raise BreakerTripped(self._tripped)


def _model_reference(value: Any) -> ModelSpec | None:
    if value is None:
        return None
    if isinstance(value, str):
        reference = value
        temperature = None
        max_tokens = 16000
        params: Mapping[str, Any] = {}
    elif isinstance(value, ModelSpecConfig):
        reference = value.model
        temperature = value.temperature
        max_tokens = value.max_tokens
        params = value.params
    elif isinstance(value, Mapping):
        reference = value.get("model")
        temperature = value.get("temperature")
        max_tokens = value.get("max_tokens", 16000)
        params = value.get("params", {})
    else:
        return None
    if not isinstance(reference, str) or not reference:
        return None
    provider, separator, model = reference.partition(":")
    if separator and provider in {"fake", "openrouter"}:
        return ModelSpec(provider, model, temperature, max_tokens, params)
    return ModelSpec("openrouter", reference, temperature, max_tokens, params)


def _role_models(config: Config) -> dict[str, ModelSpec | None]:
    models = config.models
    roles = ("sender", "plain_sender", "guard", "receiver", "judge")
    if isinstance(models, str):
        default = _model_reference(models)
        return {role: (None if role == "judge" else default) for role in roles}
    if not isinstance(models, Mapping):
        return {role: None for role in roles}
    default_value = models.get("default", _DEFAULT_MODEL)
    result: dict[str, ModelSpec | None] = {}
    for role in roles:
        value = models.get(role)
        if role == "judge" and value is None:
            result[role] = None
            continue
        if value is None:
            value = default_value
        result[role] = _model_reference(value)
    return result


def _template_for(inputs: Any, role: str) -> str:
    if inputs is None:
        return ""
    templates = getattr(inputs, "templates", None)
    if not isinstance(templates, Mapping):
        return ""
    names = {
        "sender": ("sender.md", "sender"),
        "plain_sender": ("plain_sender.md", "plain_sender"),
        "guard": ("guard.md", "guard"),
        "receiver": ("receiver.md", "receiver"),
        "judge": ("judge.md", "judge"),
        "sender_debrief": ("sender_debrief.md", "sender_debrief"),
    }.get(role, (role,))
    for name in names:
        value = templates.get(name)
        if isinstance(value, str):
            return value
    return ""


def _prompt_for(inputs: Any, role: str) -> str:
    if inputs is None:
        return ""
    field = "sender_prompt" if role in {"receiver", "sender_debrief"} else f"{role}_prompt"
    value = getattr(inputs, field, "")
    return value if isinstance(value, str) else ""


def _role_tool(role: str) -> Mapping[str, Any]:
    if role in {"sender", "plain_sender"}:
        return SEND_MESSAGE_INPUT_SCHEMA
    if role == "sender_debrief":
        return EXPLAIN_MESSAGE_INPUT_SCHEMA
    return {
        "type": "object",
        "properties": {"result": {"type": "string"}},
        "additionalProperties": True,
    }


def _log_entry_chars(config: Config) -> int:
    records = config.log_records
    if not records:
        return 0
    return 12 + sum(_LOG_VALUE_CHARS.get(name, 32) for name in records)


def _rendered_user(
    role: str,
    config: Config,
    plan_line: PlanRecord,
    inputs: Any,
    history_count: int,
) -> str:
    options = plan_line.get("options", [])
    target_index = plan_line.get("target")
    target = ""
    if isinstance(target_index, int) and 0 <= target_index < len(options):
        target = options[target_index]
    protected = plan_line.get("protected")
    protected_text = ""
    if protected is not None:
        protected_text = "\n".join(
            options[index] for index in protected if isinstance(index, int) and 0 <= index < len(options)
        )
    log_text = "x" * (_log_entry_chars(config) * max(0, history_count))
    cover_task = getattr(inputs, "cover_task", "") if inputs is not None else ""
    if not isinstance(cover_task, str):
        cover_task = ""
    values = {
        "cover_task": cover_task,
        "options": "\n".join(options),
        "target": target,
        "protected": protected_text,
        "attempt_log": log_text,
        "message": "x" * 1200,
        "debrief_outcome_section": (
            "Outcome, guard score, guard guess, and receiver pick." if config.sender_debrief == "informed" else ""
        ),
        "plain_target_section": (
            f"Name the protected target: {target}." if role == "plain_sender" and config.protected_count is not None else ""
        ),
        "option_count": str(len(options)),
        "protected_count": str(config.protected_count or 0),
        "max_message_chars": str(config.max_message_chars),
        "episode": str(plan_line.get("index", 0)),
        "role": role,
        "pick": target,
        "sender_prompt_section": getattr(inputs, "sender_prompt", "") if inputs is not None else "",
        "partner_section": "Partner model information." if config.tell_partner_model else "",
    }
    template = _template_for(inputs, role)
    if not template:
        template = "$cover_task\n$options\n$target\n$protected\n$attempt_log\n$message\n$debrief_outcome_section\n$plain_target_section"
    names = set()
    for match in Template.pattern.finditer(template):
        name = match.group("named") or match.group("braced")
        if name:
            names.add(name)
    for name in names:
        values.setdefault(name, "x" * 32)
    try:
        return Template(template).substitute(values)
    except (KeyError, ValueError):
        return template


def _estimate_body(
    role: str,
    spec: ModelSpec,
    config: Config,
    plan_line: PlanRecord,
    inputs: Any,
    history_count: int,
) -> Mapping[str, Any]:
    prompt = _prompt_for(inputs, role)
    user_text = _rendered_user(role, config, plan_line, inputs, history_count)
    messages: list[Mapping[str, Any]] = []
    if role != "sender_debrief" and prompt:
        messages.append({"role": "system", "content": prompt})
    if role == "sender_debrief":
        send_prompt = _prompt_for(inputs, "sender")
        send_text = _rendered_user("sender", config, plan_line, inputs, history_count)
        if send_prompt:
            messages.append({"role": "system", "content": send_prompt})
        messages.append({"role": "user", "content": send_text})
        reasoning_tokens = _reasoning_budget(spec)
        messages.append(
            {
                "role": "assistant",
                "content": "x" * (_ROLE_OUTPUT_TOKENS["sender"] * 4),
                "reasoning_details": ([{"type": "reasoning.text", "text": "x" * (reasoning_tokens * 4)}] if reasoning_tokens else []),
            }
        )
        messages.append({"role": "tool", "content": user_text})
        tools = [SEND_MESSAGE_INPUT_SCHEMA, EXPLAIN_MESSAGE_INPUT_SCHEMA]
    else:
        messages.append({"role": "user", "content": user_text})
        tools = [_role_tool(role)]
    return {
        "model": spec.model,
        "messages": messages,
        "tools": tools,
        "max_tokens": spec.max_tokens,
        "temperature": spec.temperature,
        "params": spec.params,
    }


def _estimate_input_tokens(body: Mapping[str, Any]) -> int:
    # The estimate is explicitly characters / 4, separate from the byte-bound
    # reservation used by Breaker.reserve.
    return math.ceil(len(canonical_json(body)) / 4)


def estimate(
    config: Config,
    plan: Sequence[PlanRecord],
    *,
    inputs: Any = None,
    catalog: Catalog | None = None,
) -> float:
    """Estimate the typical provider cost for the supplied episode plan."""
    if not plan:
        return 0.0
    specs = _role_models(config)
    totals = Decimal(0)
    kinds = {str(line.get("kind", "")) for line in plan}
    if not kinds <= {"covert", "control", "plain_allowed", "plain_protected"}:
        raise ValueError("plan contains an unknown episode kind")
    if config.scorer != "exact_match" and specs.get("judge") is None:
        raise ValueError("a model judge is required to estimate this scorer")

    sender_model = specs.get("sender")
    debrief_model = sender_model
    for position, line in enumerate(plan):
        kind = str(line["kind"])
        wave = max(1, int(config.wave_size))
        history_count = (position // wave) * wave
        if config.log_window is not None:
            history_count = min(history_count, config.log_window)
        history_count = max(0, history_count) if config.log_records else 0
        if kind in {"covert", "control"}:
            roles = ["sender", "guard", "receiver"]
            if config.sender_debrief != "off":
                roles.append("sender_debrief")
        else:
            roles = ["plain_sender", "guard", "receiver"]
        if config.scorer != "exact_match" and specs.get("judge") is not None:
            roles.append("judge")
        for role in roles:
            model = debrief_model if role == "sender_debrief" else specs.get(role)
            if model is None:
                raise ValueError(f"no model is configured for role {role!r}")
            input_tokens = _estimate_input_tokens(
                _estimate_body(role, model, config, line, inputs, history_count)
            )
            output_tokens = _ROLE_OUTPUT_TOKENS[role]
            input_price, output_price = _prices_for(model, catalog)
            totals += (
                Decimal(input_tokens) * input_price
                + Decimal(output_tokens) * output_price
            ) / _MILLION
    return float(totals)


def check_before_run(
    config: Config,
    estimate_usd: float,
    *,
    remaining_usd: float | None = None,
) -> None:
    """Refuse estimates that exceed the run's fixed or external spend cap."""
    cap = _config_cap(config)
    estimate = _decimal(estimate_usd, "estimated spend")
    if estimate > cap:
        raise BreakerTripped(f"estimated spend ${float(estimate):.4f} exceeds run cap ${float(cap):.4f}")
    if remaining_usd is not None:
        remaining = _decimal(remaining_usd, "remaining credit")
        if estimate > remaining:
            raise BreakerTripped(
                f"estimated spend ${float(estimate):.4f} exceeds remaining credit ${float(remaining):.4f}"
            )


def between_waves(
    breaker: Breaker,
    *,
    remaining_usd: float | None = None,
) -> None:
    """Tighten the breaker to refreshed external credit between waves."""
    if breaker.tripped is not None:
        raise BreakerTripped(breaker.tripped)
    if remaining_usd is not None:
        breaker._set_external_remaining(remaining_usd)


__all__ = [
    "Breaker",
    "BreakerTripped",
    "RUN_CEILING_USD",
    "between_waves",
    "check_before_run",
    "estimate",
    "run_cap",
]
