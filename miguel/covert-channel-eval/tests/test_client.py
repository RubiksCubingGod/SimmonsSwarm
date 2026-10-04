import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
import requests  # noqa: E402

import config  # noqa: E402
from openrouter_client import OpenRouterClient, OpenRouterError  # noqa: E402
from spend import BudgetExceeded, SpendLedger  # noqa: E402


class FakeResponse:
    def __init__(self, status=200, body=None, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else (json.dumps(body) if body is not None else "")

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Replays queued responses or exceptions; records every request."""

    def __init__(self, posts=(), gets=()):
        self.posts = list(posts)
        self.gets = list(gets)
        self.post_calls = []
        self.get_calls = []

    def _next(self, queue):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url, headers=None, data=None, timeout=None):
        self.post_calls.append(json.loads(data))
        return self._next(self.posts)

    def get(self, url, headers=None, timeout=None):
        self.get_calls.append(url)
        return self._next(self.gets)


def ok(text="hi", cost=0.002):
    return FakeResponse(200, {
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "cost": cost},
    })


def make(posts=(), gets=(), ledger=None, route_lookup=False):
    session = FakeSession(posts, gets)
    client = OpenRouterClient(api_key="test-key", ledger=ledger, session=session,
                              route_lookup=route_lookup)
    client._sleep = lambda s: None
    return client, session


MSGS = [{"role": "user", "content": "x"}]


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404])
def test_client_errors_not_retried(status):
    client, s = make([FakeResponse(status, {"error": {"code": status}})])
    with pytest.raises(OpenRouterError, match=str(status)):
        client.chat("m/a", MSGS)
    assert len(s.post_calls) == 1


def test_429_then_success():
    client, s = make([FakeResponse(429, text="slow down"), ok("done")])
    r = client.chat("m/a", MSGS)
    assert r.text == "done" and r.attempts == 2 and len(s.post_calls) == 2
    assert s.post_calls[0]["usage"] == {"include": True}


def test_500_three_times_raises():
    client, s = make([FakeResponse(500, text="boom")] * 3)
    with pytest.raises(OpenRouterError, match="3 attempts"):
        client.chat("m/a", MSGS)
    assert len(s.post_calls) == 3


def test_read_timeout_not_retried():
    led = SpendLedger()
    client, s = make([requests.exceptions.ReadTimeout("slow"), ok()], ledger=led)
    with pytest.raises(OpenRouterError, match="read timeout"):
        client.chat("m/a", MSGS)
    assert len(s.post_calls) == 1
    assert led.to_dict()["unknown_cost_calls"] == 1


def test_connection_error_retried():
    client, s = make([requests.ConnectionError("down"),
                      requests.exceptions.ConnectTimeout("slow"), ok()])
    assert client.chat("m/a", MSGS).attempts == 3


def test_body_error_codes():
    retry_body = {"error": {"code": 502, "message": "upstream"},
                  "usage": {"cost": 0.001}}
    led = SpendLedger()
    client, s = make([FakeResponse(200, retry_body), ok(cost=0.002)], ledger=led)
    r = client.chat("m/a", MSGS, role="collab")
    assert r.cost_usd == 0.002 and r.usage["prompt_tokens"] == 3
    d = led.to_dict()
    assert d["calls"] == 2 and d["total_usd"] == pytest.approx(0.003)
    assert d["by_role"]["collab"]["calls"] == 2

    client, s = make([FakeResponse(200, {"error": {"code": 400, "message": "bad"}})])
    with pytest.raises(OpenRouterError, match="bad"):
        client.chat("m/a", MSGS)
    assert len(s.post_calls) == 1


def test_non_json_200_not_retried():
    client, s = make([FakeResponse(200, None, text="<html>")])
    with pytest.raises(OpenRouterError, match="non-JSON"):
        client.chat("m/a", MSGS)
    assert len(s.post_calls) == 1


def test_empty_content_falls_back_to_reasoning():
    body = {"choices": [{"message": {"content": "", "reasoning": "{\"a\": 1}"}}]}
    client, _ = make([FakeResponse(200, body)])
    r = client.chat("m/a", MSGS)
    assert r.text == '{"a": 1}' and r.cost_usd is None


def test_budget_exceeded_before_http():
    led = SpendLedger(cap_usd=0.01)
    led.record("m/a", "x", 0.01, None)
    client, s = make([ok()], ledger=led)
    with pytest.raises(BudgetExceeded):
        client.chat("m/a", MSGS)
    assert s.post_calls == []


def test_projected_cost_stops_second_call():
    led = SpendLedger(cap_usd=0.05)
    client, s = make([ok(cost=0.03), ok(cost=0.03)], ledger=led)
    client.chat("m/a", MSGS)
    with pytest.raises(BudgetExceeded):
        client.chat("m/a", MSGS)
    assert len(s.post_calls) == 1


class CountingLedger(SpendLedger):
    checks = 0

    def check(self, model):
        self.checks += 1
        super().check(model)


def endpoints(*param_sets):
    return FakeResponse(200, {"data": {"endpoints": [
        {"provider_name": f"p{i}", "supported_parameters": list(p)}
        for i, p in enumerate(param_sets)]}})


def test_luna_route_handling():
    gets = [endpoints(["max_tokens", "response_format"],
                      ["max_tokens"],
                      ["max_completion_tokens"])]
    led = CountingLedger()
    client, s = make([ok(), ok()], gets=gets, ledger=led, route_lookup=True)
    r = client.chat("openai/gpt-6-luna", MSGS, temperature=0.0, max_tokens=4000,
                    response_format={"type": "json_object"})
    p = s.post_calls[0]
    assert "temperature" not in p
    assert p["max_tokens"] == 4000 and p["provider"] == {"require_parameters": True}
    assert p["response_format"] == {"type": "json_object"}
    assert r.dropped_params == ["temperature"]
    client.chat("openai/gpt-6-luna", MSGS)
    assert len(s.get_calls) == 1  # cached per model
    assert led.checks == 2  # the free lookup does not go through the ledger


def test_route_renames_token_field_when_max_tokens_unsupported():
    client, s = make([ok()], gets=[endpoints(["max_completion_tokens", "temperature"])],
                     route_lookup=True)
    r = client.chat("m/a", MSGS, max_tokens=100)
    p = s.post_calls[0]
    assert p["max_completion_tokens"] == 100 and "max_tokens" not in p
    assert "provider" not in p and p["temperature"] == 0.7
    assert r.dropped_params == ["max_tokens"]


@pytest.mark.parametrize("failure", [
    requests.ConnectionError("down"),
    FakeResponse(404, {"error": "nope"}),
    FakeResponse(200, {"unexpected": True}),
])
def test_route_lookup_failure_falls_back(failure, capsys):
    client, s = make([ok(), ok()], gets=[failure], route_lookup=True)
    r = client.chat("m/a", MSGS, temperature=0.5, max_tokens=50)
    p = s.post_calls[0]
    assert p["temperature"] == 0.5 and p["max_tokens"] == 50 and "provider" not in p
    assert r.dropped_params == []
    assert "[CC_WARN]" in capsys.readouterr().out
    client.chat("m/a", MSGS)
    assert len(s.get_calls) == 1  # failed lookup is not retried


def test_missing_key(monkeypatch):
    monkeypatch.delenv(config.OPENROUTER_API_KEY_ENV, raising=False)
    with pytest.raises(OpenRouterError, match="OPENROUTER_API_KEY"):
        OpenRouterClient(session=FakeSession())


@pytest.fixture
def clean_env(monkeypatch):
    import os
    saved = dict(os.environ)  # load_dotenv writes os.environ directly
    for name in ("CC_DISABLE_DOTENV", "SIMMONS_DISABLE_DOTENV", "CC_DOTENV",
                 "CC_COLLAB_MODEL", "CC_JUDGE_MODEL", "CC_MAX_COST",
                 "CC_COLLAB_MAX_TOKENS"):
        monkeypatch.delenv(name, raising=False)
    for name in ("COLLABORATOR_MODEL", "MONITOR_MODEL", "JUDGE_MODEL"):
        monkeypatch.setattr(config, name, getattr(config, name))
    yield monkeypatch
    os.environ.clear()
    os.environ.update(saved)


def test_load_env_disabled(clean_env, tmp_path):
    env = tmp_path / "x.env"
    env.write_text("CC_COLLAB_MODEL=should/not-load\n")
    clean_env.setenv("CC_DISABLE_DOTENV", "1")
    clean_env.setenv("CC_DOTENV", str(env))
    assert config.load_env() is None
    assert "CC_COLLAB_MODEL" not in __import__("os").environ


def test_load_env_explicit_path(clean_env, tmp_path):
    env = tmp_path / "x.env"
    env.write_text("CC_COLLAB_MODEL=fake/collab\nCC_MAX_COST=\nCC_COLLAB_MAX_TOKENS=777\n")
    clean_env.setenv("CC_DOTENV", str(env))
    assert config.load_env() == str(env)
    assert config.COLLABORATOR_MODEL == "fake/collab"
    assert config.JUDGE_MODEL == "fake/collab"
    cfg = config.ExperimentConfig()
    assert cfg.max_cost_usd is None and cfg.collaborator_max_tokens == 777


def test_config_defaults(clean_env):
    cfg = config.ExperimentConfig()
    assert cfg.max_cost_usd == 2.0 and cfg.collaborator_max_tokens == 32000
    assert cfg.monitor_max_tokens == 6000 and cfg.judge_max_tokens == 12000
    assert cfg.interview_max_tokens == 6000
    assert not hasattr(config, "INTERVIEW_MODEL")



def test_load_env_leaves_environment_clean():
    import os
    assert "CC_COLLAB_MAX_TOKENS" not in os.environ
