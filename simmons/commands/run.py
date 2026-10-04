"""Run one experiment from a JSON config."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..runner import run_experiment


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    command = subparsers.add_parser("run", help="run an experiment config")
    command.add_argument("experiment", type=Path, help="experiment JSON file")
    command.add_argument("--fake", action="store_true", help="use fake:covert for every role")
    command.add_argument("--scratch", action="store_true", help="write this run under runs-local/")
    command.add_argument("--episodes", type=int, help="override the episode count")
    command.add_argument("--seed", type=int, help="override the plan seed")
    command.add_argument("--max-cost", type=float, help="lower the config's run cap")
    command.add_argument("--json", action="store_true", help="print one JSON result to stdout")
    command.add_argument("--dry-run", action="store_true", help="estimate spend without writing a run")
    command.set_defaults(handler=_run)


def _run(args: argparse.Namespace) -> int:
    argv = ["run", str(args.experiment)]
    for flag, value in (
        ("--fake", args.fake),
        ("--scratch", args.scratch),
        ("--json", args.json),
        ("--dry-run", args.dry_run),
    ):
        if value:
            argv.append(flag)
    for flag, value in (("--episodes", args.episodes), ("--seed", args.seed), ("--max-cost", args.max_cost)):
        if value is not None:
            argv.extend((flag, str(value)))
    code, result = run_experiment(
        args.experiment,
        fake=args.fake,
        scratch=args.scratch,
        episodes=args.episodes,
        seed=args.seed,
        max_cost=args.max_cost,
        dry_run=args.dry_run,
        argv=argv,
    )
    if args.json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    else:
        _print_result(result)
    return code


def _print_result(result: dict[str, Any]) -> None:
    status = result.get("status", "unknown")
    if result.get("run_dir"):
        outcomes = result.get("outcomes", {})
        counts = " ".join(f"{name}={count}" for name, count in sorted(outcomes.items()))
        spend = result.get("spent_usd")
        spend_text = f" spend=${spend:.4f}" if isinstance(spend, (int, float)) else ""
        outcomes_text = f" {counts}" if counts else ""
        print(f"{status}:{outcomes_text}{spend_text} {result['run_dir']}")
    elif "error" in result:
        print(f"{status}: {result['error']}")
    elif status == "dry_run" and isinstance(result.get("estimate"), dict):
        _print_estimate(result)
    else:
        print(json.dumps(result, sort_keys=True))


def _print_estimate(result: dict[str, Any]) -> None:
    detail = result["estimate"]
    kinds = " ".join(f"{name}={count}" for name, count in sorted(result.get("kinds", {}).items()))
    cap = result.get("cap_usd")
    cap_text = f" · run cap ${cap:.2f}" if isinstance(cap, (int, float)) else ""
    calls = detail.get("calls", {})
    print(f"dry_run: {result.get('experiment')} · {result.get('episodes')} episodes ({kinds})")
    print(
        f"estimate ${detail['estimate_usd']:.4f}{cap_text}"
        f" = episodes ${detail['episodes_usd']:.4f} ({calls.get('episodes', 0)} calls)"
        f" + preflight ${detail['preflight_usd']:.4f} ({calls.get('preflight', 0)} calls)"
    )
    assumptions = detail.get("assumptions", {})
    print(f"worst case ${detail['worst_case_usd']:.4f}: {assumptions.get('worst_case', '')}")
    print("assumed per call (output includes reasoning):")
    for role, item in assumptions.get("per_call", {}).items():
        print(
            f"  {role:<15} {item['model']}: {item['output_tokens']:,} output tokens"
            f" ({item['reasoning_tokens']:,} reasoning, max_tokens {item['max_tokens']:,})"
            f" × {item['retry_factor']:g} attempts"
        )
    print(f"input tokens: {assumptions.get('input_tokens')}; prices: {assumptions.get('prices')}")


__all__ = ["register"]
