"""Minimal, robust client for the OpenRouter chat completions API."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

import requests

import config
from spend import SpendLedger


class OpenRouterError(RuntimeError):
    pass


class _Retryable(Exception):
    """Internal: a failure worth another attempt."""


@dataclass
class ChatResult:
    text: str
    raw: dict
    model: str
    cost_usd: float | None = None
    usage: dict | None = None
    attempts: int = 1
    dropped_params: list[str] = field(default_factory=list)


def _excerpt(text: str, n: int = 300) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= n else text[:n] + "..."


def _usage_cost(data: dict | None) -> tuple[dict | None, float | None]:
    if not isinstance(data, dict) or not isinstance(data.get("usage"), dict):
        return None, None
    usage = data["usage"]
    cost = usage.get("cost")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        cost = None
    return usage, cost


class OpenRouterClient:
    """Thin wrapper over /chat/completions with retries, spend cap and route-aware params.

    Retries only transient failures (HTTP 429, HTTP >= 500, connection errors,
    connect timeouts, and a 200 body whose error code is 429 or >= 500). A read
    timeout is never retried: the provider may already have billed a long
    reasoning reply.
    """

    def __init__(
        self,
        api_key: str | None = None,
        timeout: int = 300,
        ledger: SpendLedger | None = None,
        session=None,
        route_lookup: bool = True,
    ):
        self.api_key = api_key or os.getenv(config.OPENROUTER_API_KEY_ENV)
        if not self.api_key:
            raise OpenRouterError(
                f"Missing API key. Set {config.OPENROUTER_API_KEY_ENV} "
                "(environment or .env)."
            )
        self.timeout = timeout
        self.ledger = ledger
        self.session = session if session is not None else requests.Session()
        self.route_lookup = route_lookup
        # model -> list of per-endpoint supported_parameters sets, or None if
        # the lookup failed (it is attempted at most once per model).
        self._routes: dict[str, list[frozenset] | None] = {}
        self._sleep = time.sleep

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": config.OPENROUTER_REFERER,
            "X-Title": config.OPENROUTER_TITLE,
        }

    # --- Route-aware parameters ----------------------------------------------
    def _endpoint_params(self, model: str) -> list[frozenset] | None:
        """Per-endpoint supported_parameters for a model (free lookup, cached)."""
        if model in self._routes:
            return self._routes[model]
        routes: list[frozenset] | None = None
        try:
            resp = self.session.get(
                f"{config.OPENROUTER_BASE_URL}/models/{model}/endpoints",
                headers=self._headers(),
                timeout=min(self.timeout, 30),
            )
            if resp.status_code != 200:
                raise ValueError(f"HTTP {resp.status_code}")
            endpoints = resp.json()["data"]["endpoints"]
            routes = [frozenset(ep["supported_parameters"]) for ep in endpoints]
            if not routes:
                raise ValueError("no endpoints listed")
        except Exception as e:  # noqa: BLE001 - any failure falls back to the raw payload
            print(f"[CC_WARN] {model}: endpoint lookup failed ({_excerpt(str(e), 120)}); "
                  "sending parameters unchanged")
            routes = None
        self._routes[model] = routes
        return routes

    @staticmethod
    def _adapt_payload(payload: dict, routes: list[frozenset]) -> list[str]:
        """Fit payload to what the model's endpoints accept. Returns dropped params."""
        dropped: list[str] = []
        require = False
        candidates = list(routes)

        # Token limit: endpoints of one model may disagree on the field name.
        if "max_tokens" in payload:
            with_mt = [r for r in routes if "max_tokens" in r]
            with_mct = [r for r in routes if "max_completion_tokens" in r]
            if with_mt:
                if len(with_mt) < len(routes):
                    require = True
                    candidates = with_mt
            elif with_mct:
                payload["max_completion_tokens"] = payload.pop("max_tokens")
                dropped.append("max_tokens")
                if len(with_mct) < len(routes):
                    require = True
                    candidates = with_mct

        # Optional sampling/format params: drop when no reachable endpoint takes them.
        for name in ("temperature", "response_format"):
            if name not in payload:
                continue
            supporting = [r for r in candidates if name in r]
            if not supporting:
                payload.pop(name)
                dropped.append(name)
            elif require:
                candidates = supporting

        if require:
            payload["provider"] = {"require_parameters": True}
        return dropped

    # --- Chat ----------------------------------------------------------------
    def chat(
        self,
        model: str,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int | None = 4096,
        response_format: dict | None = None,
        max_retries: int = 3,
        role: str = "",
    ) -> ChatResult:
        url = f"{config.OPENROUTER_BASE_URL}/chat/completions"
        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "usage": {"include": True},
        }
        # Omit max_tokens entirely when None so the model is left uncapped.
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format is not None:
            payload["response_format"] = response_format

        dropped: list[str] = []
        if self.route_lookup:
            routes = self._endpoint_params(model)
            if routes is not None:
                dropped = self._adapt_payload(payload, routes)
        body = json.dumps(payload)

        last_err: Exception | None = None
        for attempt in range(max_retries):
            if attempt:
                self._sleep(min(2 ** attempt, 20))
            if self.ledger is not None:
                self.ledger.check(model)  # BudgetExceeded propagates, never retried
            try:
                return self._attempt(url, body, model, role, attempt + 1, dropped)
            except _Retryable as e:
                last_err = e
        raise OpenRouterError(f"{model}: failed after {max_retries} attempts: {last_err}")

    def _record(self, model: str, role: str, data: dict | None, billed: bool) -> tuple:
        usage, cost = _usage_cost(data)
        if self.ledger is not None and (usage is not None or billed):
            self.ledger.record(model, role, cost, usage)
        return usage, cost

    def _attempt(
        self, url: str, body: str, model: str, role: str, attempts: int, dropped: list[str]
    ) -> ChatResult:
        try:
            resp = self.session.post(
                url, headers=self._headers(), data=body, timeout=self.timeout
            )
        except requests.exceptions.ReadTimeout as e:
            # The provider may have billed the reply; count it with unknown cost.
            self._record(model, role, None, billed=True)
            raise OpenRouterError(
                f"{model}: read timeout after {self.timeout}s; not retried because "
                f"the provider may already have billed the reply ({e})"
            ) from e
        except (requests.ConnectionError, requests.exceptions.ConnectTimeout) as e:
            raise _Retryable(f"connection error: {e}") from e

        status = resp.status_code
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            data = None

        if os.getenv("CC_DEBUG") == "1":
            print(f"\n[CC_DEBUG] {model} -> HTTP {status}")
            print(json.dumps(data, indent=2)[:4000] if data else _excerpt(resp.text, 4000))

        usage, cost = self._record(model, role, data, billed=(status == 200))

        if status == 429 or status >= 500:
            raise _Retryable(f"HTTP {status}: {_excerpt(resp.text)}")
        if status >= 400:
            raise OpenRouterError(f"{model}: HTTP {status}: {_excerpt(resp.text)}")
        if data is None:
            raise OpenRouterError(
                f"{model}: HTTP {status} with a non-JSON body: {_excerpt(resp.text)}"
            )
        # OpenRouter can return HTTP 200 with an error object in the body.
        err = data.get("error")
        if isinstance(err, dict):
            code = err.get("code")
            if isinstance(code, int) and (code == 429 or code >= 500):
                raise _Retryable(f"API error {code}: {_excerpt(json.dumps(err))}")
            raise OpenRouterError(f"{model}: API error: {_excerpt(json.dumps(err))}")

        try:
            choice = data["choices"][0]
            msg = choice["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise OpenRouterError(
                f"{model}: unexpected response shape: {_excerpt(json.dumps(data))}"
            ) from e
        text = msg.get("content") or ""
        # Reasoning models (e.g. GLM) sometimes leave `content` empty and put the
        # output in a reasoning field; fall back to it so the JSON can be recovered.
        if not text.strip():
            text = msg.get("reasoning") or msg.get("reasoning_content") or ""
        if not text.strip():
            fr = choice.get("finish_reason")
            print(f"[CC_WARN] {model}: empty content "
                  f"(HTTP {status}, finish_reason={fr}, "
                  f"message keys={list(msg.keys())}, usage={data.get('usage')})")
        return ChatResult(
            text=text, raw=data, model=model, cost_usd=cost, usage=usage,
            attempts=attempts, dropped_params=list(dropped),
        )
