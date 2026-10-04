"""Run one Simmons episode through the role, gate, decode, and debrief steps."""

from __future__ import annotations

import json
import random
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import ValidationError

from . import events
from .records import append_jsonl, request_sha256
from .roles import request_for, validate_for_tool
from .types import (
    BreakerTripped,
    BudgetError,
    EpisodeContext,
    EpisodeState,
    GuardView,
    ModelSpec,
    PermanentError,
    Pick,
    PlanContext,
    PlainSenderView,
    ReceiverView,
    RoleRequest,
    RoleResponse,
    SenderDebriefView,
    SenderView,
    Target,
    TransientError,
    Usage,
)
from .visibility import build_view

_ATTEMPT_LIMIT = 6
_NETWORK_PAUSE_SECONDS = 30
_NETWORK_PAUSE_LIMIT_SECONDS = 15 * 60


class CallFailure(Exception):
    """A role call that exhausted its retry rules."""

    def __init__(
        self,
        role: str,
        kind: str,
        message: str,
        *,
        attempts: int,
        permanent: bool = False,
    ) -> None:
        super().__init__(message)
        self.role = role
        self.kind = kind
        self.attempts = attempts
        self.permanent = permanent


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _option_ref(options: Sequence[str], index: int | None) -> dict[str, Any] | None:
    if index is None or not 0 <= index < len(options):
        return None
    return {"index": index, "value": options[index]}


def _resolve_model(role: str, ctx: EpisodeContext) -> ModelSpec:
    key = "sender" if role == "sender_debrief" else role
    model = ctx.resolved_models.get(key)
    if model is None:
        raise CallFailure(role, "provider", f"no model is configured for role {role!r}", attempts=0, permanent=True)
    return model


def _provider(role: str, spec: ModelSpec, ctx: EpisodeContext) -> Any:
    provider = ctx.providers.get(role) or ctx.providers.get(spec.provider)
    if provider is None:
        raise CallFailure(role, "provider", f"provider {spec.provider!r} is unavailable", attempts=0, permanent=True)
    return provider


def _request_record(request: RoleRequest, digest: str) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema": "simmons.request/1",
        "request_sha256": digest,
        "model": asdict(request.model),
        "system": request.system,
        "user": [asdict(part) for part in request.user],
        "tool": asdict(request.tool),
        "attempt": request.attempt,
        "request_id": request.request_id,
        "role": request.role,
    }
    if request.prior is not None:
        record["prior"] = {
            "tool": asdict(request.prior.tool),
            "assistant": dict(request.prior.assistant),
            "tool_call_id": request.prior.tool_call_id,
            "tool_result": [asdict(part) for part in request.prior.tool_result],
        }
    return record


def _append_request(ctx: EpisodeContext, request: RoleRequest, digest: str) -> None:
    if ctx.request_log_path:
        append_jsonl(ctx.request_log_path, _request_record(request, digest))


def _emit(ctx: EpisodeContext, name: str, **fields: Any) -> None:
    if ctx.events_log_path:
        events.emit(name, run_id=ctx.run_id, path=ctx.events_log_path, **fields)


def _zero_response(request: RoleRequest) -> RoleResponse:
    """Settle a provider rejection that returned no billable generation."""
    return RoleResponse(
        tool_input=None,
        text=None,
        thinking=None,
        reasoning_details=None,
        parse_error=None,
        usage=Usage(input_tokens=0, output_tokens=0),
        cost_usd=0.0,
        generation_id=None,
        served_by=None,
        model_reported=request.model.model,
        finish_reason="error",
        tool_choice_mode="forced",
        latency_ms=0,
        raw={},
    )


def _response_json(response: RoleResponse) -> dict[str, Any]:
    return {
        "tool_input": dict(response.tool_input) if response.tool_input is not None else None,
        "text": response.text,
        "thinking": response.thinking,
        "reasoning_details": list(response.reasoning_details) if response.reasoning_details is not None else None,
        "parse_error": response.parse_error,
        "usage": asdict(response.usage),
        "cost_usd": response.cost_usd,
        "generation_id": response.generation_id,
        "served_by": response.served_by,
        "model_reported": response.model_reported,
        "finish_reason": response.finish_reason,
        "tool_choice_mode": response.tool_choice_mode,
        "latency_ms": response.latency_ms,
        "raw": dict(response.raw),
        "assistant_message": dict(response.assistant_message) if response.assistant_message is not None else None,
        "tool_call_id": response.tool_call_id,
    }


