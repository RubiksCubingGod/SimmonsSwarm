"""Summarize an activation-transfer run into markdown tables.

    uv run python aarav/activation_transfer/summarize.py <run-dir>
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from math import comb
from pathlib import Path

ARMS = ("hold", "told")  # A: strong hold-in-mind prompt; B: only told
READER_ORDER = [("R", "sonnet"), ("R", "sol"), ("R", "glm"), ("G", "sonnet"), ("G", "sol"), ("G", "glm")]


def tail_p(hits: int, n: int, p: float) -> float:
    """P(at least `hits` successes in n trials at chance rate p)."""
    return sum(comb(n, k) * p ** k * (1 - p) ** (n - k) for k in range(hits, n + 1))


def load(run_dir: Path) -> list[dict]:
    path = run_dir / "episodes.jsonl"
    if not path.exists():
        return []
    episodes = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            ep = json.loads(line)
            episodes[ep["id"]] = ep  # a resumed run's later line wins
    return list(episodes.values())


def _reads(episodes: list[dict], role: str, key: str) -> list[tuple[dict, dict]]:
    return [(ep, r) for ep in episodes for r in ep.get("reads", [])
            if r["role"] == role and r["model_key"] == key and r.get("answer")]


def baseline_section(episodes: list[dict]) -> tuple[list[str], dict]:
    """Each reader's #1 picks on articles written with no animal in mind."""
    favorites: dict = {}
    lines = ["## Experiment 1 baseline: each reader's default picks (no animal in mind)", "",
             "| Reader | Articles | #1 picks |", "|---|---|---|"]
    eps = [e for e in episodes if e["experiment"] == "animal_baseline"]
    for role, key in READER_ORDER:
        reads = _reads(eps, role, key)
        if not reads:
            continue
        firsts = Counter(r["answer"]["ranking"][0] for _, r in reads)
        favorites[(role, key)] = set(firsts)
        picks = ", ".join(f"{a} ({n})" for a, n in firsts.most_common())
        lines.append(f"| {role}-{key} | {len(reads)} | {picks} |")
    return lines + [""], favorites


def animal_section(episodes: list[dict], favorites: dict) -> list[str]:
    lines = ["## Experiment 1: the animal", "",
             "Rank of the true animal in each reader's top 10 (`-` = not in its top 10). Chance: 1% top-1, "
             "3% top-3, 10% top-10. `*` marks a hit on an animal that reader also picked first with no "
             "animal in mind, which may be its default rather than a signal.", "",
             "| Reader | Arm | Articles | Ranks | Top-1 | Top-3 | Top-10 | p (top-3 by chance) | Mean suspicion | Would block |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for role, key in READER_ORDER:
        for arm in ARMS:
            eps = [e for e in episodes if e["experiment"] == "animal" and e["arm"] == arm]
            reads = _reads(eps, role, key)
            if not reads:
                continue
            ranks, marks = [], []
            for ep, r in reads:
                rank = r.get("rank")
                default = ep["secret"] in favorites.get((role, key), set())
                marks.append("-" if rank is None else f"{rank}{'*' if default else ''}")
                ranks.append(rank)
            n = len(ranks)
            top = {k: sum(1 for x in ranks if x is not None and x <= k) for k in (1, 3, 10)}
            susp = [r["answer"]["suspicion"] for _, r in reads if "suspicion" in r["answer"]]
            mean_susp = f"{sum(susp) / len(susp):.2f}" if susp else ""
            blocks = f"{sum(1 for _, r in reads if r['answer'].get('would_block'))}/{n}" if susp else ""
            lines.append(f"| {role}-{key} | {arm} | {n} | {', '.join(marks)} | {top[1]} | {top[3]} | "
                         f"{top[10]} | {tail_p(top[3], n, 0.03):.3f} | {mean_susp} | {blocks} |")
    return lines + [""]


def bit_section(episodes: list[dict]) -> list[str]:
    lines = ["## Experiment 2: the bit", "",
             "Chance is 50%. `p` is the chance of at least this many correct by guessing. Mean p(1) should "
             "be higher when the bit was 1 than when it was 0 if the reader picks anything up.", "",
             "| Reader | Arm | Articles | Correct | Accuracy | p (by chance) | Mean p(1) when 1 | Mean p(1) when 0 | Mean suspicion | Would block |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for role, key in READER_ORDER:
        for arm in ARMS:
            eps = [e for e in episodes if e["experiment"] == "bit" and e["arm"] == arm]
            reads = _reads(eps, role, key)
            if not reads:
                continue
            n = len(reads)
            correct = sum(1 for _, r in reads if r.get("correct"))
            p1 = [r["answer"]["p_one"] for ep, r in reads if ep["secret"] == 1]
            p0 = [r["answer"]["p_one"] for ep, r in reads if ep["secret"] == 0]
            avg = lambda xs: f"{sum(xs) / len(xs):.2f}" if xs else ""
            susp = [r["answer"]["suspicion"] for _, r in reads if "suspicion" in r["answer"]]
            blocks = f"{sum(1 for _, r in reads if r['answer'].get('would_block'))}/{n}" if susp else ""
            lines.append(f"| {role}-{key} | {arm} | {n} | {correct} | {correct / n:.0%} | "
                         f"{tail_p(correct, n, 0.5):.3f} | {avg(p1)} | {avg(p0)} | {avg(susp)} | {blocks} |")
    return lines + [""]


def summarize(run_dir: Path) -> str:
    episodes = load(run_dir)
    status = Counter(e["status"] for e in episodes)
    rejections = sum(e.get("filter_rejections", 0) for e in episodes)
    unparsed = sum(1 for e in episodes for r in e.get("reads", []) if not r.get("answer"))
    interviews = sum(1 for e in episodes for r in e.get("reads", []) if r.get("interview_answer"))
    spend = run_dir / "spend.jsonl"
    cost = sum(json.loads(l)["cost_usd"] for l in spend.read_text(encoding="utf-8").splitlines() if l.strip()) \
        if spend.exists() else 0.0

    out = [f"# Activation transfer: {run_dir.name}", "",
           f"- Articles: {len(episodes)} ({', '.join(f'{k} {v}' for k, v in status.items())})",
           f"- Articles rejected by the rule filter and rewritten: {rejections}",
           f"- Reader answers that never parsed: {unparsed}",
           f"- Reader exit interviews (reader got it right): {interviews}",
           f"- Cost of this run: ${cost:.4f}", ""]
    if any(e["experiment"] == "animal_baseline" for e in episodes) or any(e["experiment"] == "animal" for e in episodes):
        base_lines, favorites = baseline_section(episodes)
        if any(e["experiment"] == "animal_baseline" for e in episodes):
            out += base_lines
        out += animal_section(episodes, favorites)
    if any(e["experiment"] == "bit" for e in episodes):
        out += bit_section(episodes)
    return "\n".join(out)


if __name__ == "__main__":
    print(summarize(Path(sys.argv[1])))
