"""Regenerate a run summary from its frozen records."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..records import read_jsonl, write_metadata
from ..runner import _summary_record


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    command = subparsers.add_parser("summarize", help="regenerate summary.json from a run")
    command.add_argument("run_dir", type=Path, help="run directory containing run.json and episodes.jsonl")
    command.set_defaults(handler=_run)


def _run(args: argparse.Namespace) -> int:
    run_dir: Path = args.run_dir
    run_record = _read_object(run_dir / "run.json")
    config = _read_object(run_dir / "config.json")
    episode_path = run_dir / "episodes.jsonl"
    episode_bytes = episode_path.read_bytes()
    episodes = read_jsonl(episode_path)

    summary = _summary_record(run_record, config, episodes, episode_bytes)
    write_metadata(run_dir / "summary.json", summary)
    print(f"Wrote {run_dir / 'summary.json'} ({len(episodes)} episodes)")
    return 0


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value