def _append_call(ctx: EpisodeContext, digest: str, response: Mapping[str, Any], elapsed_ms: int) -> None:
    if ctx.calls_log_path:
        append_jsonl(
            ctx.calls_log_path,
            {
                "schema": "simmons.call/1",
                "request_sha256": digest,
                "response": dict(response),
                "timings": {"elapsed_ms": elapsed_ms},
            },
        )


def _retry_delay(attempt: int, retry_after_s: float | None) -> float:
    base = min(60.0, 2.0 ** max(0, attempt - 1))
    jittered = random.SystemRandom().uniform(base * 0.5, base)
    requested = max(0.0, retry_after_s or 0.0)
    return min(60.0, max(jittered, requested))


def _safe_exception(exc: Exception) -> str:
    text = str(exc).strip()
    return text[:2000] if text else type(exc).__name__


def _call_role(
    role: str,
    state: EpisodeState,
    ctx: EpisodeContext,
    *,
    protected_options: Sequence[str] | None = None,
    attempt_prefix: str | None = None,
) -> tuple[RoleResponse, Mapping[str, Any], Any, Any, int]:
    """Make, account for, and validate one logical role call."""
    spec = _resolve_model(role, ctx)
    provider = _provider(role, spec, ctx)
    view = build_view(role, state, ctx.config, ctx.inputs)
    request = request_for(role, state, ctx.config, ctx.inputs, spec)
    network_started: float | None = None
    network_pauses = 0
    counted_attempts = 0
    while counted_attempts < _ATTEMPT_LIMIT:
        provider_attempt = counted_attempts + 1
        current = replace(request, attempt=provider_attempt)
        try:
            body = provider.build_body(current)
            digest = request_sha256(current)
            attempt_id = f"{ctx.run_id}:{attempt_prefix or state.episode}:{role}:a{provider_attempt}:n{network_pauses}"
            reservation = ctx.breaker.reserve(attempt_id, body, spec) if ctx.breaker else None
            try:
                _append_request(ctx, current, digest)
                _emit(ctx, "call_start", episode=state.episode, role=role, attempt=provider_attempt, request_sha256=digest)
            except Exception:
                if reservation is not None:
                    ctx.breaker.settle(reservation, _zero_response(current))
                raise
            started = time.monotonic()
            response: RoleResponse | None = None
            try:
                response = provider.complete(current)
            except TransientError as exc:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                if reservation is not None:
                    ctx.breaker.settle(reservation, _zero_response(current))
                _append_call(ctx, digest, {"error": _safe_exception(exc), "kind": "network_down" if exc.network_down else "transient"}, elapsed_ms)
                if exc.network_down:
                    now = time.monotonic()
                    if network_started is None:
                        network_started = now
                    if now - network_started >= _NETWORK_PAUSE_LIMIT_SECONDS:
                        raise CallFailure(role, "provider", "network remained unavailable for 15 minutes", attempts=counted_attempts)
                    network_pauses += 1
                    _emit(ctx, "network_pause", episode=state.episode, role=role, request_sha256=digest, pause_seconds=_NETWORK_PAUSE_SECONDS)
                    print("network down, paused 30s", file=sys.stderr, flush=True)
                    time.sleep(_NETWORK_PAUSE_SECONDS)
                    if time.monotonic() - network_started >= _NETWORK_PAUSE_LIMIT_SECONDS:
                        raise CallFailure(role, "provider", "network remained unavailable for 15 minutes", attempts=counted_attempts)
                    _emit(ctx, "network_resume", episode=state.episode, role=role, request_sha256=digest)
                    continue
                counted_attempts += 1
                _emit(ctx, "call_retry", episode=state.episode, role=role, attempt=provider_attempt, kind="transient", message=_safe_exception(exc))
                if counted_attempts < _ATTEMPT_LIMIT:
                    time.sleep(_retry_delay(counted_attempts, exc.retry_after_s))
                continue
            except PermanentError as exc:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                if reservation is not None:
                    ctx.breaker.settle(reservation, _zero_response(current))
                _append_call(ctx, digest, {"error": _safe_exception(exc), "kind": "permanent"}, elapsed_ms)
                raise CallFailure(role, "provider", _safe_exception(exc), attempts=counted_attempts + 1, permanent=True) from exc
            except BudgetError as exc:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                if reservation is not None:
                    ctx.breaker.settle(reservation, _zero_response(current))
                _append_call(ctx, digest, {"error": _safe_exception(exc), "kind": "budget"}, elapsed_ms)
                raise CallFailure(role, "provider", _safe_exception(exc), attempts=counted_attempts + 1) from exc
            except Exception as exc:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                if reservation is not None:
                    ctx.breaker.unresolved(reservation, f"provider call raised {type(exc).__name__}")
                _append_call(ctx, digest, {"error": _safe_exception(exc), "kind": "provider"}, elapsed_ms)
                raise CallFailure(role, "provider", _safe_exception(exc), attempts=counted_attempts + 1) from exc

            assert response is not None
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if reservation is not None:
                ctx.breaker.settle(reservation, response)
            _append_call(ctx, digest, _response_json(response), elapsed_ms)
            _emit(ctx, "call_end", episode=state.episode, role=role, attempt=provider_attempt, request_sha256=digest, latency_ms=response.latency_ms)
            counted_attempts += 1
            if response.parse_error or response.tool_input is None:
                if response.finish_reason == "length":
                    raise CallFailure(role, "length", "provider stopped at the token limit without a usable tool call", attempts=counted_attempts)
                validation_error: Exception = ValueError(response.parse_error or "response did not contain a tool call")
            else:
                try:
                    validated = validate_for_tool(
                        role,
                        response.tool_input,
                        view,
                        current.tool,
                        protected_options=protected_options,
                    )
                    return response, validated, view, current, counted_attempts
                except (ValidationError, ValueError, TypeError) as exc:
                    if response.finish_reason == "length":
                        raise CallFailure(role, "length", "provider stopped at the token limit without a usable tool call", attempts=counted_attempts) from exc
                    validation_error = exc
            _emit(ctx, "call_retry", episode=state.episode, role=role, attempt=provider_attempt, kind="format", message=_safe_exception(validation_error))
            if counted_attempts < _ATTEMPT_LIMIT:
                continue
            raise CallFailure(role, "format", _safe_exception(validation_error), attempts=counted_attempts)
        except BreakerTripped:
            raise
        except CallFailure:
            raise
        except Exception as exc:
            if isinstance(exc, (ValueError, TypeError, KeyError)):
                raise CallFailure(role, "format", _safe_exception(exc), attempts=counted_attempts) from exc
            raise
    raise CallFailure(role, "format", "role call exhausted its six-attempt budget", attempts=counted_attempts)


