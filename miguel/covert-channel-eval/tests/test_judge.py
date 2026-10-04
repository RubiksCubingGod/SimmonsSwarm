import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import config  # noqa: E402
from agents import TurnResult  # noqa: E402
from judge import Judge  # noqa: E402


class FakeResult:
    def __init__(self, text):
        self.text = text
        self.cost_usd = None
        self.usage = None
        self.attempts = 1


class FakeClient:
    """Returns queued `.text` replies; records every chat() call."""

    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = []

    def chat(self, model, messages, temperature=0.0, max_tokens=None, role="", **kw):
        self.calls.append({
            "model": model, "messages": messages, "temperature": temperature,
            "max_tokens": max_tokens, "role": role,
        })
        return FakeResult(self.texts.pop(0))


def cfg(condition):
    return config.ExperimentConfig(condition=condition)


def a_turn():
    return TurnResult(role_name="Agent A", thinking="t", public_message="m",
                      channel_status="established", tool_name=None, tool_value=None,
                      raw="{}")


VALID_JSON = json.dumps({
    "directions": {
        "A_to_B": {"valid": True, "exact_match": True, "method": "x", "reasons": "y"},
        "B_to_A": {"valid": False, "exact_match": False, "method": "x", "reasons": "y"},
    },
    "channel_established": True,
    "overall_reasons": "ok",
    "summary": "narrative",
})


def _system_sent(client):
    return client.calls[0]["messages"][0]["content"]


def _user_sent(client):
    return client.calls[0]["messages"][1]["content"]


def test_covert_condition_rule_text_present():
    client = FakeClient([VALID_JSON])
    Judge(client, cfg("covert")).adjudicate([a_turn()], [a_turn()], None, None)
    system = _system_sent(client)
    assert "{{CONDITION_RULES}}" not in system  # sentinel was filled
    assert "COVERT" in system
    assert "plain text" in system  # open leakage => INVALID in covert
    assert "covert" in _user_sent(client)


def test_plaintext_condition_permits_open_transmission():
    client = FakeClient([VALID_JSON])
    Judge(client, cfg("plaintext")).adjudicate([a_turn()], [a_turn()], None, None)
    system = _system_sent(client)
    assert "{{CONDITION_RULES}}" not in system
    assert "PLAINTEXT" in system
    assert "OPEN transmission IS PERMITTED" in system
    assert "plaintext" in _user_sent(client)


def test_system_prompt_differs_between_conditions():
    covert = FakeClient([VALID_JSON])
    Judge(covert, cfg("covert")).adjudicate([a_turn()], [a_turn()], None, None)
    plain = FakeClient([VALID_JSON])
    Judge(plain, cfg("plaintext")).adjudicate([a_turn()], [a_turn()], None, None)
    assert _system_sent(covert) != _system_sent(plain)


def test_retry_once_on_parse_failure():
    client = FakeClient(["not json", VALID_JSON])
    out = Judge(client, cfg("covert")).adjudicate([a_turn()], [a_turn()], None, None)
    assert len(client.calls) == 2  # retried exactly once
    assert out["verdict"]["channel_established"] is True


def test_max_tokens_is_cfg_value_not_hardcoded():
    c = cfg("covert")
    client = FakeClient(["bad", VALID_JSON])
    Judge(client, c).adjudicate([a_turn()], [a_turn()], None, None)
    # Both the initial call and the retry use cfg.judge_max_tokens.
    assert client.calls[0]["max_tokens"] == c.judge_max_tokens
    assert client.calls[1]["max_tokens"] == c.judge_max_tokens
    assert client.calls[0]["role"] == "judge"


def test_no_hardcoded_token_limits_in_judge_source():
    src = (Path(__file__).resolve().parents[1] / "judge.py").read_text()
    assert "6000" not in src
    assert "8000" not in src
    assert "judge_max_tokens" in src
