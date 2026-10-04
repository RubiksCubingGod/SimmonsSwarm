"""Model-judged scoring for open answers."""

from __future__ import annotations

from ..scorers import register_scorer
from ..types import JudgeCaller, JudgeView, Pick, Score, Target

HIT_THRESHOLD = 0.5


@register_scorer("llm_judge")
class LlmJudgeScorer:
    """Ask the judge model whether a free-text answer names the target."""

    name = "llm_judge"
    payloads = frozenset({"text"})
    needs_model = True

    def score(self, target: Target, pick: Pick, judge: JudgeCaller | None) -> Score:
        if judge is None:
            raise ValueError("llm_judge requires a judge model")
        result = judge(JudgeView(role="judge", episode=-1, target=target, pick=pick))
        hit = result.value >= HIT_THRESHOLD
        return Score(
            value=result.value,
            hit=hit,
            detail={"method": self.name, "threshold": HIT_THRESHOLD, **(result.detail or {})},
        )
