"""Hermetic OpenRouter adapter tests backed by recorded wire responses."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx2
import pytest

from simmons.env import load_env
from simmons.providers.openrouter import OpenRouterProvider, reasoning_roundtrip_result
from simmons.types import (
    BudgetError,
    ModelInfo,
    ModelSpec,
    PermanentError,
    PriorTurn,
    RoleRequest,
    TextPart,
    ToolSpec,
    TransientError,
)

FIXTURES = Path(__file__).parent / "fixtures" / "openrouter"
API_KEY = "local-test-key-without-provider-prefix"
CLAUDE = "anthropic/claude-sonnet-5.5"
GPT = "openai/gpt-4-turbo"


class _Catalog:
    def __init__(self, *models: ModelInfo) -> None:
        self.models = {model.slug: model for model in models}

    def lookup(self, slug: str) -> ModelInfo | None:
        return self.models.get(slug)

    def age_seconds(self) -> float:
        return 0.0


def _model_info(slug: str, supported: set[str]) -> ModelInfo:
    return ModelInfo(
        slug=slug,
        context_length=200_000,
        supports_tools=True,
        endpoint_prices=(("host-a", 0.25, 1.25), ("host-b", 0.40, 1.50)),
        supported_parameters=frozenset(supported),
    )


def _tool(name: str, description: str = "Return a structured answer") -> ToolSpec:
    return ToolSpec(name, description, {"type": "object", "properties": {"value": {"type": "string"}}})


def _request(
    slug: str,
    *,
    temperature: float | None = 0.2,
    params: dict[str, Any] | None = None,
    prior: PriorTurn | None = None,
    tool: ToolSpec | None = None,
) -> RoleRequest:
    return RoleRequest(
        request_id="request-test",
        role="sender_debrief" if prior else "sender",
        model=ModelSpec("openrouter", slug, temperature, 16000, params or {}),
        system="system prompt",
        user=(TextPart("current user prompt"),),
        tool=tool or _tool("explain_message" if prior else "send_message"),
        prior=prior,
        attempt=3,
    )


def _recorded(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _recorded_tool_input(name: str, tool_name: str) -> dict[str, Any]:
    response = _recorded(name)
    message = response["choices"][0]["message"]
    call = next(call for call in message.get("tool_calls", []) if call["function"]["name"] == tool_name)
    return json.loads(call["function"]["arguments"])


def _provider(
    slug: str,
    supported: set[str],
    body: dict[str, Any] | None = None,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    on_request=None,
    api_key: str = API_KEY,
) -> tuple[OpenRouterProvider, httpx2.Client]:
    fixture = body or _recorded("claude-thinking-send.json")

    def respond(request: httpx2.Request) -> httpx2.Response:
        if on_request is not None:
            on_request(request)
        return httpx2.Response(status, headers=headers, json=fixture)

    transport = httpx2.MockTransport(respond)
    http_client = httpx2.Client(transport=transport)
    provider = OpenRouterProvider(
        api_key=api_key,
        catalog=_Catalog(_model_info(slug, supported)),
        http_client=http_client,
    )
    return provider, http_client


CLAUDE_PARAMS = {"max_tokens", "tools", "tool_choice", "reasoning"}
GPT_PARAMS = {"max_tokens", "tools", "tool_choice"}


def test_claude_recorded_response_and_body_contract():
    captured: list[httpx2.Request] = []
    provider, http_client = _provider(
        CLAUDE,
        CLAUDE_PARAMS,
        _recorded("claude-thinking-send.json"),
        on_request=captured.append,
    )
    try:
        request = _request(
            CLAUDE,
            temperature=None,
            params={
                "reasoning": {"effort": "high"},
                "provider": {"order": ["host-b"], "allow_fallbacks": False},
            },
        )
        body = provider.build_body(request)
        response = provider.complete(request)

        assert body["tool_choice"] == "auto"
        assert body["tools"][0]["function"]["name"] == "send_message"
        assert body["provider"] == {
            "order": ["host-b"],
            "allow_fallbacks": False,
            "require_parameters": True,
            "max_price": {"prompt": 0.4, "completion": 1.5},
        }
        assert body["transforms"] == []
        assert body["reasoning"] == {"effort": "high"}
        assert "parallel_tool_calls" not in body
        assert "top_p" not in body and "seed" not in body
        assert response.tool_choice_mode == "auto"
        fixture = _recorded("claude-thinking-send.json")
        usage = fixture["usage"]
        message = fixture["choices"][0]["message"]
        assert response.tool_input == _recorded_tool_input("claude-thinking-send.json", "send_message")
        assert response.cost_usd == pytest.approx(usage["cost"])
        assert response.generation_id == fixture["id"]
        assert response.served_by == fixture.get("provider")
        assert response.usage.input_tokens == usage["prompt_tokens"]
        assert response.usage.output_tokens == usage["completion_tokens"]
        assert response.usage.reasoning_tokens == usage["completion_tokens_details"]["reasoning_tokens"]
        assert response.usage.cache_read_tokens == usage["prompt_tokens_details"]["cached_tokens"]
        assert response.assistant_message == message
        assert response.tool_call_id == message["tool_calls"][0]["id"]
        with_reasoning = replace(
            response,
            usage=replace(response.usage, input_tokens=response.usage.input_tokens + 80),
        )
        without_reasoning = replace(
            response,
            usage=replace(response.usage, input_tokens=response.usage.input_tokens + 3),
        )
        assert reasoning_roundtrip_result(response, with_reasoning, without_reasoning) == {
            "carried": True,
            "input_token_delta": 77,
            "send_reasoning_tokens": response.usage.reasoning_tokens,
            "formats": [item["format"] for item in message["reasoning_details"]],
        }
        equal_tokens = replace(with_reasoning, usage=without_reasoning.usage)
        assert reasoning_roundtrip_result(response, equal_tokens, without_reasoning)["carried"] is False
        no_reasoning = replace(response, reasoning_details=None)
        assert reasoning_roundtrip_result(no_reasoning, with_reasoning, without_reasoning)["carried"] is None
        assert provider.client.max_retries == 0
        assert provider.client.timeout == 120
        assert captured[0].headers["x-title"] == "SimmonsSwarm"
        sent = json.loads(captured[0].content)
        assert sent["provider"]["require_parameters"] is True
        assert sent["provider"]["max_price"] == {"prompt": 0.4, "completion": 1.5}
        assert "parallel_tool_calls" not in sent
        assert "authorization" not in response.raw
    finally:
        http_client.close()


def test_claude_debrief_replays_assistant_message_reasoning_and_both_tools():
    send_message = _recorded("claude-thinking-send.json")["choices"][0]["message"]
    prior = PriorTurn(
        tool=_tool("send_message", "Write the cover message"),
        assistant=send_message,
        tool_call_id="toolu_recorded_send",
        tool_result=(TextPart("How did the message carry its target?"),),
    )
    captured: list[httpx2.Request] = []
    provider, http_client = _provider(
        CLAUDE,
        CLAUDE_PARAMS,
        _recorded("claude-thinking-debrief.json"),
        on_request=captured.append,
    )
    try:
        request = _request(CLAUDE, temperature=None, prior=prior)
        response = provider.complete(request)
        sent = json.loads(captured[0].content)

        assert sent["messages"][1] == send_message
        assert sent["messages"][2] == {
            "role": "tool",
            "tool_call_id": "toolu_recorded_send",
            "content": "How did the message carry its target?",
        }
        assert [tool["function"]["name"] for tool in sent["tools"]] == ["send_message", "explain_message"]
        fixture = _recorded("claude-thinking-debrief.json")
        message = fixture["choices"][0]["message"]
        assert response.assistant_message == message
        assert response.tool_call_id is None
        assert response.tool_input is None
        assert response.cost_usd is None
        assert response.generation_id == fixture["id"]
        assert sent["messages"][1]["reasoning_details"] == send_message["reasoning_details"]
    finally:
        http_client.close()


def test_gpt_recorded_response_uses_forced_tool_choice():
    provider, http_client = _provider(GPT, GPT_PARAMS, _recorded("gpt-tool-call.json"))
    try:
        request = _request(GPT, temperature=None)
        body = provider.build_body(request)
        response = provider.complete(request)
        assert body["tool_choice"] == {"type": "function", "function": {"name": "send_message"}}
        assert body["max_tokens"] == 16000
        assert "max_completion_tokens" not in body
        assert response.tool_choice_mode == "forced"
        assert response.tool_input == _recorded_tool_input("gpt-tool-call.json", "send_message")
    finally:
        http_client.close()


def test_configured_unsupported_parameters_are_rejected_without_fallback():
    provider, http_client = _provider(CLAUDE, CLAUDE_PARAMS)
    try:
        with pytest.raises(PermanentError, match="top_p"):
            provider.build_body(_request(CLAUDE, temperature=None, params={"top_p": 0.5}))
        with pytest.raises(PermanentError, match="temperature"):
            provider.build_body(_request(CLAUDE, temperature=0.2))
        with pytest.raises(PermanentError, match="parallel_tool_calls"):
            provider.build_body(_request(CLAUDE, temperature=None, params={"parallel_tool_calls": False}))
    finally:
        http_client.close()


def test_billed_malformed_tool_arguments_keep_cost_and_parse_error():
    response_body = _recorded("gpt-tool-call.json")
    response_body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{bad json"
    provider, http_client = _provider(GPT, GPT_PARAMS, response_body)
    try:
        response = provider.complete(_request(GPT, temperature=None))
        assert response.tool_input is None
        assert response.parse_error and "malformed JSON" in response.parse_error
        fixture = _recorded("gpt-tool-call.json")
        assert response.cost_usd == pytest.approx(fixture["usage"]["cost"])
        assert response.generation_id == fixture["id"]
    finally:
        http_client.close()


def test_bad_request_maps_provider_message_and_redacts_a_planted_key():
    planted = "sk-or-v1-" + "a" * 32
    error_body = _recorded("bad-request.json")
    error_body["error"]["message"] = f"Bad parameter; echoed credential {planted}"
    provider, http_client = _provider(CLAUDE, CLAUDE_PARAMS, error_body, status=400, api_key=planted)
    try:
        with pytest.raises(PermanentError) as caught:
            provider.complete(_request(CLAUDE, temperature=None, params={"reasoning": {"effort": "high"}}))
        assert planted not in str(caught.value)
        assert "Bad parameter" in str(caught.value)
    finally:
        http_client.close()


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (402, {"error": {"message": "brief hold", "metadata": {"limit_source": "openrouter_in_flight_budget"}}}, TransientError),
        (402, {"error": {"message": "credit exhausted", "metadata": {"limit_source": "account"}}}, BudgetError),
        (429, {"error": {"message": "slow down"}}, TransientError),
        (503, {"error": {"message": "upstream unavailable"}}, TransientError),
    ],
)
def test_status_error_mapping(status, body, expected):
    provider, http_client = _provider(CLAUDE, CLAUDE_PARAMS, body, status=status, headers={"Retry-After": "3"})
    try:
        with pytest.raises(expected) as caught:
            provider.complete(_request(CLAUDE, temperature=None, params={"reasoning": {"effort": "high"}}))
        if status == 402 and expected is TransientError:
            assert caught.value.retry_after_s == 3
    finally:
        http_client.close()


def test_dotenv_loads_without_overriding_existing_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("SIMMONS_DISABLE_DOTENV", raising=False)
    monkeypatch.delenv("SIMMONS_AUTHOR", raising=False)
    monkeypatch.setenv("EXISTING_VALUE", "process")
    (tmp_path / ".env").write_text(
        "# comment\nOPENROUTER_API_KEY='fixture-only-secret'\nEXISTING_VALUE=file\nexport SIMMONS_AUTHOR=local\n",
        encoding="utf-8",
    )
    load_env(tmp_path)
    assert os.environ["OPENROUTER_API_KEY"] == "fixture-only-secret"
    assert os.environ["EXISTING_VALUE"] == "process"
    assert os.environ["SIMMONS_AUTHOR"] == "local"


def test_dotenv_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMMONS_DISABLE_DOTENV", "1")
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=should-not-load\n", encoding="utf-8")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    load_env(tmp_path)
    assert "OPENROUTER_API_KEY" not in os.environ
