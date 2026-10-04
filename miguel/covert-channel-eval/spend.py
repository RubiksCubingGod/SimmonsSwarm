"""Spend accounting and a hard cost cap for OpenRouter calls.

A batch owns one ``SpendLedger`` with the batch cap; each run gets a child
ledger (``SpendLedger(cap, parent=batch_ledger)``). Every recorded call adds to
the child and all its ancestors, so the batch total is always the sum of its
runs, and a cap at any level stops further calls.

Overshoot bound: ``check()`` refuses a call when the money already spent plus
the largest single-call cost seen so far for that model would exceed the cap.
The check and the call are not atomic, so with N workers sharing a ledger the
cap can still be exceeded by roughly one call per concurrent worker (each may
pass the check before any of them records), plus any call whose cost exceeds
the largest one observed so far.
"""

from __future__ import annotations

import threading


class BudgetExceeded(RuntimeError):
    """Raised before a call that a spend cap forbids."""


def _new_bucket() -> dict:
    return {"usd": 0.0, "calls": 0, "unknown_cost_calls": 0}


class SpendLedger:
    """Thread-safe running total of API spend with an optional cap."""

    def __init__(
        self,
        cap_usd: float | None = None,
        parent: "SpendLedger | None" = None,
        label: str = "",
    ):
        self.cap_usd = cap_usd
        self.parent = parent
        self.label = label
        self._lock = threading.Lock()
        self._total = 0.0
        self._calls = 0
        self._unknown = 0
        self._refused = False
        self._max_cost: dict[str, float] = {}
        self._by_role: dict[str, dict] = {}
        self._by_model: dict[str, dict] = {}
        self._tokens = {"prompt": 0, "completion": 0, "reasoning": 0}

    def _chain(self) -> list["SpendLedger"]:
        chain: list[SpendLedger] = []
        node: SpendLedger | None = self
        while node is not None:
            chain.append(node)
            node = node.parent
        return chain

    def check(self, model: str) -> None:
        """Raise BudgetExceeded if this ledger or any ancestor would be exceeded."""
        chain = self._chain()
        projected = 0.0
        for node in chain:
            with node._lock:
                projected = max(projected, node._max_cost.get(model, 0.0))
        for node in chain:
            if node.cap_usd is None:
                continue
            with node._lock:
                spent = node._total
                refuse = spent >= node.cap_usd or spent + projected > node.cap_usd
                if refuse:
                    node._refused = True
            if refuse:
                if node is not self:
                    with self._lock:
                        self._refused = True
                name = f" '{node.label}'" if node.label else ""
                raise BudgetExceeded(
                    f"spend cap{name} reached: spent ${spent:.4f} of "
                    f"${node.cap_usd:.4f}; next {model} call projected at "
                    f"${projected:.4f}"
                )

    def record(
        self,
        model: str,
        role: str,
        cost_usd: float | None,
        usage: dict | None,
    ) -> None:
        """Record one call on this ledger and every ancestor."""
        tokens = _usage_tokens(usage)
        for node in self._chain():
            node._record_one(model, role, cost_usd, tokens)

    def _record_one(
        self, model: str, role: str, cost: float | None, tokens: dict
    ) -> None:
        with self._lock:
            self._calls += 1
            buckets = (
                self._by_role.setdefault(role or "unknown", _new_bucket()),
                self._by_model.setdefault(model, _new_bucket()),
            )
            for b in buckets:
                b["calls"] += 1
            if cost is None:
                self._unknown += 1
                for b in buckets:
                    b["unknown_cost_calls"] += 1
            else:
                cost = float(cost)
                self._total += cost
                for b in buckets:
                    b["usd"] += cost
                if cost > self._max_cost.get(model, 0.0):
                    self._max_cost[model] = cost
            for k, v in tokens.items():
                self._tokens[k] += v

    @property
    def total_usd(self) -> float:
        with self._lock:
            return self._total

    @property
    def exhausted(self) -> bool:
        with self._lock:
            if self._refused:
                return True
            return self.cap_usd is not None and self._total >= self.cap_usd

    def to_dict(self) -> dict:
        exhausted = self.exhausted
        with self._lock:
            def rounded(buckets: dict) -> dict:
                return {
                    k: {**v, "usd": round(v["usd"], 6)} for k, v in buckets.items()
                }

            return {
                "label": self.label,
                "cap_usd": self.cap_usd,
                "total_usd": round(self._total, 6),
                "calls": self._calls,
                "unknown_cost_calls": self._unknown,
                "by_role": rounded(self._by_role),
                "by_model": rounded(self._by_model),
                "tokens": dict(self._tokens),
                "exhausted": exhausted,
            }


def _usage_tokens(usage: dict | None) -> dict:
    """Extract prompt/completion/reasoning token counts from an OpenRouter usage dict."""
    out = {"prompt": 0, "completion": 0, "reasoning": 0}
    if not isinstance(usage, dict):
        return out

    def as_int(v) -> int:
        return v if isinstance(v, int) and not isinstance(v, bool) else 0

    out["prompt"] = as_int(usage.get("prompt_tokens"))
    out["completion"] = as_int(usage.get("completion_tokens"))
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict):
        out["reasoning"] = as_int(details.get("reasoning_tokens"))
    return out
