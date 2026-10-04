"""Render the sender-by-guard grid as a pivot."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..dataset import default_roots, scan_runs
from ..grid import (
    build_grid,
    grid_display,
    grid_payload,
    render_grid,
    render_other_arenas,
    select_grid_runs,
)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    command = subparsers.add_parser("grid", help="show the sender-by-guard matchup grid")
    command.add_argument("--arena", help="select an arena id prefix")
    command.add_argument("--where", action="append", default=[], metavar="FIELD=VALUE")
    command.add_argument("--runs-dir", action="append", type=Path, help="run root (repeatable)")
    command.add_argument("--include-local", action="store_true", help="also scan runs-local/")
    command.add_argument("--include-partial", action="store_true", help="include stopped or interrupted runs")
    command.add_argument("--include-fake", action="store_true", help="include runs using fake providers")
    command.add_argument("--allow-mixed", action="store_true", help="pool mixed buckets")
    output = command.add_mutually_exclusive_group()
    output.add_argument("--md", action="store_true", help="render Markdown")
    output.add_argument("--json", action="store_true", help="print machine-readable output")
    command.set_defaults(handler=_run)


def _run(args: argparse.Namespace) -> int:
    try:
        roots = default_roots(args.runs_dir, include_local=args.include_local, repo_root=Path.cwd())
        runs, skipped = scan_runs(
            roots,
            include_partial=args.include_partial,
            include_fake=args.include_fake,
            where=args.where,
        )
        report = build_grid(runs, arena=args.arena, allow_mixed=args.allow_mixed)
    except (OSError, ValueError) as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"grid: {exc}", file=sys.stderr)
        return 2

    if args.json:
        payload = grid_payload(report, select_grid_runs(runs, arena=args.arena))
        payload["skipped"] = skipped
        if report["refused"]:
            payload.update({"error": "mixed", "mixed": report["refused"]})
        print(json.dumps(payload, ensure_ascii=False))
    else:
        display_report, other_arenas = grid_display(
            report,
            runs,
            arena=args.arena,
            repo_root=Path.cwd().resolve(),
        )
        rendered = render_grid(display_report, markdown=args.md)
        footer = render_other_arenas(other_arenas)
        if footer:
            rendered += ("\n" if args.md else "") + footer + "\n"
        print(rendered, end="")
    return 2 if report["refused"] else 0