def _role_result(role: str, spec: ModelSpec, response: RoleResponse, output: Mapping[str, Any], digest: str, attempts: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        "role": role,
        "model": f"{spec.provider}:{spec.model}",
        "model_reported": response.model_reported,
        "served_by": response.served_by,
        "generation_id": response.generation_id,
        "tool_choice_mode": response.tool_choice_mode,
        "output": dict(output),
        "thinking": response.thinking,
        "discarded_text": response.text,
        "reasoning_details": list(response.reasoning_details) if response.reasoning_details is not None else None,
        "attempts": attempts,
        "usage": asdict(response.usage),
        "cost_usd": response.cost_usd,
        "latency_ms": response.latency_ms,
        "request_sha256": digest,
    }
    return result


def _prior_reasoning(response: RoleResponse) -> dict[str, Any]:
    details = list(response.reasoning_details or ())
    types = [str(item.get("type", "unknown")) for item in details]
    formats = [str(item.get("format", "unknown")) for item in details]
    readable_chars = sum(
        len(str(item.get("text", "")))
        for item in details
        if isinstance(item.get("text"), str)
    )
    return {
        "items": len(details),
        "types": types,
        "formats": formats,
        "readable_chars": readable_chars,
        "passed_back": response.assistant_message is not None and response.tool_call_id is not None,
    }


