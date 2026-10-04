"""Small helpers: prompt loading, secret generation, robust JSON parsing."""

from __future__ import annotations

import json
import os
import random
import re
import string

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")


def load_prompt(name: str) -> str:
    with open(os.path.join(PROMPTS_DIR, name), "r", encoding="utf-8") as f:
        return f.read()


def make_rng(seed: int, salt: str) -> random.Random:
    return random.Random(f"{seed}:{salt}")


def random_id(rng: random.Random, length: int) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"  # base32-ish, no ambiguous chars
    return "".join(rng.choice(alphabet) for _ in range(length))


def random_url(rng: random.Random) -> str:
    hosts = ["example.com", "acme-metrics.io", "data-vault.net", "notes.app",
             "ref-archive.org", "cdn-assets.co"]
    slug = "".join(rng.choice(string.ascii_lowercase + string.digits) for _ in range(8))
    return f"https://{rng.choice(hosts)}/r/{slug}"


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def parse_json_lenient(text: str) -> dict:
    """Parse a JSON object from a model response, tolerating code fences / prose."""
    text = text.strip()
    # Strip ```json ... ``` fences if present.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _JSON_BLOCK.search(text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {"_parse_error": True, "_raw": text}
