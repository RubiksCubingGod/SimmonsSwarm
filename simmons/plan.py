"""Build deterministic M1 episode plans."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from random import Random

from .config import Config
from .records import PlanRecord
from .types import OptionSet

_PLAN_SCHEMA = "simmons.plan/1"


def _round_half_up_product(count: int, rate: float) -> int:
    """Round a non-negative count-times-rate value using decimal half-up."""
    product = Decimal(count) * Decimal(str(rate))
    return int(product.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _require_m1_modes(config: Config) -> None:
    supported = (
        ("option_draw", "fixed"),
        ("option_order", "fixed"),
        ("protected_selection", "random"),
        ("protected_draw", "sample"),
    )
    for field, expected in supported:
        actual = getattr(config, field)
        if actual != expected:
            raise ValueError(
                f"{field}={actual!r} lands in v1.1; M1 supports {expected!r}"
            )


def _episode_kinds(config: Config, start: int) -> list[str]:
    n_plain = _round_half_up_product(config.episodes, config.plain_sender_rate)
    n_plain_protected = (
        0
        if config.protected_count is None
        else _round_half_up_product(n_plain, config.plain_protected_share)
    )
    n_plain_allowed = n_plain - n_plain_protected
    n_control = min(
        _round_half_up_product(config.episodes, config.control_rate),
        config.episodes - n_plain,
    )
    n_covert = config.episodes - n_plain - n_control

    kinds = (
        ["covert"] * n_covert
        + ["control"] * n_control
        + ["plain_allowed"] * n_plain_allowed
        + ["plain_protected"] * n_plain_protected
    )
    Random(f"{config.seed}/kinds/{start}").shuffle(kinds)
    return kinds


def _fixed_option_pool(config: Config, options: OptionSet) -> list[str]:
    pool = list(options.options)
    option_count = (
        len(pool)
        if config.options_per_episode is None
        else config.options_per_episode
    )
    if option_count == len(pool):
        return pool

    chosen = set(Random(f"{config.seed}/options").sample(pool, option_count))
    return [option for option in pool if option in chosen]


def build_plan(config: Config, options: OptionSet, start: int = 0) -> list[PlanRecord]:
    """Build M1 plan records with deterministic draws and global episode indices."""
    _require_m1_modes(config)
    option_pool = _fixed_option_pool(config, options)
    kinds = _episode_kinds(config, start)
    plan: list[PlanRecord] = []

    for offset, kind in enumerate(kinds):
        index = start + offset
        if config.protected_count is None:
            protected_values: set[str] = set()
            protected: list[int] | None = None
        else:
            protected_values = set(
                Random(f"{config.seed}/protected/{index}").sample(
                    option_pool, config.protected_count
                )
            )
            protected = [
                position
                for position, option in enumerate(option_pool)
                if option in protected_values
            ]

        if kind in {"covert", "plain_protected"} and protected_values:
            target_pool = [
                option for option in option_pool if option in protected_values
            ]
        elif kind in {"control", "plain_allowed"} and protected_values:
            target_pool = [
                option for option in option_pool if option not in protected_values
            ]
        else:
            target_pool = option_pool

        target = Random(f"{config.seed}/target/{index}").choice(target_pool)
        target_index = option_pool.index(target)
        plan.append(
            {
                "schema": _PLAN_SCHEMA,
                "index": index,
                "kind": kind,
                "options": list(option_pool),
                "protected": protected,
                "target": target_index,
            }
        )

    return plan
