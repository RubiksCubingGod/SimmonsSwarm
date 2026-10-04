"""Generate a static browser view for one committed run."""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

from ..viewer import generate


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    command = subparsers.add_parser("view", help="write local HTML pages for a run")
    command.add_argument("run_dir", type=Path, help="run directory containing run.json and episodes.jsonl")
    command.add_argument("--open", action="store_true", help="open the generated run page in a browser")
    command.set_defaults(handler=_run)


def _run(args: argparse.Namespace) -> int:
    try:
        index, episodes = generate(args.run_dir)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"simmons view: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote {index} and {len(episodes)} episode pages")
    if args.open:
        opened = webbrowser.open(index.resolve().as_uri())
        if not opened:
            print(f"simmons view: could not open {index}", file=sys.stderr)
            return 1
        print(f"Opened {index.resolve().as_uri()}")
    return 0


__all__ = ["register"]
