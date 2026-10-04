"""Cliente mínimo y robusto para la API de chat de OpenRouter."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

import requests

import config


class OpenRouterError(RuntimeError):
    pass


@dataclass
class ChatResult:
    text: str
    raw: dict
    model: str


class OpenRouterClient:
    """Wrapper fino sobre /chat/completions con reintentos y backoff."""

    def __init__(self, api_key: str | None = None, timeout: int = 120):
        self.api_key = api_key or os.getenv(config.OPENROUTER_API_KEY_ENV)
        if not self.api_key:
            raise OpenRouterError(
                f"Falta la clave de API. Exporta {config.OPENROUTER_API_KEY_ENV}."
            )
        self.timeout = timeout
        self.session = requests.Session()

    def chat(
        self,
        model: str,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int | None = 4096,
        response_format: dict | None = None,
        max_retries: int = 4,
    ) -> ChatResult:
        url = f"{config.OPENROUTER_BASE_URL}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": config.OPENROUTER_REFERER,
            "X-Title": config.OPENROUTER_TITLE,
        }
        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        # Omit max_tokens entirely when None so the model is left uncapped.
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format is not None:
            payload["response_format"] = response_format

        last_err: Exception | None = None
        for attempt in range(max_retries):
            try:
                resp = self.session.post(
                    url, headers=headers, data=json.dumps(payload),
                    timeout=self.timeout,
                )
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise OpenRouterError(
                        f"HTTP {resp.status_code}: {resp.text[:300]}"
                    )
                resp.raise_for_status()
                data = resp.json()
                if os.getenv("CC_DEBUG") == "1":
                    print(f"\n[CC_DEBUG] {model} -> HTTP {resp.status_code}")
                    print(json.dumps(data, indent=2)[:4000])
                # OpenRouter can return HTTP 200 with an error object in the body.
                if isinstance(data.get("error"), dict):
                    raise OpenRouterError(
                        f"API error for {model}: {data['error']}"
                    )
                msg = data["choices"][0]["message"]
                text = msg.get("content") or ""
                # Reasoning models (e.g. GLM) sometimes leave `content` empty and
                # put the output in a reasoning field — fall back to it so the JSON
                # can still be recovered.
                if not text.strip():
                    text = msg.get("reasoning") or msg.get("reasoning_content") or ""
                if not text.strip():
                    fr = data["choices"][0].get("finish_reason")
                    print(f"[CC_WARN] {model}: empty content "
                          f"(HTTP {resp.status_code}, finish_reason={fr}, "
                          f"message keys={list(msg.keys())}, usage={data.get('usage')})")
                return ChatResult(text=text, raw=data, model=model)
            except Exception as e:  # noqa: BLE001 - reintento genérico controlado
                last_err = e
                sleep = min(2 ** attempt, 20)
                time.sleep(sleep)
        raise OpenRouterError(f"Fallo tras {max_retries} intentos: {last_err}")
