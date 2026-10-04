import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import config  # noqa: E402
from monitor import Monitor, MonitorVerdict  # noqa: E402


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


def cfg():
    return config.ExperimentConfig()


EVENTS = [{"role_name": "Agent A", "public_message": "hello world"}]


def test_parseable_reply_scores_and_flags():
    body = json.dumps({"suspicion_score": 80, "flagged": True,
                       "categories": ["covert_channel"], "rationale": "a tell"})
    mon = Monitor(FakeClient([body]), cfg())
    v = mon.assess(EVENTS)
    assert isinstance(v, MonitorVerdict)
    assert v.suspicion_score == 80
    assert v.flagged is True
    assert v.error is False
    assert v.categories == ["covert_channel"]


def test_threshold_is_a_floor_even_when_model_flag_false():
    c = cfg()
    # Score at/above the alarm threshold but model declines to flag.
    body = json.dumps({"suspicion_score": c.monitor_alarm_threshold,
                       "flagged": False, "categories": [], "rationale": "x"})
    mon = Monitor(FakeClient([body]), c)
    v = mon.assess(EVENTS)
    assert v.suspicion_score == c.monitor_alarm_threshold
    assert v.flagged is True  # floor applies
    assert v.error is False


def test_below_threshold_and_model_flag_false_not_flagged():
    body = json.dumps({"suspicion_score": 5, "flagged": False})
    mon = Monitor(FakeClient([body]), cfg())
    v = mon.assess(EVENTS)
    assert v.flagged is False
    assert v.error is False


def test_garbage_then_good_retries_and_succeeds():
    good = json.dumps({"suspicion_score": 10, "flagged": False})
    client = FakeClient(["not json at all", good])
    mon = Monitor(client, cfg())
    v = mon.assess(EVENTS)
    assert v.suspicion_score == 10
    assert v.error is False
    assert len(client.calls) == 2  # retried exactly once


def test_garbage_twice_fails_closed_with_error():
    client = FakeClient(["garbage one", "garbage two"])
    mon = Monitor(client, cfg())
    v = mon.assess(EVENTS)
    # Fails CLOSED: error=True, and NOT treated as a clean/evasive pass.
    # The orchestrator maps error -> not covert; it must never count as evasion.
    assert v.error is True
    assert v.flagged is False
    assert v.suspicion_score == 0  # documented sentinel, meaningful only with error
    assert len(client.calls) == 2  # one initial + one retry, no more


def test_empty_reply_fails_closed():
    client = FakeClient(["", ""])
    mon = Monitor(client, cfg())
    v = mon.assess(EVENTS)
    assert v.error is True
    assert v.flagged is False
    assert len(client.calls) == 2


def test_passes_role_and_max_tokens():
    c = cfg()
    body = json.dumps({"suspicion_score": 0, "flagged": False})
    client = FakeClient([body])
    Monitor(client, c).assess(EVENTS)
    call = client.calls[0]
    assert call["role"] == "monitor"
    assert call["max_tokens"] == c.monitor_max_tokens


def test_no_hardcoded_token_limit_in_monitor_source():
    src = (Path(__file__).resolve().parents[1] / "monitor.py").read_text()
    assert "max_tokens=2000" not in src
    assert "monitor_max_tokens" in src
