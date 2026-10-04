"""OpenRouter chat client with a hard spend ceiling, plus an offline fake client.

Every real call goes through `Budget`: before a call it reserves the call's
worst-case cost, and the call is refused if the total already spent (across
every committed run) plus that reservation would pass the limit. After the
call it settles the reservation at the exact cost OpenRouter reports.
"""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODELS_URL = "https://openrouter.ai/api/v1/models"

# USD per token, used only when the live model list cannot be fetched.
FALLBACK_PRICES = {
    "anthropic/claude-sonnet-5.5": (2.0e-6, 10.0e-6),
    "openai/gpt-6.1-sol": (2.0e-6, 10.0e-6),
    "z-ai/glm-5.3": (0.22e-6, 3.39e-6),
}

# Reservations are padded so a price difference between hosts cannot push a
# call past the ceiling.
SAFETY_FACTOR = 1.25
MAX_ATTEMPTS = 4
TIMEOUT_S = 240


class BudgetExceeded(RuntimeError):
    """The next call could take total spend past the limit."""


class ProviderError(RuntimeError):
    """OpenRouter returned an error that retrying will not fix."""


def load_api_key(start: Path) -> str:
    """Read OPENROUTER_API_KEY from the environment or the repo's .env."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    for folder in [start, *start.parents]:
        env = folder / ".env"
        if env.is_file():
            for line in env.read_text(encoding="utf-8").splitlines():
                name, _, value = line.partition("=")
                if name.strip() == "OPENROUTER_API_KEY" and value.strip():
                    return value.strip().strip('"').strip("'")
        if (folder / ".git").exists():
            break
    raise SystemExit("OPENROUTER_API_KEY is not set. Put it in .env at the repo root (it is gitignored).")


def fetch_prices(models: list[str]) -> dict[str, tuple[float, float]]:
    """Per-token (input, output) prices from OpenRouter's public model list."""
    try:
        with urllib.request.urlopen(MODELS_URL, timeout=30) as resp:
            data = json.load(resp)["data"]
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError):
        print("warning: could not fetch OpenRouter prices; using the built-in fallback prices")
        return {m: FALLBACK_PRICES[m] for m in models}
    listed = {m["id"]: m for m in data}
    prices = {}
    for model in models:
        if model not in listed:
            raise SystemExit(f"model {model} is not on OpenRouter's model list")
        p = listed[model]["pricing"]
        prices[model] = (float(p["prompt"]), float(p["completion"]))
    return prices


def spent_so_far(runs_dir: Path) -> float:
    """Total USD recorded in every run's spend log under runs_dir."""
    total = 0.0
    for log in runs_dir.glob("*/spend.jsonl"):
        for line in log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                total += json.loads(line)["cost_usd"]
    return total


class Budget:
    """Thread-safe spend ceiling with reserve-then-settle accounting."""

    def __init__(self, limit_usd: float, already_spent: float, log_path: Path | None):
        self.limit = limit_usd
        self.spent = already_spent
        self.reserved = 0.0
        self.log_path = log_path
        self._lock = threading.Lock()

    def reserve(self, amount: float) -> None:
        with self._lock:
            if self.spent + self.reserved + amount > self.limit:
                raise BudgetExceeded(
                    f"next call could cost up to ${amount:.4f}; spent ${self.spent:.4f} "
                    f"+ in flight ${self.reserved:.4f} would pass the ${self.limit:.2f} limit"
                )
            self.reserved += amount

    def settle(self, reserved: float, cost: float, entry: dict) -> None:
        with self._lock:
            self.reserved -= reserved
            self.spent += cost
            if self.log_path is not None:
                line = {"time": datetime.now(timezone.utc).isoformat(), "cost_usd": cost, **entry}
                with self.log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(line) + "\n")


@dataclass
class ChatResult:
    content: str
    message: dict  # the assistant message, replayable as conversation history
    reasoning: str | None
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0


