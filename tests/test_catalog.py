"""Hermetic tests for the recorded OpenRouter catalog and cache."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from simmons.catalog import (
    API_BASE_URL,
    CACHE_TTL_SECONDS,
    check_models,
    load_catalog,
    refresh_catalog,
)
from simmons.config import Config, ModelSpecConfig
from simmons.types import ModelSpec

FIXTURES = Path(__file__).parent / "fixtures" / "openrouter"
CATALOG_FIXTURE = FIXTURES / "catalog.json"


def _read_snapshot() -> dict[str, Any]:
    return json.loads(CATALOG_FIXTURE.read_text(encoding="utf-8"))


def _spec(
    slug: str,
    *,
    max_tokens: int = 16000,
    params: dict[str, Any] | None = None,
) -> ModelSpec:
    return ModelSpec("openrouter", slug, None, max_tokens, params or {})


def _recorded_http(snapshot: dict[str, Any], calls: list[tuple[str, dict[str, str]]]):
    entries = snapshot["models"]

    def get(url: str, headers: dict[str, str]) -> dict[str, Any]:
        calls.append((url, dict(headers)))
        if url == f"{API_BASE_URL}/models":
            return {"data": [entry["model"] for entry in entries]}
        for entry in entries:
            details = entry["model"]["links"]["details"]
            expected = f"https://openrouter.ai{details}"
            if url == expected:
                return {
                    "data": {
                        "id": entry["model"]["id"],
                        "endpoints": entry["endpoints"],
                    }
                }
        raise AssertionError(f"unexpected catalog URL: {url}")

    return get


def test_recorded_catalog_parses_all_endpoints_and_intersects_parameters():
    catalog = load_catalog(CATALOG_FIXTURE)
    sonnet = catalog.lookup("anthropic/claude-sonnet-5.5")
    opus = catalog.lookup("anthropic/claude-opus-5.5")

    assert sonnet is not None and opus is not None
    assert sonnet.context_length == 1_000_000
    assert sonnet.supports_tools
    assert sonnet.endpoint_prices == (
        ("google-vertex", 2.0, 10.0),
        ("amazon-bedrock", 2.0, 10.0),
        ("azure", 2.0, 10.0),
        ("claude-platform-aws", 2.0, 10.0),
        ("anthropic", 2.0, 10.0),
    )
    assert sonnet.supported_parameters == frozenset(
        {"tools", "tool_choice", "max_tokens", "temperature", "reasoning", "response_format"}
    )
    for unsupported in ("parallel_tool_calls", "top_p", "seed"):
        assert unsupported not in sonnet.supported_parameters
        assert unsupported not in opus.supported_parameters

    no_tools = catalog.lookup("fixture/no-tools-model")
    unpriced = catalog.lookup("fixture/unpriced-model")
    assert no_tools is not None and not no_tools.supports_tools and no_tools.endpoint_prices
    assert no_tools.supported_parameters == frozenset({"temperature", "max_tokens"})
    assert unpriced is not None and unpriced.supports_tools and not unpriced.endpoint_prices


def test_catalog_absence_is_network_free_and_reported_as_structural_only(tmp_path: Path):
    missing = load_catalog(tmp_path / "missing.json")
    assert missing.age_seconds() is None
    notices = check_models(
        {"sender": _spec("not/in-cache")},
        missing,
    )
    assert len(notices) == 1
    assert "structural only" in notices[0]


@pytest.mark.parametrize(
    ("slug", "message"),
    [
        ("missing/model", "unknown OpenRouter model slug"),
        ("fixture/no-tools-model", "does not support tools"),
        ("fixture/unpriced-model", "has no priced endpoints"),
    ],
)
def test_check_models_rejects_unknown_non_tool_and_unpriced_models(
    slug: str, message: str
):
    catalog = load_catalog(CATALOG_FIXTURE)
    with pytest.raises(ValueError, match=message):
        check_models({"sender": _spec(slug)}, catalog)


def test_context_fit_uses_max_tokens_and_projected_request_size():
    catalog = load_catalog(CATALOG_FIXTURE)
    assert Config(name="context").log_window == 20
    assert ModelSpecConfig(model="anthropic/claude-sonnet-5.5").max_tokens == 16000

    assert check_models(
        {"sender": _spec("anthropic/claude-sonnet-5.5")},
        catalog,
        projected_input_tokens={"sender": 984000},
    ) == ()
    with pytest.raises(ValueError, match="cannot fit model context"):
        check_models(
            {"sender": _spec("anthropic/claude-sonnet-5.5")},
            catalog,
            projected_input_tokens={"sender": 984001},
        )


@pytest.mark.parametrize("parameter", ["top_p", "seed"])
def test_check_models_rejects_unsupported_model_params_but_allows_provider_routing(
    parameter: str,
):
    catalog = load_catalog(CATALOG_FIXTURE)
    with pytest.raises(ValueError, match=rf"models.sender.params.{parameter}"):
        check_models(
            {
                "sender": _spec(
                    "anthropic/claude-sonnet-5.5",
                    params={parameter: 0.9 if parameter == "top_p" else 17},
                )
            },
            catalog,
        )

    assert check_models(
        {
            "sender": _spec(
                "anthropic/claude-sonnet-5.5",
                params={"provider": {"order": ["anthropic"], "allow_fallbacks": False}},
            )
        },
        catalog,
    ) == ()


def test_fresh_cache_is_reused_and_day_old_cache_refreshes_with_injected_clock(
    tmp_path: Path,
):
    snapshot = _read_snapshot()
    calls: list[tuple[str, dict[str, str]]] = []
    http_get = _recorded_http(snapshot, calls)
    cache_path = tmp_path / ".simmons" / "catalog.json"
    clock = [1000.0]

    first = refresh_catalog(
        cache_path,
        now=lambda: clock[0],
        http_get=http_get,
        api_key="fake-api-key",
        management_key="fake-management-key",
    )
    assert first.lookup("anthropic/claude-sonnet-5.5") is not None
    assert len(calls) == 1 + len(snapshot["models"])
    assert calls[0] == (
        f"{API_BASE_URL}/models",
        {"Authorization": "Bearer fake-api-key"},
    )
    assert all(
        headers == {"Authorization": "Bearer fake-management-key"}
        for _, headers in calls[1:]
    )

    clock[0] += CACHE_TTL_SECONDS - 1
    reused = refresh_catalog(
        cache_path,
        now=lambda: clock[0],
        http_get=lambda *_: pytest.fail("fresh catalog unexpectedly made an HTTP request"),
    )
    assert reused.age_seconds() == CACHE_TTL_SECONDS - 1

    clock[0] += 1
    refreshed = refresh_catalog(
        cache_path,
        now=lambda: clock[0],
        http_get=http_get,
        api_key="fake-api-key",
        management_key="fake-management-key",
    )
    assert refreshed.age_seconds() == 0
    assert len(calls) == 2 * (1 + len(snapshot["models"]))
    assert "fake-api-key" not in cache_path.read_text(encoding="utf-8")
    assert "fake-management-key" not in cache_path.read_text(encoding="utf-8")


def test_check_models_checks_tools_on_the_pinned_endpoints():
    catalog = load_catalog(CATALOG_FIXTURE)
    # fixture-host-b lacks tools: unpinned routing can reach it, a pin to host-a cannot.
    with pytest.raises(ValueError, match="does not support tools"):
        check_models({"sender": _spec("fixture/no-tools-model")}, catalog)
    with pytest.raises(ValueError, match="does not support tools"):
        check_models(
            {"sender": _spec("fixture/no-tools-model", params={"provider": {"only": ["fixture-host-b"]}})},
            catalog,
        )
    assert check_models(
        {"sender": _spec("fixture/no-tools-model", params={"provider": {"order": ["fixture-host-a"]}})},
        catalog,
    ) == ()
