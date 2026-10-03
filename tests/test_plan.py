"""M1 plan draws are stable, paired, and valid against the frozen record schema."""

from __future__ import annotations

import hashlib
from collections import Counter

import pytest

import simmons.plan as plan_module
from simmons.config import Config, ModelSpecConfig
from simmons.plan import build_plan
from simmons.records import PlanRecord, append_jsonl, canonical_json, validate_record
from simmons.types import OptionSet


def _option_set(size: int = 100) -> OptionSet:
    return OptionSet(options=tuple(f"City {index:03d}" for index in range(size)))


def _plan_bytes(plan: list[PlanRecord]) -> bytes:
    return b"".join(
        (canonical_json(record) + "\n").encode("utf-8") for record in plan
    )


def test_same_seed_has_identical_jsonl_and_hash_and_different_seed_changes_them():
    config = Config(name="plan-determinism")
    options = _option_set()

    first = _plan_bytes(build_plan(config, options))
    repeated = _plan_bytes(build_plan(config, options))
    changed = _plan_bytes(build_plan(config.model_copy(update={"seed": 8}), options))

    assert first == repeated
    assert hashlib.sha256(first).digest() == hashlib.sha256(repeated).digest()
    assert first != changed
    assert hashlib.sha256(first).digest() != hashlib.sha256(changed).digest()


def test_plan_hash_matches_append_jsonl_bytes_and_ignores_non_draw_config(tmp_path):
    assert hasattr(plan_module, "plan_hash"), "missing plan_hash helper"
    config = Config(name="plan-hash", episodes=8)
    options = _option_set()
    lines = build_plan(config, options)
    path = tmp_path / "plan.jsonl"
    for line in lines:
        append_jsonl(path, line)

    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    actual = plan_module.plan_hash(lines)
    changed_prompts_models = config.model_copy(
        update={
            "sender_prompt": "prompts/sender/another.md",
            "guard_prompt": "prompts/guard/another.md",
            "plain_sender_prompt": "prompts/plain/another.md",
            "models": {
                "default": ModelSpecConfig(model="fake:random", max_tokens=512)
            },
            "log_window": 1,
        }
    )
    changed_seed = config.model_copy(update={"seed": config.seed + 1})

    assert actual == expected
    assert plan_module.plan_hash(build_plan(changed_prompts_models, options)) == actual
    assert plan_module.plan_hash(build_plan(changed_seed, options)) != actual


def test_plan_ignores_prompts_models_and_log_window():
    config = Config(name="plan-pairing")
    changed = config.model_copy(
        update={
            "sender_prompt": "prompts/sender/another.md",
            "guard_prompt": "prompts/guard/another.md",
            "plain_sender_prompt": "prompts/plain/another.md",
            "models": {
                "default": ModelSpecConfig(model="fake:random", max_tokens=512)
            },
            "log_window": 1,
        }
    )
    options = _option_set()

    assert _plan_bytes(build_plan(config, options)) == _plan_bytes(
        build_plan(changed, options)
    )


def test_base_story_has_exact_mix_and_null_protected_sets():
    plan = build_plan(Config(name="base"), _option_set())

    assert Counter(record["kind"] for record in plan) == {
        "covert": 140,
        "plain_allowed": 60,
    }
    assert all(record["protected"] is None for record in plan)
    assert all(0 <= record["target"] < len(record["options"]) for record in plan)
    for record in plan:
        validate_record(record)


def test_watchlist_has_exact_mix_and_target_membership():
    config = Config(
        name="watchlist",
        protected_count=10,
        control_rate=0.2,
        plain_sender_rate=0.1,
    )
    plan = build_plan(config, _option_set())

    assert Counter(record["kind"] for record in plan) == {
        "covert": 140,
        "control": 40,
        "plain_allowed": 10,
        "plain_protected": 10,
    }
    assert len({tuple(record["protected"]) for record in plan}) > 1
    for record in plan:
        protected = record["protected"]
        assert protected is not None and len(protected) == 10
        assert protected == sorted(set(protected))
        if record["kind"] in {"covert", "plain_protected"}:
            assert record["target"] in protected
        else:
            assert record["target"] not in protected
        validate_record(record)


def test_fixed_option_draw_keeps_the_pool_order_and_reuses_the_pool():
    options = _option_set(30)
    config = Config(
        name="fixed-subset",
        episodes=12,
        options_per_episode=10,
        plain_sender_rate=0.0,
    )
    plan = build_plan(config, options)
    first_options = plan[0]["options"]
    pool_positions = [options.options.index(option) for option in first_options]

    assert pool_positions == sorted(pool_positions)
    assert all(record["options"] == first_options for record in plan)


def test_kind_rounding_uses_half_up_and_no_watchlist_has_no_protected_plain_kind():
    no_watchlist = Config(
        name="half-up-base",
        episodes=5,
        plain_sender_rate=0.5,
        plain_protected_share=1.0,
    )
    plan = build_plan(no_watchlist, _option_set())
    assert Counter(record["kind"] for record in plan) == {
        "covert": 2,
        "plain_allowed": 3,
    }
    assert all(record["protected"] is None for record in plan)

    watchlist = Config(
        name="half-up-watchlist",
        episodes=5,
        protected_count=1,
        plain_sender_rate=1.0,
        plain_protected_share=0.5,
    )
    plan = build_plan(watchlist, _option_set())
    assert Counter(record["kind"] for record in plan) == {
        "plain_allowed": 2,
        "plain_protected": 3,
    }


def test_start_sets_global_indices():
    config = Config(name="continued", episodes=4)

    assert [record["index"] for record in build_plan(config, _option_set(), start=37)] == [
        37,
        38,
        39,
        40,
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("option_draw", "sample"),
        ("option_order", "shuffled"),
        ("protected_selection", "cluster"),
        ("protected_draw", "fixed"),
    ),
)
def test_non_m1_modes_fail_with_v1_1_guidance(field: str, value: str):
    config = Config(name="deferred").model_copy(update={field: value})

    with pytest.raises(ValueError, match="lands in v1\\.1"):
        build_plan(config, _option_set())
