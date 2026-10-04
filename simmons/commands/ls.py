"""List run records and summarize spend by author, experiment, and matrix."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..records import read_jsonl
from ..runner import _RunMetadata, _forked_run_ids, _iter_run_metadata

_STALE_AFTER = timedelta(minutes=10)
_COLUMNS = (
    ("experiment", "experiment"),
    ("cell", "matrix_cell"),
    ("run_id", "run_id"),
    ("author", "author"),
    ("status", "status"),
    ("episodes", "episodes_done"),
    ("hit_rate", "hit_rate"),
    ("false_alarm_rate", "false_alarm_rate"),
    ("spend_usd", "spent_usd"),
    ("size_bytes", "size_bytes"),
    ("flags", "flags"),
)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    command = subparsers.add_parser("ls", help="list runs")
    command.add_argument("--experiment", help="show runs for one experiment")
    command.add_argument("--matrix", help="show runs from one matrix")
    command.add_argument("--runs-dir", type=Path, default=Path("runs"), help="run root (default: runs)")
    command.add_argument("--cost", action="store_true", help="show spend totals by author, experiment, and matrix")
    command.add_argument("--json", action="store_true", help="print machine-readable output")
    command.set_defaults(handler=_run)


def _run(args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    runs_root = args.runs_dir.expanduser()
    if not runs_root.is_absolute():
        runs_root = repo_root / runs_root
    metadata = _iter_run_metadata((runs_root.resolve(),))
    forked = _forked_run_ids(metadata)
    rows = [
        _row(item, forked=forked)
        for item in metadata
        if _matches(item, experiment=args.experiment, matrix=args.matrix)
    ]
    rows.sort(key=lambda row: (row["experiment"], row["run_id"]))

    result: dict[str, Any] = {"runs": rows}
    if args.cost:
        result["cost"] = _cost_totals(rows)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        _print_rows(rows)
        if args.cost:
            _print_costs(result["cost"])
    return 0


def _matches(item: _RunMetadata, *, experiment: str | None, matrix: str | None) -> bool:
    run = item.run
    matrix_record = run.get("matrix")
    matrix_name = matrix_record.get("name") if isinstance(matrix_record, Mapping) else None
    if experiment is not None and run.get("experiment") != experiment:
        return False
    if matrix is not None and matrix_name != matrix:
        return False
    return True


def _row(item: _RunMetadata, *, forked: set[str]) -> dict[str, Any]:
    run = item.run
    run_id = item.run_id
    matrix_record = run.get("matrix")
    matrix_name = matrix_record.get("name") if isinstance(matrix_record, Mapping) else None
    matrix_cell = matrix_record.get("cell") if isinstance(matrix_record, Mapping) else None
    summary = _read_object(item.path / "summary.json")
    metrics = summary.get("rates", {})
    if not isinstance(metrics, Mapping):
        metrics = {}

    status = str(run.get("status", "unknown"))
    if status == "running" and _is_stale(item.path, run):
        status = "stale"
    is_forked = run_id in forked
    flags = ["fork"] if is_forked else []
    episodes_done = run.get("episodes_done")
    if type(episodes_done) is not int:
        episodes = _read_jsonl(item.path / "episodes.jsonl")
        episodes_done = len(episodes)

    return {
        "run_dir": str(item.path),
        "experiment": str(run.get("experiment", item.path.parent.name)),
        "matrix_cell": matrix_cell if isinstance(matrix_cell, str) else None,
        "matrix": matrix_name if isinstance(matrix_name, str) else None,
        "run_id": run_id,
        "author": str(run.get("author", "unknown")),
        "status": status,
        "episodes_done": episodes_done,
        "hit_rate": _rate_value(metrics.get("hit_rate")),
        "false_alarm_rate": _rate_value(metrics.get("false_alarm_rate")),
        "spent_usd": _number(run.get("spent_usd")),
        "size_bytes": _directory_size(item.path),
        "forked": is_forked,
        "flags": flags,
    }


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        return read_jsonl(path) if path.exists() else []
    except (OSError, ValueError):
        return []


def _rate_value(value: Any) -> float | None:
    if isinstance(value, Mapping):
        value = value.get("value")
    return float(value) if type(value) in (int, float) else None


def _number(value: Any) -> float:
    return float(value) if type(value) in (int, float) else 0.0


def _directory_size(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_stale(run_dir: Path, run: Mapping[str, Any]) -> bool:
    events = _read_jsonl(run_dir / "events.jsonl")
    last_event = _timestamp(events[-1].get("ts")) if events else None
    last_activity = last_event or _timestamp(run.get("started_at"))
    return last_activity is not None and datetime.now(timezone.utc) - last_activity > _STALE_AFTER


def _cost_totals(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    by_author: defaultdict[str, float] = defaultdict(float)
    by_experiment: defaultdict[str, float] = defaultdict(float)
    by_matrix: defaultdict[str, float] = defaultdict(float)
    for row in rows:
        spend = row["spent_usd"]
        by_author[row["author"]] += spend
        by_experiment[row["experiment"]] += spend
        if row["matrix"] is not None:
            by_matrix[row["matrix"]] += spend
    return {
        "by_author": dict(sorted(by_author.items())),
        "by_experiment": dict(sorted(by_experiment.items())),
        "by_matrix": dict(sorted(by_matrix.items())),
    }


def _print_rows(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("No runs found.")
        return
    text_rows = [[_format_cell(row.get(key)) for _label, key in _COLUMNS] for row in rows]
    headers = [label for label, _key in _COLUMNS]
    widths = [max(len(headers[i]), *(len(row[i]) for row in text_rows)) for i in range(len(headers))]
    print("  ".join(label.ljust(widths[i]) for i, label in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in text_rows:
        print("  ".join(value.ljust(widths[i]) for i, value in enumerate(row)))


def _format_cell(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, list):
        return ",".join(value) if value else "—"
    return str(value)


def _print_costs(cost: Mapping[str, Mapping[str, float]]) -> None:
    for dimension in ("by_author", "by_experiment", "by_matrix"):
        values = cost[dimension]
        if not values:
            continue
        print(f"\n{dimension[3:].replace('_', ' ').title()} spend")
        for label, amount in values.items():
            print(f"  {label}: ${amount:.4f}")


__all__ = ["register"]
