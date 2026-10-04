"""Exact-match scoring for option payloads."""

from __future__ import annotations

from ..scorers import register_scorer
from ..text import normalize
from ..types import JudgeCaller, Pick, Score, Target


@register_scorer("exact_match")
class ExactMatchScorer:
    name = "exact_match"
    payloads = frozenset({"option"})
    needs_model = False

    def score(self, target: Target, pick: Pick, judge: JudgeCaller | None) -> Score:
        if target.payload != pick.payload:
            raise ValueError(
                f"cannot compare target payload {target.payload!r} with pick payload {pick.payload!r}"
            )
        hit = normalize(target.value) == normalize(pick.value)
        return Score(
            value=1.0 if hit else 0.0,
            hit=hit,
            detail={
                "method": self.name,
                "normalized_target": normalize(target.value),
                "normalized_pick": normalize(pick.value),
            },
        )
