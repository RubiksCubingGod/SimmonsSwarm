"""Offline stand-in for OpenRouter: no network, no cost.

The fake sender sometimes names its city outright (and is made to rewrite),
the fake receiver records any city it sees and sometimes ends the chat, and the
fake guard's suspicion is random.
"""

from __future__ import annotations

import json
import random
import re
import threading

from orclient import ChatResult

CITY_RE = re.compile(r"\*\*(.+?)\*\*")


class FakeClient:
    def __init__(self, seed: int = 0):
        self._rng = random.Random(seed)
        self._lock = threading.Lock()

    def chat(self, model: str, messages: list[dict], **_: object) -> ChatResult:
        system = messages[0]["content"]
        with self._lock:
            if "compliance monitor" in system:
                content = self._guard(messages[-1]["content"])
            elif "You are an investigator" in system:
                content = json.dumps({"guess": [], "confidence": round(self._rng.random(), 2), "reason": "fake"})
            elif "<record>" in system:
                content = self._receiver(messages)
            else:
                content = self._sender(system, messages[-1]["content"])
        return ChatResult(content=content, message={"role": "assistant", "content": content}, reasoning=None)

    def _sender(self, system: str, last: str) -> str:
        city = CITY_RE.search(system)
        text = "How about Priya on evaluation for week one?"
        if city and "must never appear" not in last and self._rng.random() < 0.3:
            text += f" Also, {city.group(1)}."
        return f"<notes>fake sender notes</notes>\n<message>{text}</message>"

    def _receiver(self, messages: list[dict]) -> str:
        from handshake import CITIES, ITEMS
        last = messages[-1]["content"]
        seen = [c for c in CITIES if c in last]
        items = [i for i in ITEMS if i in last]
        record = (f"\n<record>{'; '.join(items)}</record>" if items else
                  f"\n<record>{seen[0]}</record>" if seen else "")
        seen = seen or items
        end = "\n<end/>" if not seen and self._rng.random() < 0.1 else ""
        return f"<notes>fake receiver notes</notes>\n<message>Sounds good to me.</message>{record}{end}"

    def _guard(self, user: str) -> str:
        answer = {"suspicion": round(self._rng.random() * 0.6, 2), "reason": "fake"}
        if '"guess"' in user:
            answer["guess"] = None
        return json.dumps(answer)
