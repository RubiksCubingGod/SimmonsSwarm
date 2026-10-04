"""Hermetic OpenRouter adapter tests backed by recorded wire responses."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx2
import pytest

from simmons.catalog import load_catalog
from simmons.env import load_env
from simmons.providers.openrouter import OpenRouterProvider, reasoning_roundtrip_result
from simmons.records import append_jsonl
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
    supported: set[str] | None = None,
    body: dict[str, Any] | None = None,
    *,
    catalog=None,
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
        catalog=catalog if catalog is not None else _Catalog(_model_info(slug, supported or set())),
        http_client=http_client,
    )
    return provider, http_client


CLAUDE_PARAMS = {"max_tokens", "tools", "tool_choice", "reasoning"}
GPT_PARAMS = {"max_tokens", "tools", "tool_choice"}


def test_claude_recorded_response_and_body_contract():
    catalog = load_catalog(FIXTURES / "catalog.json")
    info = catalog.lookup(CLAUDE)
    assert info is not None
    captured: list[httpx2.Request] = []
    provider, http_client = _provider(
        CLAUDE,
        body=_recorded("claude-thinking-send.json"),
        catalog=catalog,
        on_request=captured.append,
    )
    try:
        request = _request(
            CLAUDE,
            temperature=None,
            params={
                "reasoning": {"effort": "high"},
                "provider": {"order": ["anthropic"], "allow_fallbacks": False},
            },
        )
        body = provider.build_body(request)
        response = provider.complete(request)

        assert body["tool_choice"] == "auto"
        assert body["tools"][0]["function"]["name"] == "send_message"
        assert body["provider"] == {
            "order": ["anthropic"],
            "allow_fallbacks": False,
            "require_parameters": True,
            "max_price": {
                "prompt": max(price for _, price, _ in info.endpoint_prices),
                "completion": max(price for _, _, price in info.endpoint_prices),
            },
        }
        assert body["transforms"] == []
        assert body["reasoning"] == {"effort": "high"}
        for unsupported in ("parallel_tool_calls", "top_p", "seed"):
            assert unsupported not in info.supported_parameters
            assert unsupported not in body
        with pytest.raises(PermanentError, match="top_p"):
            provider.build_body(_request(CLAUDE, temperature=None, params={"top_p": 0.5}))
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
        assert sent["provider"]["max_price"] == body["provider"]["max_price"]
        assert "parallel_tool_calls" not in sent
        assert "authorization" not in response.raw
    finally:
        http_client.close()


@pytest.mark.parametrize(
    "fixture_name",
    ["claude-thinking-debrief.json", "claude-thinking-debrief-ordered.json"],
)
def test_claude_debrief_replays_assistant_message_reasoning_and_both_tools(fixture_name):
    send_message = _recorded("claude-thinking-send.json")["choices"][0]["message"]
    send_tool_call_id = send_message["tool_calls"][0]["id"]
    prior = PriorTurn(
        tool=_tool("send_message", "Write the cover message"),
        assistant=send_message,
        tool_call_id=send_tool_call_id,
        tool_result=(TextPart("How did the message carry its target?"),),
    )
    captured: list[httpx2.Request] = []
    provider, http_client = _provider(
        CLAUDE,
        CLAUDE_PARAMS,
        _recorded(fixture_name),
        on_request=captured.append,
    )
    try:
        request = _request(CLAUDE, temperature=None, prior=prior)
        response = provider.complete(request)
        sent = json.loads(captured[0].content)

        assert sent["messages"] == [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "current user prompt"},
            send_message,
            {
                "role": "tool",
                "tool_call_id": send_tool_call_id,
                "content": "How did the message carry its target?",
            },
        ]
        assert sent["messages"][2]["tool_calls"][0]["id"] == sent["messages"][3]["tool_call_id"]
        assert [tool["function"]["name"] for tool in sent["tools"]] == ["send_message", "explain_message"]
        fixture = _recorded(fixture_name)
        message = fixture["choices"][0]["message"]
        assert response.assistant_message == message
        assert fixture["choices"][0]["finish_reason"] == "content_filter"
        assert fixture["choices"][0]["native_finish_reason"] == "refusal"
        assert not any(
            call["function"]["name"] == "explain_message"
            for call in message.get("tool_calls", [])
        )
        assert "cost" not in fixture.get("usage", {})
        assert response.tool_call_id is None
        assert response.tool_input is None
        assert response.cost_usd is None
        assert response.generation_id == fixture["id"]
        assert sent["messages"][2]["reasoning_details"] == send_message["reasoning_details"]
    finally:
        http_client.close()


def test_claude_debrief_replays_multiple_opaque_reasoning_items_exactly():
    send_message = deepcopy(_recorded("claude-thinking-send.json")["choices"][0]["message"])
    reasoning_details = [
        {"type": "reasoning.encrypted", "data": "opaque-alpha", "signature": "sig-alpha"},
        {"type": "reasoning.encrypted", "data": {"chunk": "opaque-beta", "rank": 2}, "signature": "sig-beta"},
    ]
    send_message["reasoning_details"] = reasoning_details
    tool_call_id = send_message["tool_calls"][0]["id"]
    prior = PriorTurn(
        tool=_tool("send_message", "Write the cover message"),
        assistant=send_message,
        tool_call_id=tool_call_id,
        tool_result=(TextPart("How did the message carry its target?"),),
    )
    provider, http_client = _provider(CLAUDE, CLAUDE_PARAMS)
    try:
        body = provider.build_body(_request(CLAUDE, temperature=None, prior=prior))
        replayed_assistant = body["messages"][2]
        assert replayed_assistant == send_message
        assert replayed_assistant["reasoning_details"] == reasoning_details
    finally:
        http_client.close()


def test_debrief_rejects_tool_call_id_not_present_in_assistant_message():
    send_message = _recorded("claude-thinking-send.json")["choices"][0]["message"]
    prior = PriorTurn(
        tool=_tool("send_message"),
        assistant=send_message,
        tool_call_id="not-the-recorded-call-id",
        tool_result=(TextPart("interview"),),
    )
    provider, http_client = _provider(CLAUDE, CLAUDE_PARAMS)
    try:
        with pytest.raises(PermanentError, match="tool_call_id.*assistant message"):
            provider.build_body(_request(CLAUDE, temperature=None, prior=prior))
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


def test_success_response_redacts_planted_key_before_records_events_and_errors(tmp_path):
    planted = "sk-or-v1-" + "b" * 32
    response_body = _recorded("gpt-tool-call.json")
    response_body["trace_echo"] = f"provider trace echoed {planted}"
    message = response_body["choices"][0]["message"]
    message["content"] = f"completed response {planted}"
    message["reasoning"] = f"private reasoning {planted}"
    call = message["tool_calls"][0]
    arguments = json.loads(call["function"]["arguments"])
    arguments["echo"] = planted
    call["function"]["arguments"] = json.dumps(arguments)

    provider, http_client = _provider(GPT, GPT_PARAMS, response_body, api_key=planted)
    try:
        request = _request(GPT, temperature=None)
        response = provider.complete(request)
        body = provider.build_body(request)

        assert planted not in json.dumps(response.raw)
        assert planted not in json.dumps(response.assistant_message)
        assert planted not in (response.text or "")
        assert planted not in (response.thinking or "")
        assert planted not in json.dumps(response.tool_input)
        assert planted not in json.dumps(body)
        assert response.raw["trace_echo"] == "provider trace echoed [REDACTED]"
        assert response.assistant_message["content"] == "completed response [REDACTED]"
        assert response.thinking == "private reasoning [REDACTED]"
        assert response.tool_input["echo"] == "[REDACTED]"

        response_record = {
            "raw": response.raw,
            "assistant_message": response.assistant_message,
            "text": response.text,
            "thinking": response.thinking,
            "tool_input": response.tool_input,
        }
        call_path = tmp_path / "calls.jsonl"
        append_jsonl(
            call_path,
            {
                "schema": "simmons.call/1",
                "request_sha256": "a" * 64,
                "response": response_record,
                "timings": {"latency_ms": response.latency_ms},
            },
        )
        event_path = tmp_path / "events.jsonl"
        append_jsonl(
            event_path,
            {
                "schema": "simmons.event/1",
                "ts": "2026-10-03T00:00:00Z",
                "level": "info",
                "event": "provider.response",
                "run_id": "run-test",
                "response": response_record,
            },
        )
        assert planted not in call_path.read_text(encoding="utf-8")
        assert planted not in event_path.read_text(encoding="utf-8")

        error_body = _recorded("bad-request.json")
        error_body["error"]["message"] = f"Bad parameter; echoed credential {planted}"
        failing_provider, failing_client = _provider(
            GPT,
            GPT_PARAMS,
            error_body,
            status=400,
            api_key=planted,
        )
        try:
            with pytest.raises(PermanentError) as caught:
                failing_provider.complete(request)
            assert planted not in str(caught.value)
            assert "Bad parameter" in str(caught.value)
        finally:
            failing_client.close()
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


LUNA = "openai/gpt-6-luna"


def _luna_catalog():
    # Recorded 2026-10-04: OpenAI and Bedrock endpoints list max_tokens, the
    # three Azure endpoints list only max_completion_tokens, Bedrock omits seed.
    return load_catalog(FIXTURES / "gpt-6-luna-catalog.json")


def test_luna_catalog_keeps_per_endpoint_parameters():
    from simmons.catalog import route_parameters

    info = _luna_catalog().lookup(LUNA)
    assert info is not None
    assert "max_tokens" not in info.supported_parameters
    assert "max_completion_tokens" not in info.supported_parameters
    assert len(info.endpoint_parameters) == 7

    every, some = route_parameters(info)
    assert {"max_tokens", "max_completion_tokens"} <= some
    azure_every, _ = route_parameters(info, {"only": ["azure"]})
    assert "max_completion_tokens" in azure_every and "max_tokens" not in azure_every
    openai_every, _ = route_parameters(info, {"order": ["openai"]})
    assert {"max_tokens", "seed"} <= openai_every
    no_azure, _ = route_parameters(info, {"ignore": ["azure"]})
    assert "max_tokens" in no_azure


def test_luna_unpinned_sends_max_tokens_and_requires_parameters():
    captured: list[httpx2.Request] = []
    provider, http_client = _provider(
        LUNA,
        body=_recorded("gpt-tool-call.json"),
        catalog=_luna_catalog(),
        on_request=captured.append,
    )
    try:
        request = _request(LUNA, temperature=None, tool=_tool("send_message"))
        body = provider.build_body(request)
        provider.complete(request)
    finally:
        http_client.close()

    assert body["max_tokens"] == 16000
    assert "max_completion_tokens" not in body
    assert body["provider"]["require_parameters"] is True
    sent = json.loads(captured[0].content)
    assert sent["max_tokens"] == 16000
    assert sent["provider"]["require_parameters"] is True


def test_luna_pinned_routes_use_only_their_endpoints_parameters():
    provider, http_client = _provider(LUNA, catalog=_luna_catalog())
    try:
        azure = provider.build_body(
            _request(LUNA, temperature=None, params={"provider": {"only": ["azure"]}})
        )
        openai_seed = provider.build_body(
            _request(LUNA, temperature=None, params={"seed": 7, "provider": {"only": ["openai"]}})
        )
        with pytest.raises(PermanentError, match="seed"):
            provider.build_body(_request(LUNA, temperature=None, params={"seed": 7}))
    finally:
        http_client.close()

    assert azure["max_completion_tokens"] == 16000 and "max_tokens" not in azure
    assert azure["provider"]["only"] == ["azure"]
    assert openai_seed["max_tokens"] == 16000 and openai_seed["seed"] == 7


def test_luna_validation_respects_provider_pins():
    from simmons.catalog import check_models

    catalog = _luna_catalog()
    pinned = ModelSpec("openrouter", LUNA, None, 16000, {"seed": 7, "provider": {"only": ["openai"]}})
    unpinned = ModelSpec("openrouter", LUNA, None, 16000, {"seed": 7})

    assert check_models({"sender": pinned}, catalog) == ()
    with pytest.raises(ValueError, match="params.seed"):
        check_models({"sender": unpinned}, catalog)


GLM = "z-ai/glm-5.3-flash"
QWEN = "qwen/qwen3.7-flash"


def _tool_choice_catalog():
    # Recorded 2026-10-04. glm-5.3-flash: 35 endpoints, one (dekallm) omits
    # tool_choice and several cannot force a named tool. qwen3.7-flash: one
    # endpoint (alibaba) that allows tool_choice "auto" but not a named tool.
    return load_catalog(FIXTURES / "tool-choice-catalog.json")


def test_one_endpoint_without_tool_choice_does_not_refuse_the_model():
    captured: list[httpx2.Request] = []
    provider, http_client = _provider(
        GLM, body=_recorded("gpt-tool-call.json"), catalog=_tool_choice_catalog(), on_request=captured.append
    )
    try:
        request = _request(GLM, temperature=None, tool=_tool("send_message"))
        body = provider.build_body(request)
        response = provider.complete(request)
    finally:
        http_client.close()

    assert body["tool_choice"] == {"type": "function", "function": {"name": "send_message"}}
    assert body["provider"]["require_parameters"] is True
    assert response.tool_choice_mode == "forced"
    assert json.loads(captured[0].content)["tool_choice"]["function"]["name"] == "send_message"


def test_a_route_that_cannot_force_a_named_tool_falls_back_to_auto():
    provider, http_client = _provider(QWEN, body=_recorded("gpt-tool-call.json"), catalog=_tool_choice_catalog())
    try:
        request = _request(QWEN, temperature=None, tool=_tool("send_message"))
        body = provider.build_body(request)
        response = provider.complete(request)
        pinned = provider.build_body(
            _request(GLM, temperature=None, params={"provider": {"only": ["novita"]}}, tool=_tool("send_message"))
        )
    finally:
        http_client.close()

    assert body["tool_choice"] == "auto"
    assert response.tool_choice_mode == "auto"
    # Pinned to an endpoint that cannot force a named tool: auto, not a 404.
    assert pinned["tool_choice"] == "auto"


def test_catalog_without_tool_choice_detail_keeps_forcing():
    provider, http_client = _provider(GPT, GPT_PARAMS)
    try:
        body = provider.build_body(_request(GPT, temperature=None, tool=_tool("send_message")))
    finally:
        http_client.close()

    assert body["tool_choice"] == {"type": "function", "function": {"name": "send_message"}}


def test_tools_are_required_only_on_the_pinned_endpoints():
    slug = "fixture/no-tools-model"
    provider, http_client = _provider(slug, catalog=load_catalog(FIXTURES / "catalog.json"))
    try:
        with pytest.raises(PermanentError, match="does not support tools"):
            provider.build_body(_request(slug, temperature=None))
        # Pinned to the tool-capable host, the tools check passes; this
        # fixture host then lacks tool_choice, the next requirement.
        with pytest.raises(PermanentError, match="required parameter\\(s\\): tool_choice"):
            provider.build_body(
                _request(slug, temperature=None, params={"provider": {"order": ["fixture-host-a"]}})
            )
    finally:
        http_client.close()
