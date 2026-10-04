"""Hermetic tests for the T02e fake-price breaker and estimator."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from threading import Barrier

import pytest

from simmons import records, spend
from simmons.config import Config, ModelSpecConfig
from simmons.records import read_jsonl, request_sha256
from simmons.types import Inputs, ModelInfo, ModelSpec, OptionSet, RoleResponse, Usage


BODY = {"messages": [{"role": "user", "content": "ordinary request"}], "tools": []}


def _spec(
    *,
    price_in: float = 0,
    price_out: float = 120,
    max_tokens: int = 100,
    **params,
) -> ModelSpec:
    return ModelSpec(
        provider="fake",
        model="covert",
        temperature=None,
        max_tokens=max_tokens,
        params={"price_in": price_in, "price_out": price_out, **params},
    )


def _response(cost: float | None, generation_id: str | None = "gen-test") -> RoleResponse:
    return RoleResponse(
        tool_input=None,
        text=None,
        thinking=None,
        reasoning_details=None,
        parse_error=None,
        usage=Usage(input_tokens=0, output_tokens=0),
        cost_usd=cost,
        generation_id=generation_id,
        served_by=None,
        model_reported="fake:covert",
        finish_reason="tool_calls",
        tool_choice_mode="forced",
        latency_ms=0,
        raw={},
    )


def _priced_config(*, debrief: str = "blind", cap: float = 0.05) -> Config:
    return Config(
        name="spend-test",
        sender_debrief=debrief,
        max_cost_usd=cap,
        models={
            "default": ModelSpecConfig(
                model="fake:covert",
                max_tokens=100,
                params={"price_in": 1_000_000, "price_out": 1_000_000},
            )
        },
    )


def _covert_plan():
    return [{"index": 0, "kind": "covert", "options": ["Canberra", "Sydney"], "protected": None, "target": 1}]


def test_reservation_uses_utf8_body_bytes_and_reasoning_budget(tmp_path):
    body = {"messages": [{"role": "user", "content": "café"}], "tools": []}
    spec = _spec(
        price_in=800,
        price_out=1200,
        max_tokens=7,
        reasoning={"max_tokens": 10},
    )
    expected_input_bytes = len(json.dumps(body, ensure_ascii=True).encode("utf-8"))
    expected = (expected_input_bytes * 800 + (7 + 10) * 1200) / 1_000_000

    reservation = spend.Breaker(0.1, tmp_path / "spend.jsonl").reserve("a1", body, spec)

    assert reservation.usd == pytest.approx(expected)
    row = read_jsonl(tmp_path / "spend.jsonl")[0]
    assert row["request_sha256"] == request_sha256(body)
    assert row["event"] == "reserve"


def test_real_price_source_covers_fallback_routes_and_respects_hard_route_filters():
    class _Catalog:
        def lookup(self, slug: str):
            assert slug == "vendor/model"
            return ModelInfo(
                slug=slug,
                context_length=16_000,
                supports_tools=True,
                endpoint_prices=(("cheap", 10, 20), ("dear", 100, 200)),
            )

    catalog = _Catalog()
    preferred = ModelSpec(
        "openrouter", "vendor/model", None, 100, {"provider": {"order": ["cheap"]}}
    )
    pinned = ModelSpec(
        "openrouter",
        "vendor/model",
        None,
        100,
        {"provider": {"order": ["cheap"], "allow_fallbacks": False}},
    )
    only = ModelSpec(
        "openrouter", "vendor/model", None, 100, {"provider": {"only": ["cheap"]}}
    )

    assert spend._prices_for(preferred, catalog) == (Decimal(100), Decimal(200))
    assert spend._prices_for(pinned, catalog) == (Decimal(10), Decimal(20))
    assert spend._prices_for(only, catalog) == (Decimal(10), Decimal(20))


def test_reservation_journal_is_fsynced_before_reserve_returns(tmp_path, monkeypatch):
    fsynced: list[int] = []
    real_fsync = records.os.fsync

    def track_fsync(fd: int) -> None:
        fsynced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(records.os, "fsync", track_fsync)
    path = tmp_path / "spend.jsonl"
    reservation = spend.Breaker(0.05, path).reserve("a1", BODY, _spec())

    assert fsynced
    assert reservation.attempt_id == "a1"
    assert read_jsonl(path)[0]["event"] == "reserve"


def test_eight_threads_never_reserve_past_cap_and_stop_below_five_cents(tmp_path):
    breaker = spend.Breaker(0.05, tmp_path / "spend.jsonl")
    barrier = Barrier(8)

    def reserve(index: int) -> float | None:
        barrier.wait(timeout=5)
        try:
            return breaker.reserve(f"a{index}", BODY, _spec()).usd
        except spend.BreakerTripped:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(8)))

    successful = [value for value in results if value is not None]
    assert len(successful) == 4
    assert sum(successful) == pytest.approx(0.048)
    assert breaker.spent_usd() == pytest.approx(sum(successful))
    assert breaker.spent_usd() < 0.05
    assert len(read_jsonl(tmp_path / "spend.jsonl")) == 4
    assert breaker.tripped is not None


def test_settled_unresolved_and_open_spend_replay_to_the_live_total(tmp_path):
    path = tmp_path / "spend.jsonl"
    breaker = spend.Breaker(0.03, path)
    unresolved = breaker.reserve("unresolved", BODY, _spec())
    breaker.unresolved(unresolved, "timeout after submission")
    settled = breaker.reserve("settled", BODY, _spec())
    breaker.settle(settled, _response(0.004))
    open_reservation = breaker.reserve("open", BODY, _spec())
    live_total = breaker.spent_usd()

    restored = spend.Breaker(0.03, path)

    assert live_total == pytest.approx(unresolved.usd + 0.004 + open_reservation.usd)
    assert restored.spent_usd() == pytest.approx(live_total)
    with pytest.raises(spend.BreakerTripped):
        restored.reserve("too-much", BODY, _spec())


def test_torn_final_journal_line_is_truncated_before_the_next_append(tmp_path):
    path = tmp_path / "spend.jsonl"
    first = spend.Breaker(0.05, path).reserve("before-crash", BODY, _spec())
    with path.open("ab") as stream:
        stream.write(
            b'{"schema":"simmons.spend/1","attempt_id":"before-crash",'
            b'"event":"sett'
        )

    resumed = spend.Breaker(0.05, path)

    assert resumed.spent_usd() == pytest.approx(first.usd)
    assert path.read_bytes().endswith(b"\n")
    second = resumed.reserve("after-crash", BODY, _spec())
    replayed = spend.Breaker(0.05, path)

    assert resumed.spent_usd() == pytest.approx(first.usd + second.usd)
    assert replayed.spent_usd() == pytest.approx(resumed.spent_usd())
    assert [row["event"] for row in read_jsonl(path)] == ["reserve", "reserve"]


def test_settlement_uses_exact_cost_and_trips_when_it_exceeds_reservation(tmp_path):
    breaker = spend.Breaker(0.05, tmp_path / "spend.jsonl")
    under = breaker.reserve("under", BODY, _spec())
    breaker.settle(under, _response(0.004))
    assert breaker.spent_usd() == pytest.approx(0.004)

    over = breaker.reserve("over", BODY, _spec())
    breaker.settle(over, _response(over.usd + 0.001))

    assert breaker.tripped is not None
    assert "exceeded its reservation" in breaker.tripped
    assert breaker.spent_usd() == pytest.approx(0.004 + over.usd + 0.001)
    with pytest.raises(spend.BreakerTripped):
        breaker.reserve("after-trip", BODY, _spec())


def test_missing_cost_keeps_the_reservation_as_unresolved(tmp_path):
    path = tmp_path / "spend.jsonl"
    breaker = spend.Breaker(0.05, path)
    reservation = breaker.reserve("a1", BODY, _spec())
    breaker.settle(reservation, _response(None, None))

    assert breaker.spent_usd() == pytest.approx(reservation.usd)
    rows = read_jsonl(path)
    assert [row["event"] for row in rows] == ["reserve", "unresolved"]
    assert rows[-1]["reason"] == "missing usage.cost"
    assert spend.Breaker(0.05, path).spent_usd() == pytest.approx(reservation.usd)


def test_estimate_counts_sender_exit_interview_only_when_enabled():
    plan = _covert_plan()
    blind_config = _priced_config(debrief="blind")
    with_debrief = spend.estimate(blind_config, plan)
    informed = spend.estimate(_priced_config(debrief="informed"), plan)
    off = spend.estimate(_priced_config(debrief="off"), plan)
    sender = spend._role_models(blind_config)["sender"]
    assert sender is not None
    debrief_body = spend._estimate_body("sender_debrief", sender, blind_config, plan[0], None, 0)
    debrief_input_tokens = spend._estimate_input_tokens(debrief_body)
    input_price, output_price = spend._prices_for(sender)
    expected_debrief_cost = float(
        (
            Decimal(debrief_input_tokens) * input_price
            + Decimal(spend._ROLE_OUTPUT_TOKENS["sender_debrief"]) * output_price
        )
        / spend._MILLION
    )

    assert with_debrief - off == pytest.approx(expected_debrief_cost)
    assert informed > off


def test_plain_episode_estimates_include_receiver_calls():
    zero_receiver = Config(
        name="plain-receiver-cost",
        sender_debrief="off",
        models={
            "default": "fake:covert",
            "receiver": ModelSpecConfig(model="fake:covert", params={"price_out": 0}),
        },
    )
    priced_receiver = zero_receiver.model_copy(
        update={
            "models": {
                "default": "fake:covert",
                "receiver": ModelSpecConfig(model="fake:covert", params={"price_out": 100}),
            }
        }
    )

    for kind in ("plain_allowed", "plain_protected"):
        plan = [{"index": 0, "kind": kind, "options": ["Canberra", "Sydney"], "protected": [0], "target": 1}]
        cost_with_receiver = spend.estimate(priced_receiver, plan)
        cost_without_receiver = spend.estimate(zero_receiver, plan)
        assert cost_with_receiver - cost_without_receiver == pytest.approx(0.01)


def test_receiver_estimate_includes_shared_sender_prompt():
    config = Config(
        name="receiver-prompt-cost",
        sender_debrief="off",
        models={
            "default": "fake:covert",
            "receiver": ModelSpecConfig(model="fake:covert", params={"price_in": 1_000}),
        },
    )
    inputs = Inputs(
        option_set=OptionSet(("Canberra", "Sydney")),
        sender_prompt="",
        guard_prompt="",
        plain_sender_prompt="",
        cover_task="news",
        templates={"receiver.md": "$options $message"},
    )
    empty_prompt = spend.estimate(config, _covert_plan(), inputs=inputs)
    long_prompt = spend.estimate(
        config,
        _covert_plan(),
        inputs=replace(inputs, sender_prompt="x" * 4_000),
    )

    assert long_prompt - empty_prompt >= 1.0


def test_preflight_checks_run_ceiling_estimate_and_remaining_credit():
    config = _priced_config(cap=0.05)
    spend.check_before_run(config, 0.05, remaining_usd=0.05)

    with pytest.raises(spend.BreakerTripped, match="exceeds run cap"):
        spend.check_before_run(config, 0.051)
    with pytest.raises(spend.BreakerTripped, match="remaining credit"):
        spend.check_before_run(config, 0.02, remaining_usd=0.01)
    with pytest.raises(spend.BreakerTripped, match="no more than 20.00"):
        spend.check_before_run(_priced_config(cap=25), 0.01)


def test_run_cap_refuses_raised_override_and_returns_lowered_cap():
    config = _priced_config(cap=0.05)

    assert spend.run_cap(config) == pytest.approx(0.05)
    assert spend.run_cap(config, max_cost=0.02) == pytest.approx(0.02)
    with pytest.raises(spend.BreakerTripped, match="exceeds configured cap"):
        spend.run_cap(config, max_cost=0.06)
    with pytest.raises(spend.BreakerTripped, match="greater than 0"):
        spend.run_cap(config, max_cost=0)


def test_between_waves_zero_credit_trips_and_positive_credit_limits_future_reserves():
    breaker = spend.Breaker(0.05)
    spend.between_waves(breaker, remaining_usd=0.02)
    breaker.reserve("a1", BODY, _spec())
    with pytest.raises(spend.BreakerTripped, match="no provider credit remains"):
        spend.between_waves(breaker, remaining_usd=0)

    assert breaker.tripped is not None
    with pytest.raises(spend.BreakerTripped):
        breaker.reserve("a2", BODY, _spec())