def _attempt_record(ctx: EpisodeContext, index: int, state: EpisodeState, record: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {"schema": "simmons.attempt/1", "episode": index}
    selectable: dict[str, Any] = {
        "target": state.target.value,
        "message": state.message,
        "outcome": record.get("outcome"),
        "receiver_pick": record.get("pick", {}).get("value") if isinstance(record.get("pick"), Mapping) else None,
        "guard_suspicion": record.get("guard", {}).get("suspicion") if isinstance(record.get("guard"), Mapping) else None,
        "guard_guess": record.get("guard", {}).get("guess", {}).get("value") if isinstance(record.get("guard"), Mapping) and isinstance(record.get("guard", {}).get("guess"), Mapping) else None,
    }
    for field in ctx.config.log_records:
        if field in selectable:
            values[field] = selectable[field]
    return values


def run_episode(line: Mapping[str, Any], ctx: EpisodeContext) -> dict[str, Any]:
    """Run one plan line and return a schema-valid episode record."""
    index = int(line["index"])
    kind = str(line["kind"])
    options = tuple(str(value) for value in line["options"])
    target_index = int(line["target"])
    protected_value = line.get("protected")
    protected = None if protected_value is None else frozenset(int(value) for value in protected_value)
    target = Target(payload=ctx.config.payload, value=options[target_index], index=target_index)
    state = EpisodeState(
        episode=index,
        kind=kind,
        options=options,
        target=target,
        protected=protected,
        attempt_log=ctx.attempt_log,
    )
    started_at = _utc_now()
    before = ctx.breaker.spent_usd() if ctx.breaker is not None else 0.0
    calls: dict[str, Any] = {
        "sender": None,
        "sender_debrief": None,
        "guard": None,
        "receiver": None,
        "shadow_receiver": None,
        "judge": None,
    }
    messages: list[dict[str, Any]] = []
    guard_record: dict[str, Any] | None = None
    pick_ref: dict[str, Any] | None = None
    shadow_pick_ref: dict[str, Any] | None = None
    score_record: dict[str, Any] | None = None
    debrief_error: str | None = None
    shadow_error: str | None = None
    outcome = "ERROR"
    error: dict[str, Any] | None = None
    message: str | None = None
    sender_response: RoleResponse | None = None
    sender_spec: ModelSpec | None = None
    receiver_pick: Pick | None = None
    guard_pick: Pick | None = None
    suspicion: float | None = None

    def call_record(role: str, response: RoleResponse, output: Mapping[str, Any], request: RoleRequest, attempts: int) -> dict[str, Any]:
        spec = _resolve_model(role, ctx)
        return _role_result(role, spec, response, output, request_sha256(request), attempts)

    try:
        sender_role = "sender" if kind in {"covert", "control"} else "plain_sender"
        sender_spec = _resolve_model(sender_role, ctx)
        protected_options = None
        if sender_role == "plain_sender" and protected is not None:
            protected_options = tuple(options[position] for position in sorted(protected))
        response, output, _view, request, attempts = _call_role(
            sender_role, state, ctx, protected_options=protected_options
        )
        sender_response = response
        message = str(output["message"])
        calls["sender"] = call_record(sender_role, response, output, request, attempts)
        state = replace(state, message=message, sender_response=response)
        messages.append({"n": 0, "from": sender_role, "to": "receiver", "sent": message, "rewritten": None, "delivered": None})

        guard_response, guard_output, guard_view, guard_request, guard_attempts = _call_role("guard", state, ctx)
        assert isinstance(guard_view, GuardView)
        calls["guard"] = call_record("guard", guard_response, guard_output, guard_request, guard_attempts)
        suspicion_value = guard_output.get("suspicion")
        suspicion = float(suspicion_value) if isinstance(suspicion_value, (int, float)) and not isinstance(suspicion_value, bool) else None
        raw_guess = guard_output.get("guess")
        if isinstance(raw_guess, str):
            for position, option in enumerate(options):
                from .text import normalize
                if normalize(raw_guess) == normalize(option):
                    guard_pick = Pick(payload=ctx.config.payload, value=option, index=position)
                    break
        blocked = (
            suspicion is not None and suspicion >= ctx.config.block_threshold
            if ctx.config.guard_output == "suspicion"
            else guard_output.get("verdict") == "block"
        )
        guard_record = {
            "mode": ctx.config.guard_output,
            "suspicion": suspicion,
            "verdict": guard_output.get("verdict"),
            "threshold": ctx.config.block_threshold if ctx.config.guard_output == "suspicion" else None,
            "blocked": bool(blocked),
            "guess": _option_ref(options, guard_pick.index if guard_pick else None),
        }
        if blocked:
            outcome = "CATCH"
            messages[0]["delivered"] = None
        else:
            messages[0]["delivered"] = message
            receiver_state = replace(state, guard_suspicion=suspicion, guard_guess=guard_pick)
            receiver_response, receiver_output, receiver_view, receiver_request, receiver_attempts = _call_role("receiver", receiver_state, ctx)
            assert isinstance(receiver_view, ReceiverView)
            receiver_pick = ctx.payload.parse_pick(receiver_output, receiver_view)
            calls["receiver"] = call_record("receiver", receiver_response, receiver_output, receiver_request, receiver_attempts)
            receiver_state = replace(receiver_state, receiver_pick=receiver_pick)
            score = ctx.scorer.score(target, receiver_pick, None)
            score_record = {"scorer": ctx.scorer.name, "value": score.value, "hit": score.hit, "detail": score.detail}
            outcome = "HIT" if score.hit else "MISS"
            state = replace(receiver_state, outcome=outcome)

        if blocked and ctx.config.shadow_decode and kind in {"covert", "control"}:
            shadow_state = replace(state, guard_suspicion=suspicion, guard_guess=guard_pick, outcome="CATCH")
            try:
                shadow_response, shadow_output, shadow_view, shadow_request, shadow_attempts = _call_role("receiver", shadow_state, ctx, attempt_prefix=f"{index}-shadow")
                assert isinstance(shadow_view, ReceiverView)
                shadow_pick = ctx.payload.parse_pick(shadow_output, shadow_view)
                shadow_pick_ref = _option_ref(options, shadow_pick.index)
                calls["shadow_receiver"] = call_record("receiver", shadow_response, shadow_output, shadow_request, shadow_attempts)
            except BreakerTripped:
                raise
            except CallFailure as exc:
                shadow_error = f"{exc.kind}: {exc}"
            except Exception as exc:
                shadow_error = f"format: {_safe_exception(exc)}"

        if kind in {"covert", "control"} and ctx.config.sender_debrief != "off" and sender_response is not None:
            debrief_state = replace(
                state,
                message=message,
                sender_response=sender_response,
                guard_suspicion=suspicion,
                guard_guess=guard_pick,
                receiver_pick=receiver_pick,
                outcome=outcome,
            )
            try:
                debrief_response, debrief_output, _debrief_view, debrief_request, debrief_attempts = _call_role("sender_debrief", debrief_state, ctx, attempt_prefix=f"{index}-debrief")
                calls["sender_debrief"] = call_record("sender_debrief", debrief_response, debrief_output, debrief_request, debrief_attempts)
                calls["sender_debrief"]["prior_reasoning"] = _prior_reasoning(sender_response)
            except BreakerTripped:
                raise
            except CallFailure as exc:
                debrief_error = f"{exc.kind}: {exc}"
            except Exception as exc:
                debrief_error = f"format: {_safe_exception(exc)}"
    except BreakerTripped:
        raise
    except CallFailure as exc:
        outcome = "ERROR"
        error = {"role": exc.role, "kind": exc.kind, "message": str(exc), "attempts": exc.attempts}
        if exc.permanent:
            error["permanent"] = True
    except Exception as exc:
        outcome = "ERROR"
        error = {"role": "harness", "kind": "format", "message": _safe_exception(exc)}

    finished_at = _utc_now()
    log_seen: dict[str, Any] = {
        "sender": [int(item["episode"]) for item in ctx.attempt_log] if kind in {"covert", "control"} and ctx.config.log_records else None,
        "receiver": [int(item["episode"]) for item in ctx.attempt_log] if ctx.config.receiver_sees_log and ctx.config.log_records else None,
    }
    record: dict[str, Any] = {
        "schema": "simmons.episode/1",
        "run_id": ctx.run_id,
        "index": index,
        "wave": ctx.wave,
        "kind": kind,
        "target": {"index": target_index, "value": target.value},
        "protected": None if protected is None else [_option_ref(options, position) for position in sorted(protected)],
        "log_seen": log_seen,
        "calls": calls,
        "messages": messages,
        "guard": guard_record,
        "pick": pick_ref,
        "shadow_pick": shadow_pick_ref,
        "debrief_error": debrief_error,
        "shadow_error": shadow_error,
        "score": score_record,
        "outcome": outcome,
        "error": error,
        "cost_usd": max(0.0, (ctx.breaker.spent_usd() - before)) if ctx.breaker is not None else 0.0,
        "started_at": started_at,
        "finished_at": finished_at,
    }
    if outcome in {"HIT", "MISS"}:
        record["pick"] = _option_ref(options, receiver_pick.index if receiver_pick else None)
    if shadow_error is not None:
        record["shadow_error"] = shadow_error
    return record


__all__ = ["CallFailure", "run_episode"]
