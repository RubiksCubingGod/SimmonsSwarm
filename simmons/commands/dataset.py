"""Write the run-level dataset as CSV and JSONL."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..dataset import default_roots, scan_runs, write_dataset


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    command = subparsers.add_parser("dataset", help="write the run-level dataset")
    command.add_argument("--runs-dir", action="append", type=Path, help="run root (repeatable)")
    command.add_argument("--include-local", action="store_true", help="also scan runs-local/")
    command.add_argument("--include-partial", action="store_true", help="include stopped or interrupted runs")
    command.add_argument("--include-fake", action="store_true", help="include runs using fake providers")
    command.add_argument("--where", action="append", default=[], metavar="FIELD=VALUE")
    output = command.add_mutually_exclusive_group()
    output.add_argument("--stdout", action="store_true", help="stream JSONL rows to stdout")
    output.add_argument("--json", action="store_true", help="print output paths and counts as JSON")
    command.set_defaults(handler=_run)


def _run(args: argparse.Namespace) -> int:
    root = Path.cwd().resolve()
    try:
        roots = default_roots(args.runs_dir, include_local=args.include_local, repo_root=root)
        runs, skipped = scan_runs(
            roots,
            include_partial=args.include_partial,
            include_fake=args.include_fake,
            where=args.where,
        )
        rows = [run.row for run in runs]
        jsonl_path, csv_path, columns = write_dataset(rows, root / "results", stdout=args.stdout)
    except (OSError, ValueError) as exc:
        print(f"dataset: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({
            "jsonl": jsonl_path.relative_to(root).as_posix() if jsonl_path is not None else None,
            "csv": csv_path.relative_to(root).as_posix() if csv_path is not None else None,
            "rows": len(rows),
            "columns": len(columns),
            "skipped": skipped,
        }, ensure_ascii=False))
    elif not args.stdout:
        print(f"Wrote {len(rows)} rows to {jsonl_path} and {csv_path}", file=sys.stderr)
        for item in skipped:
            print(f"Skipped {item['run_dir']}: {item['reason']}", file=sys.stderr)
    return 0
