"""Render a run-level metric pivot."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..dataset import default_roots, scan_runs
from ..grid import filter_arena_runs, grid_display, render_grid, render_other_arenas, select_grid_runs
from ..pivot import DEFAULT_METRICS, build_pivot, json_payload, normalize_column, render_pivot


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    command = subparsers.add_parser("pivot", help="pivot metrics over run-level columns")
    command.add_argument("--rows", required=True, metavar="COL")
    command.add_argument("--cols", metavar="COL")
    command.add_argument("--metric", default=",".join(DEFAULT_METRICS), metavar="LIST")
    command.add_argument("--split", metavar="COL")
    command.add_argument("--arena", help="select an arena id prefix")
    command.add_argument("--where", action="append", default=[], metavar="FIELD=VALUE")
    command.add_argument("--runs-dir", action="append", type=Path, help="run root (repeatable)")
    command.add_argument("--include-local", action="store_true", help="also scan runs-local/")
    command.add_argument("--include-partial", action="store_true", help="include stopped or interrupted runs")
    command.add_argument("--include-fake", action="store_true", help="include runs using fake providers")
    command.add_argument("--allow-mixed", action="store_true", help="pool buckets that vary in other config fields")
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
        metrics = tuple(name.strip() for name in args.metric.split(",") if name.strip())
        grid_shape = (
            normalize_column(args.rows) == "id.sender"
            and normalize_column(args.cols) == "id.guard"
            and normalize_column(args.split) == "id.arena"
        )
        selected_runs = (
            select_grid_runs(runs, arena=args.arena)
            if grid_shape
            else filter_arena_runs(runs, args.arena)
        )
        report = build_pivot(
            selected_runs,
            rows=args.rows,
            cols=args.cols,
            split=args.split,
            selected_metrics=metrics,
            allow_mixed=args.allow_mixed,
        )
    except (OSError, ValueError) as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"pivot: {exc}", file=sys.stderr)
        return 2

    if args.json:
        payload = {**json_payload(report), "skipped": skipped}
        if report["refused"]:
            payload.update({"error": "mixed", "mixed": report["refused"]})
        print(json.dumps(payload, ensure_ascii=False))
    elif grid_shape:
        display_report, other_arenas = grid_display(
            report,
            selected_runs,
            arena=args.arena,
            repo_root=Path.cwd().resolve(),
        )
        renderer = render_grid if metrics == DEFAULT_METRICS else render_pivot
        rendered = renderer(display_report, markdown=args.md)
        footer = render_other_arenas(other_arenas)
        if footer:
            rendered += ("\n" if args.md else "") + footer + "\n"
        print(rendered, end="")
    else:
        print(render_pivot(report, markdown=args.md), end="")
    return 2 if report["refused"] else 0