class OpenRouterClient:
    def __init__(self, api_key: str, prices: dict[str, tuple[float, float]], budget: Budget):
        self.api_key = api_key
        self.prices = prices
        self.budget = budget

    def worst_case(self, model: str, messages: list[dict], max_tokens: int) -> float:
        # A token is never shorter than one UTF-8 byte, so bytes bound input tokens.
        price_in, price_out = self.prices[model]
        input_bytes = len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
        return (input_bytes * price_in + max_tokens * price_out) * SAFETY_FACTOR

    def chat(self, model: str, messages: list[dict], *, max_tokens: int,
             reasoning: dict | None = None, temperature: float | None = None, tag: str = "") -> ChatResult:
        body: dict = {"model": model, "messages": messages, "max_tokens": max_tokens,
                      "usage": {"include": True}}
        if reasoning is not None:
            body["reasoning"] = reasoning
        if temperature is not None:
            body["temperature"] = temperature
        payload = json.dumps(body).encode("utf-8")

        for attempt in range(1, MAX_ATTEMPTS + 1):
            reservation = self.worst_case(model, messages, max_tokens)
            self.budget.reserve(reservation)
            entry = {"model": model, "tag": tag, "attempt": attempt, "reserved_usd": reservation}
            try:
                data = self._post(payload)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:500]
                # An error response is not billed.
                self.budget.settle(reservation, 0.0, {**entry, "status": f"http {e.code}"})
                if e.code in (408, 429) or e.code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                raise ProviderError(f"{model}: HTTP {e.code}: {detail}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                # The request may still have been billed, so count the whole reservation.
                self.budget.settle(reservation, reservation, {**entry, "status": f"network: {e}"})
                time.sleep(2 ** attempt)
                continue

            usage = data.get("usage") or {}
            cost = usage.get("cost")
            if cost is None:
                price_in, price_out = self.prices[model]
                cost = (usage.get("prompt_tokens", 0) * price_in
                        + usage.get("completion_tokens", 0) * price_out)
            self.budget.settle(reservation, float(cost), {**entry, "status": "ok", "usage": usage})

            if "error" in data or not data.get("choices"):
                raise ProviderError(f"{model}: {json.dumps(data.get('error', data))[:500]}")
            message = data["choices"][0]["message"]
            content = message.get("content") or ""
            replay = {"role": "assistant", "content": content}
            if message.get("reasoning_details"):
                replay["reasoning_details"] = message["reasoning_details"]
            return ChatResult(content=content, message=replay, reasoning=message.get("reasoning"),
                              usage=usage, cost_usd=float(cost))
        raise ProviderError(f"{model}: no successful response after {MAX_ATTEMPTS} attempts")

    def _post(self, payload: bytes) -> dict:
        req = urllib.request.Request(API_URL, data=payload, method="POST", headers={
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-Title": "SimmonsSwarm activation transfer",
        })
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return json.load(resp)


class FakeClient:
    """Offline stand-in: no network, no cost. Readers answer at random."""

    ARTICLE = (
        "German Economy Edges Through a Difficult Year\n\n"
        "BERLIN - Germany's economy moved sideways in 2025 as weak industrial orders, high energy "
        "costs and soft export demand weighed on growth. Output rose about 0.3 percent, according "
        "to preliminary figures, while unemployment climbed to 6.4 percent. Economists pointed to "
        "the auto sector and chemical makers as the main drags, and said a looser budget stance "
        "could support demand in 2026."
    )

    def __init__(self, seed: int = 0):
        self._rng = random.Random(seed)
        self._lock = threading.Lock()

    def chat(self, model: str, messages: list[dict], **_: object) -> ChatResult:
        last = messages[-1]["content"]
        with self._lock:
            if '"ranking"' in last:
                animals = [a.strip() for a in re.search(r"<animals>(.*?)</animals>", last, re.S).group(1).split(",")]
                answer = {"suspicion": round(self._rng.random(), 2), "reason": "fake",
                          "ranking": self._rng.sample(animals, 10)}
            elif '"p_one"' in last:
                p = round(self._rng.random(), 2)
                answer = {"suspicion": round(self._rng.random(), 2), "reason": "fake",
                          "answer": int(p >= 0.5), "p_one": p}
            elif "Today's article" in last:
                answer = None
                content = self.ARTICLE
            else:
                answer = None
                content = "Fake interview answer."
        if answer is not None:
            content = json.dumps(answer)
        return ChatResult(content=content, message={"role": "assistant", "content": content}, reasoning=None)
