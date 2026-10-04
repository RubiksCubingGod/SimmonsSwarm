"""OpenRouter's OpenAI-compatible Chat Completions adapter."""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from .base import register_provider
from ..env import load_env
from ..types import (
    BudgetError,
    FilePart,
    ModelInfo,
    PermanentError,
    ProviderStatus,
    RoleRequest,
    RoleResponse,
    TextPart,
    TransientError,
    Usage,
)

_BASE_URL = "https://openrouter.ai/api/v1"
_TIMEOUT_SECONDS = 120.0
_ALLOWED_PARAMS = {"reasoning", "provider", "top_p", "seed"}
_ALLOWED_PROVIDER_PARAMS = {
    "order",
    "only",
    "ignore",
    "allow_fallbacks",
    "data_collection",
    "quantizations",
}
_SDK_BODY_FIELDS = {
    "model",
    "messages",
    "max_tokens",
    "max_completion_tokens",
    "temperature",
    "tool_choice",
    "tools",
    "top_p",
    "seed",
}
_SECRET_PATTERN = re.compile(r"sk-or-v1-[A-Za-z0-9_-]+", re.IGNORECASE)
_BEARER_PATTERN = re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+")


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        result = dump(mode="json", exclude_unset=True)
        if isinstance(result, Mapping):
            return result
    if hasattr(value, "__dict__"):
        return vars(value)
    return {}


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _jsonable(dump(mode="json", exclude_unset=True))
    return str(value)


def _redact(value: Any, api_key: str | None = None) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in {"authorization", "proxy-authorization"}:
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = _redact(item, api_key)
        return redacted
    if isinstance(value, (list, tuple)):
        return [_redact(item, api_key) for item in value]
    if isinstance(value, str):
        if api_key:
            value = value.replace(api_key, "[REDACTED]")
        value = _BEARER_PATTERN.sub(r"\1[REDACTED]", value)
        return _SECRET_PATTERN.sub("[REDACTED]", value)
    return value


def _error_body(error: APIStatusError) -> Mapping[str, Any]:
    body = getattr(error, "body", None)
    if isinstance(body, Mapping):
        return body
    response = getattr(error, "response", None)
    try:
        parsed = response.json() if response is not None else None
    except Exception:
        parsed = None
    return parsed if isinstance(parsed, Mapping) else {}


def _error_message(error: APIStatusError, api_key: str | None) -> str:
    body = _error_body(error)
    value: Any = body.get("error", body)
    if isinstance(value, Mapping):
        value = value.get("message") or value.get("detail") or value.get("code")
    message = value if isinstance(value, str) else "OpenRouter returned an HTTP error"
    return str(_redact(message, api_key))[:1000]


def _retry_after(error: APIStatusError) -> float | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {}) or {}
    value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except (TypeError, ValueError):
        try:
            parsed = parsedate_to_datetime(str(value))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _tool_definition(tool: Any) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.input_schema),
        },
    }


def _parts_text(parts: Sequence[Any], *, purpose: str) -> str:
    rendered: list[str] = []
    for part in parts:
        if isinstance(part, TextPart):
            rendered.append(part.text)
        elif isinstance(part, FilePart):
            raise PermanentError(f"OpenRouter v1 does not support file parts in {purpose}")
        else:
            raise PermanentError(f"OpenRouter received an unsupported part in {purpose}")
    return "\n\n".join(rendered)


def _tool_choice(model: str, tool_name: str) -> tuple[str, str | Mapping[str, Any]]:
    if model.startswith("anthropic/"):
        return "auto", "auto"
    return "forced", {"type": "function", "function": {"name": tool_name}}


def _json_arguments(value: Any) -> tuple[Mapping[str, Any] | None, str | None]:
    if isinstance(value, Mapping):
        return value, None
    if not isinstance(value, str):
        return None, "tool arguments were not a JSON object"
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError) as exc:
        return None, f"tool arguments were malformed JSON: {exc.msg if isinstance(exc, json.JSONDecodeError) else 'invalid input'}"
    if not isinstance(parsed, Mapping):
        return None, "tool arguments were valid JSON but not an object"
    return parsed, None


