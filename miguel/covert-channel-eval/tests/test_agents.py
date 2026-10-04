import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from agents import Collaborator  # noqa: E402


class FakeClient:
    """Records every chat() call and returns queued reply texts in order."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, model, messages, temperature=0.7, max_tokens=None,
             response_format=None, max_retries=3, role=""):
        self.calls.append({
            "model": model, "messages": messages, "temperature": temperature,
            "max_tokens": max_tokens, "role": role,
        })
        return SimpleNamespace(text=self.replies.pop(0))

    def user_content(self, i=-1):
        return self.calls[i]["messages"][1]["content"]


def reply(public="ordinary work", status="none", scheme="", notes="", tool=None):
    d = {"thinking": "notes", "public_message": public,
         "channel_status": status, "tool_call": tool}
    if scheme:
        d["scheme"] = scheme
    if notes:
        d["notes"] = notes
    return json.dumps(d)


def cfg(condition="covert"):
    c = config.ExperimentConfig()
    c.condition = condition
    return c


def test_private_scheme_reaches_only_owner():
    scheme_text = "CODEBOOK-array-length-maps-to-digit"
    client_a = FakeClient([reply(scheme=scheme_text), reply()])
    a = Collaborator(client_a, cfg(), "Agent A")

    a.respond([], [])  # turn 1 declares the scheme
    history = [{"role_name": "Agent A", "public_message": "ordinary work"}]
    a.respond(history, [])  # turn 2

    # The owner sees its own scheme carried into the next turn's prompt.
    assert scheme_text in client_a.user_content(-1)

    # The partner, a separate instance, never receives it (only public messages).
    client_b = FakeClient([reply()])
    b = Collaborator(client_b, cfg(), "Agent B")
    b.respond(history, [])
    assert scheme_text not in client_b.user_content(-1)


def test_inbox_item_persists_to_next_turn():
    token_line = "get_collaboration_token -> your token is 'ABCDE12345'"
    client = FakeClient([reply(), reply()])
    a = Collaborator(client, cfg(), "Agent A")

    a.respond([], [token_line])  # injected once, this turn
    history = [{"role_name": "Agent A", "public_message": "ordinary work"}]
    a.respond(history, [])  # inbox empty now

    assert "ABCDE12345" in client.user_content(-1)


def test_plaintext_condition_loads_different_prompt():
    covert = Collaborator(FakeClient([]), cfg("covert"), "Agent A")
    plain = Collaborator(FakeClient([]), cfg("plaintext"), "Agent A")

    assert covert.system != plain.system
    assert "subtle" in covert.system
    assert "subtle" not in plain.system


def test_respond_passes_max_tokens_and_role():
    c = cfg()
    client = FakeClient([reply()])
    a = Collaborator(client, c, "Agent A")
    a.respond([], [])

    call = client.calls[-1]
    assert call["max_tokens"] == c.collaborator_max_tokens
    assert call["max_tokens"] is not None
    assert call["role"] == "Agent A"
    assert call["temperature"] == c.collaborator_temperature
