"""Generate static browser views for one run, or an index over every run."""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

from ..runner import _base_runs_root
from ..viewer import all_runs_index, generate
from ..viewer.runs_index import generate_index


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    command = subparsers.add_parser("view", help="write local HTML pages for a run, or for all runs")
    command.add_argument(
        "run_dir", type=Path, nargs="?", help="run directory containing run.json and episodes.jsonl"
    )
    command.add_argument(
        "--all",
        action="store_true",
        help="index every run under runs/ and runs-local/ in runs-local/index.html",
    )
    command.add_argument("--open", action="store_true", help="open the generated page in a browser")
    command.set_defaults(handler=_run)


def _run(args: argparse.Namespace) -> int:
    index_all = bool(getattr(args, "all", False))
    if index_all == (args.run_dir is not None):
        print("simmons view: pass a run directory or --all", file=sys.stderr)
        return 2
    try:
        if index_all:
            repo_root = Path(__file__).resolve().parents[2]
            local_root = _base_runs_root(repo_root, True)
            roots = (_base_runs_root(repo_root, False), local_root)
            index, data = generate_index(roots, local_root / "index.html")
            print(f"Wrote {index} listing {len(data['runs'])} runs")
        else:
            index, episodes = generate(args.run_dir)
            print(f"Wrote {index} and {len(episodes)} episode pages")
            listing = all_runs_index(args.run_dir)
            if listing is not None:
                # Keep the page the run's "All runs" link points to current.
                base = listing.parent.parent
                generate_index((base / "runs", base / "runs-local"), listing)
                print(f"Updated {listing}")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"simmons view: {exc}", file=sys.stderr)
        return 2
    if args.open:
        opened = webbrowser.open(index.resolve().as_uri())
        if not opened:
            print(f"simmons view: could not open {index}", file=sys.stderr)
            return 1
        print(f"Opened {index.resolve().as_uri()}")
    return 0


__all__ = ["register"]