def _tool_response(message: Mapping[str, Any], expected_name: str) -> tuple[Mapping[str, Any] | None, str | None, str | None]:
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return None, None, None

    first_matching: tuple[Mapping[str, Any], Mapping[str, Any]] | None = None
    for call in tool_calls:
        if not isinstance(call, Mapping):
            continue
        function = call.get("function")
        if not isinstance(function, Mapping) or function.get("name") != expected_name:
            continue
        if first_matching is None:
            first_matching = (call, function)
        parsed, parse_error = _json_arguments(function.get("arguments"))
        if parsed is not None:
            return parsed, None, _optional_string(call.get("id"))

    if first_matching is None:
        return None, None, None
    call, function = first_matching
    _, parse_error = _json_arguments(function.get("arguments"))
    return None, parse_error, _optional_string(call.get("id"))


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _token_count(usage: Mapping[str, Any], *names: str) -> int:
    for name in names:
        value = usage.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            return max(0, value)
    return 0


def reasoning_roundtrip_result(
    send_response: RoleResponse,
    with_reasoning: RoleResponse,
    without_reasoning: RoleResponse,
) -> dict[str, Any]:
    """Classify whether replayed reasoning contributes to interview input.

    The caller performs and accounts for both interview requests. This pure
    helper compares their billed input-token counts and returns the shape used
    by ``run.json`` preflight metadata.
    """
    details = send_response.reasoning_details or ()
    formats = [
        item["format"]
        for item in details
        if isinstance(item, Mapping) and isinstance(item.get("format"), str)
    ]
    delta = with_reasoning.usage.input_tokens - without_reasoning.usage.input_tokens
    return {
        "carried": None if not details else delta > 0,
        "input_token_delta": delta,
        "send_reasoning_tokens": send_response.usage.reasoning_tokens,
        "formats": formats,
    }


def _retry_metadata(body: Mapping[str, Any]) -> Mapping[str, Any]:
    # APIStatusError.body is the decoded inner object; raw HTTP JSON keeps the
    # outer {"error": ...} envelope.
    error = body.get("error", body)
    if not isinstance(error, Mapping):
        return {}
    metadata = error.get("metadata")
    return metadata if isinstance(metadata, Mapping) else {}


