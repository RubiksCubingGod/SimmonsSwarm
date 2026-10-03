"""Frozen breaker surface; T02e fills fake-price behavior and T08 real prices."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .config import Config
from .records import PlanRecord
from .types import Breaker as BreakerProtocol
from .types import BreakerTripped, Catalog, ModelSpec, Reservation, RoleResponse

RUN_CEILING_USD = 20.0


class Breaker(BreakerProtocol):
    def __init__(self, cap_usd: float, journal_path: str | Path | None = None) -> None:
        raise NotImplementedError("T02e")

    def reserve(self, attempt_id: str, body: Mapping[str, Any], spec: ModelSpec) -> Reservation:
        raise NotImplementedError("T02e")

    def settle(self, reservation: Reservation, response: RoleResponse) -> None:
        raise NotImplementedError("T02e")

    def unresolved(self, reservation: Reservation, reason: str) -> None:
        raise NotImplementedError("T02e")

    @property
    def tripped(self) -> str | None:
        raise NotImplementedError("T02e")

    def spent_usd(self) -> float:
        raise NotImplementedError("T02e")


def estimate(
    config: Config,
    plan: Sequence[PlanRecord],
    *,
    inputs: Any = None,
    catalog: Catalog | None = None,
) -> float:
    """Estimate the run's provider cost before starting it."""
    raise NotImplementedError("T02e")


def check_before_run(
    config: Config,
    estimate_usd: float,
    *,
    remaining_usd: float | None = None,
) -> None:
    """Refuse a run whose estimate exceeds a configured or provider cap."""
    raise NotImplementedError("T02e")


def between_waves(
    breaker: Breaker,
    *,
    remaining_usd: float | None = None,
) -> None:
    """Refresh external budget state between runner waves."""
    raise NotImplementedError("T02e")


__all__ = ["Breaker", "BreakerTripped", "RUN_CEILING_USD", "between_waves", "check_before_run", "estimate"]
