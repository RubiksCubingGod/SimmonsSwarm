"""Render the sender-by-guard grid as a pivot."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from ..config import Config, compute_ids
from ..dataset import default_roots, scan_runs
from ..grid import build_grid, grid_payload, render_grid


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
        payload = grid_payload(report, runs)
        payload["skipped"] = skipped
        if report["refused"]:
            payload.update({"error": "mixed", "mixed": report["refused"]})
        print(json.dumps(payload, ensure_ascii=False))
    else:
        display_report = report
        other_arenas: list[tuple[str, str, int]] = []
        if args.arena is None:
            reference_arena = _reference_arena(Path.cwd().resolve())
            if reference_arena and any(
                table.get("split", {}).get("value") == reference_arena
                for table in report.get("slices", [])
            ):
                display_report = dict(report)
                display_report["slices"] = [
                    table for table in report["slices"]
                    if table.get("split", {}).get("value") == reference_arena
                ]
                counts: dict[str, int] = {}
                labels: dict[str, str] = {}
                for run in runs:
                    if not _has_valid_covert(run):
                        continue
                    arena_id = str(run.row.get("id.arena", ""))
                    if arena_id == reference_arena:
                        continue
                    counts[arena_id] = counts.get(arena_id, 0) + 1
                    labels.setdefault(arena_id, _arena_name(run))
                other_arenas = [
                    (arena_id, labels[arena_id], count)
                    for arena_id, count in sorted(counts.items())
                ]
        print(render_grid(display_report, markdown=args.md), end="")
        if other_arenas:
            lines = ["Other arenas (runs):"]
            lines.extend(f"  {arena_id} · {label}: {count}" for arena_id, label, count in other_arenas)
            lines.append("Use --arena <id-prefix> to show another arena.")
            print(("\n" if args.md else "") + "\n".join(lines))
    return 2 if report["refused"] else 0


def _reference_arena(root: Path) -> str | None:
    path = root / "experiments" / "reference.json"
    if not path.is_file():
        return None
    try:
        config = Config.model_validate(json.loads(path.read_text(encoding="utf-8")))
        return compute_ids(config, repo_root=root)["arena_hash"]
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, ValidationError):
        return None


def _has_valid_covert(run) -> bool:
    return any(
        episode.get("kind") == "covert"
        and isinstance(episode.get("guard"), dict)
        and isinstance(episode["guard"].get("blocked"), bool)
        for episode in run.episodes
    )


def _arena_name(run) -> str:
    option_set = str(run.row.get("cfg.option_set") or "unknown").rsplit("/", 1)[-1].removesuffix(".json")
    cover_task = str(run.row.get("cfg.cover_task") or "unknown").rsplit("/", 1)[-1].removesuffix(".md")
    count = run.row.get("cfg.options_per_episode")
    protected = run.row.get("cfg.protected_count")
    watchlist = "no watchlist" if protected is None else f"watchlist {protected}"
    options = f" · {count} options" if count is not None else ""
    return f"{option_set} · {cover_task}{options}, {watchlist}"