@register_provider("openrouter")
class OpenRouterProvider:
    name = "openrouter"

    def __init__(self, **params: Any) -> None:
        """Create the adapter.

        ``catalog``, ``client`` and ``http_client`` are injectable for the
        harness and recorded-response tests. No request is made at
        construction time.
        """
        allowed = {"api_key", "catalog", "client", "http_client", "repo_root"}
        unexpected = sorted(set(params) - allowed)
        if unexpected:
            raise TypeError(f"unexpected OpenRouter provider parameter(s): {', '.join(unexpected)}")

        self.catalog = params.get("catalog")
        self.api_key = params.get("api_key")
        if self.api_key is None:
            load_env(params.get("repo_root"))
            self.api_key = os.environ.get("OPENROUTER_API_KEY")
        injected_client = params.get("client")
        http_client = params.get("http_client")
        self.client = injected_client
        if self.client is None and self.api_key:
            client_options: dict[str, Any] = {
                "api_key": self.api_key,
                "base_url": _BASE_URL,
                "timeout": _TIMEOUT_SECONDS,
                "max_retries": 0,
                "default_headers": {"X-Title": "SimmonsSwarm"},
            }
            if http_client is not None:
                client_options["http_client"] = http_client
            self.client = OpenAI(**client_options)

    def build_body(self, request: RoleRequest) -> Mapping[str, Any]:
        """Build a complete request body without network access or mutation."""
        info = self._model_info(request.model.model)
        if not info.supports_tools:
            raise PermanentError(f"OpenRouter model {request.model.model} does not support tools")

        configured = dict(request.model.params)
        unknown = sorted(set(configured) - _ALLOWED_PARAMS)
        if unknown:
            raise PermanentError(f"unsupported OpenRouter parameter(s): {', '.join(unknown)}")
        provider_params = configured.get("provider", {})
        if not isinstance(provider_params, Mapping):
            raise PermanentError("OpenRouter parameter provider must be an object")
        unknown_provider = sorted(set(provider_params) - _ALLOWED_PROVIDER_PARAMS)
        if unknown_provider:
            raise PermanentError(f"unsupported OpenRouter provider parameter(s): {', '.join(unknown_provider)}")

        supports = info.supported_parameters
        required_params = {"tools", "tool_choice"}
        missing_required = sorted(required_params - supports)
        if missing_required:
            raise PermanentError(
                f"OpenRouter model {request.model.model} does not support required parameter(s): "
                f"{', '.join(missing_required)}"
            )

        max_token_field = "max_tokens" if "max_tokens" in supports else None
        if max_token_field is None and "max_completion_tokens" in supports:
            max_token_field = "max_completion_tokens"
        if max_token_field is None:
            raise PermanentError(f"OpenRouter model {request.model.model} does not support a token limit")

        body: dict[str, Any] = {"model": request.model.model}
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append(
            {
                "role": "user",
                "content": _parts_text(request.user, purpose="user message"),
            }
        )
        if request.prior is not None:
            prior_calls = request.prior.assistant.get("tool_calls")
            if not isinstance(prior_calls, list) or not any(
                isinstance(call, Mapping)
                and call.get("id") == request.prior.tool_call_id
                and isinstance(call.get("function"), Mapping)
                and call["function"].get("name") == request.prior.tool.name
                for call in prior_calls
            ):
                raise PermanentError("prior tool_call_id does not match the assistant message")
            # Keep this map and every opaque value in it unchanged. In
            # particular, Anthropic reasoning_details must survive the replay.
            messages.append(dict(request.prior.assistant))
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": request.prior.tool_call_id,
                    "content": _parts_text(request.prior.tool_result, purpose="tool result"),
                }
            )
        body["messages"] = messages

        tools: list[dict[str, Any]] = []
        if request.prior is not None:
            tools.append(_tool_definition(request.prior.tool))
        if all(tool["function"]["name"] != request.tool.name for tool in tools):
            tools.append(_tool_definition(request.tool))
        body["tools"] = tools

        mode, choice = _tool_choice(request.model.model, request.tool.name)
        body["tool_choice"] = choice
        body[max_token_field] = request.model.max_tokens

        if request.model.temperature is not None:
            self._require_supported(request.model.model, "temperature", supports)
            body["temperature"] = request.model.temperature
        for name in ("reasoning", "top_p", "seed"):
            if name in configured:
                self._require_supported(request.model.model, name, supports)
                body[name] = configured[name]

        input_price, output_price = self._max_prices(info)
        # OpenRouter preferences survive, while harness-owned limits always win.
        body["provider"] = {
            **dict(provider_params),
            "require_parameters": True,
            "max_price": {"prompt": input_price, "completion": output_price},
        }
        body["transforms"] = []
        return body

    def complete(self, request: RoleRequest) -> RoleResponse:
        if self.client is None:
            raise PermanentError("OPENROUTER_API_KEY is not set")
        body = self.build_body(request)
        mode, _ = _tool_choice(request.model.model, request.tool.name)
        started = time.monotonic()
        try:
            sdk_body = {key: value for key, value in body.items() if key in _SDK_BODY_FIELDS}
            openrouter_body = {key: value for key, value in body.items() if key not in _SDK_BODY_FIELDS}
            if openrouter_body:
                sdk_body["extra_body"] = openrouter_body
            result = self.client.chat.completions.create(**sdk_body)
        except APITimeoutError:
            raise TransientError("OpenRouter request timed out") from None
        except APIConnectionError:
            raise TransientError("OpenRouter connection failed") from None
        except APIStatusError as error:
            raise self._map_status_error(error) from None
        except (TimeoutError, OSError):
            raise TransientError("OpenRouter request timed out or disconnected") from None

        raw = _redact(_jsonable(_as_mapping(result)), self.api_key)
        choices = raw.get("choices") if isinstance(raw, Mapping) else None
        choice = choices[0] if isinstance(choices, list) and choices else {}
        choice = choice if isinstance(choice, Mapping) else {}
        message = choice.get("message", {})
        message = message if isinstance(message, Mapping) else {}

        # Preserve the exact returned assistant message, including opaque fields.
        assistant_message = _redact(dict(message), self.api_key)
        tool_input, parse_error, tool_call_id = _tool_response(message, request.tool.name)
        reasoning = message.get("reasoning_details")
        reasoning_details = tuple(reasoning) if isinstance(reasoning, list) else None
        reasoning_text = message.get("reasoning")
        if not isinstance(reasoning_text, str):
            reasoning_text = None
        usage_data = raw.get("usage", {})
        usage_data = usage_data if isinstance(usage_data, Mapping) else {}
        prompt_details = usage_data.get("prompt_tokens_details", {})
        prompt_details = prompt_details if isinstance(prompt_details, Mapping) else {}
        completion_details = usage_data.get("completion_tokens_details", {})
        completion_details = completion_details if isinstance(completion_details, Mapping) else {}
        usage = Usage(
            input_tokens=_token_count(usage_data, "prompt_tokens", "input_tokens"),
            output_tokens=_token_count(usage_data, "completion_tokens", "output_tokens"),
            reasoning_tokens=_token_count(completion_details, "reasoning_tokens"),
            cache_read_tokens=_token_count(prompt_details, "cached_tokens", "cache_read_tokens"),
            cache_write_tokens=_token_count(prompt_details, "cache_write_tokens"),
        )
        cost = usage_data.get("cost")
        cost_usd = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None
        generation_id = raw.get("id") or raw.get("generation_id")
        served_by = raw.get("provider") or raw.get("served_by")
        model_reported = raw.get("model")
        finish_reason = choice.get("finish_reason")
        return RoleResponse(
            tool_input=tool_input,
            text=message.get("content") if isinstance(message.get("content"), str) else None,
            thinking=reasoning_text,
            reasoning_details=reasoning_details,
            parse_error=parse_error,
            usage=usage,
            cost_usd=cost_usd,
            generation_id=_optional_string(generation_id),
            served_by=_optional_string(served_by),
            model_reported=_optional_string(model_reported) or request.model.model,
            finish_reason=_optional_string(finish_reason) or "",
            tool_choice_mode=mode,
            latency_ms=max(0, int((time.monotonic() - started) * 1000)),
            raw=raw if isinstance(raw, Mapping) else {},
            assistant_message=assistant_message,
            tool_call_id=tool_call_id,
        )

    def check(self) -> ProviderStatus:
        if not self.api_key:
            return ProviderStatus(self.name, False, "OPENROUTER_API_KEY is not set")
        if self.client is None:
            return ProviderStatus(self.name, True, "OpenRouter client is unavailable")
        try:
            data = self.client.get("/key", cast_to=dict)
        except APIStatusError as error:
            raise self._map_status_error(error) from None
        except (APITimeoutError, APIConnectionError):
            raise TransientError("OpenRouter key check timed out or disconnected") from None
        payload = data.get("data", data) if isinstance(data, Mapping) else {}
        payload = payload if isinstance(payload, Mapping) else {}
        fields = ("label", "limit", "limit_remaining", "usage", "usage_daily", "usage_weekly", "usage_monthly")
        detail = "; ".join(f"{field}={payload[field]}" for field in fields if field in payload)
        return ProviderStatus(self.name, True, detail or "OpenRouter key accepted")

    def _model_info(self, slug: str) -> ModelInfo:
        catalog = self.catalog
        if catalog is None:
            from ..catalog import load_catalog

            catalog = load_catalog()
        info = catalog.lookup(slug)
        if info is None:
            raise PermanentError(f"OpenRouter model {slug} is not present in the cached catalog")
        return info

    @staticmethod
    def _require_supported(slug: str, parameter: str, supports: frozenset[str]) -> None:
        if parameter not in supports:
            raise PermanentError(f"OpenRouter model {slug} does not support configured parameter {parameter}")

    @staticmethod
    def _max_prices(info: ModelInfo) -> tuple[float, float]:
        if not info.endpoint_prices:
            raise PermanentError(f"OpenRouter model {info.slug} has no priced serving endpoints")
        prices = [(float(input_price), float(output_price)) for _, input_price, output_price in info.endpoint_prices]
        if any(not math.isfinite(price) or price < 0 for pair in prices for price in pair):
            raise PermanentError(f"OpenRouter model {info.slug} has invalid endpoint prices")
        return max(pair[0] for pair in prices), max(pair[1] for pair in prices)

    def _map_status_error(self, error: APIStatusError) -> Exception:
        status = error.status_code
        message = _error_message(error, self.api_key)
        if status == 402:
            if _retry_metadata(_error_body(error)).get("limit_source") == "openrouter_in_flight_budget":
                return TransientError(message, retry_after_s=_retry_after(error))
            return BudgetError(message)
        if status == 429 or status >= 500:
            return TransientError(message, retry_after_s=_retry_after(error))
        if 400 <= status < 500:
            return PermanentError(message)
        return PermanentError(message)
