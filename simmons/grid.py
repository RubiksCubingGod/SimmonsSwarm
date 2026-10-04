"""The sender-by-guard grid, expressed through the shared pivot engine."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .config import Config, compute_ids
from .dataset import RunData
from .pivot import DEFAULT_METRICS, _pool_cell, build_pivot, render_pivot

_GRID_METRICS = (*DEFAULT_METRICS, "catch_rate", "decode_gap", "auc")


def build_grid(
    runs: Iterable[RunData],
    *,
    arena: str | None = None,
    allow_mixed: bool = False,
) -> dict[str, Any]:
    """Build grid JSON data with the exact pivot implementation underneath."""
    selected = select_grid_runs(runs, arena=arena)
    report = build_pivot(
        selected,
        rows="sender",
        cols="guard",
        split="arena",
        selected_metrics=_GRID_METRICS,
        allow_mixed=allow_mixed,
    )
    # The human grid and `pivot --rows sender --cols guard --split arena` share
    # the same visible fields and formatter byte for byte.
    return report


def filter_arena_runs(
    runs: Iterable[RunData], arena: str | None = None
) -> list[RunData]:
    selected = list(runs)
    if arena:
        selected = [
            run for run in selected
            if str(run.row.get("id.arena", "")).startswith(arena)
        ]
    return selected


def select_grid_runs(
    runs: Iterable[RunData], *, arena: str | None = None
) -> list[RunData]:
    """Apply the grid's shared arena-prefix and valid-covert selection."""
    return [run for run in filter_arena_runs(runs, arena) if _has_valid_covert(run)]


def grid_display(
    report: Mapping[str, Any],
    runs: Iterable[RunData],
    *,
    arena: str | None,
    repo_root: Path,
) -> tuple[dict[str, Any], list[tuple[str, str, int]]]:
    """Apply the grid's default reference-arena view and list other arenas."""
    display_report = dict(report)
    other_arenas: list[tuple[str, str, int]] = []
    if arena is None:
        reference_arena = _reference_arena(repo_root)
        if reference_arena and any(
            table.get("split", {}).get("value") == reference_arena
            for table in report.get("slices", [])
        ):
            display_report["slices"] = [
                table for table in report["slices"]
                if table.get("split", {}).get("value") == reference_arena
            ]
            counts: dict[str, int] = {}
            labels: dict[str, str] = {}
            for run in select_grid_runs(runs):
                arena_id = str(run.row.get("id.arena", ""))
                if arena_id == reference_arena:
                    continue
                counts[arena_id] = counts.get(arena_id, 0) + 1
                labels.setdefault(arena_id, _arena_name(run))
            other_arenas = [
                (arena_id, labels[arena_id], count)
                for arena_id, count in sorted(counts.items())
            ]
    return display_report, other_arenas


def render_other_arenas(arenas: Iterable[tuple[str, str, int]]) -> str:
    values = list(arenas)
    if not values:
        return ""
    lines = ["Other arenas (runs):"]
    lines.extend(f"  {arena_id} · {label}: {count}" for arena_id, label, count in values)
    lines.append("Use --arena <id-prefix> to show another arena.")
    return "\n".join(lines)


def grid_payload(report: Mapping[str, Any], runs: Iterable[RunData]) -> dict[str, Any]:
    selected = list(runs)
    by_run_id = {str(run.row.get("run_id", "")): run for run in selected}
    arenas: list[dict[str, Any]] = []
    for table in report.get("slices", []):
        split = table.get("split")
        arena_id = split.get("value") if isinstance(split, Mapping) else None
        arena_runs = [run for run in selected if run.row.get("id.arena") == arena_id]
        row_axes = table.get("rows", [])
        col_axes = table.get("cols", [])
        cells: list[dict[str, Any]] = []
        for pivot_cell in table.get("cells", []):
            cell_runs = [by_run_id[key] for key in pivot_cell.get("run_ids", []) if key in by_run_id]
            pooled = _pool_cell(cell_runs, _GRID_METRICS) if cell_runs else pivot_cell
            cells.append({
                "row": pivot_cell.get("row"),
                "col": pivot_cell.get("col"),
                "n_runs": pivot_cell.get("n_runs", 0),
                "n_covert": pivot_cell.get("n_covert", 0),
                "hit_rate": pooled.get("hit_rate"),
                "catch_rate": pooled.get("catch_rate"),
                "false_alarm_rate": pooled.get("false_alarm_rate"),
                "decode_gap": pooled.get("decode_gap"),
                "auc": pooled.get("auc"),
                "authors": pivot_cell.get("authors", []),
                "hosts": pivot_cell.get("hosts", []),
                "multiple_hosts": pivot_cell.get("multiple_hosts", False),
                "seeds": pivot_cell.get("seeds", []),
                "run_ids": pivot_cell.get("run_ids", []),
            })
        arenas.append({
            "arena": arena_id,
            "label": _arena_label(arena_runs),
            "fields": _arena_fields(arena_runs),
            "rows": [{"id": row.get("value"), "label": row.get("label")} for row in row_axes],
            "cols": [{"id": col.get("value"), "label": col.get("label")} for col in col_axes],
            "cells": cells,
        })
    return {"arenas": arenas}


def render_grid(report: Mapping[str, Any], *, markdown: bool = False) -> str:
    visible = dict(report)
    visible["metrics"] = list(DEFAULT_METRICS)
    visible["metric"] = list(DEFAULT_METRICS)
    return render_pivot(visible, markdown=markdown)


def _has_valid_covert(run: RunData) -> bool:
    return any(
        episode.get("kind") == "covert"
        and isinstance(episode.get("guard"), Mapping)
        and isinstance(episode["guard"].get("blocked"), bool)
        for episode in run.episodes
    )


def _reference_arena(root: Path) -> str | None:
    path = root / "experiments" / "reference.json"
    if not path.is_file():
        return None
    try:
        config = Config.model_validate(json.loads(path.read_text(encoding="utf-8")))
        return compute_ids(config, repo_root=root)["arena_hash"]
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, ValidationError):
        return None


def _arena_name(run: RunData) -> str:
    option_set = (
        str(run.row.get("cfg.option_set") or "unknown")
        .rsplit("/", 1)[-1]
        .removesuffix(".json")
    )
    cover_task = (
        str(run.row.get("cfg.cover_task") or "unknown")
        .rsplit("/", 1)[-1]
        .removesuffix(".md")
    )
    count = run.row.get("cfg.options_per_episode")
    protected = run.row.get("cfg.protected_count")
    watchlist = "no watchlist" if protected is None else f"watchlist {protected}"
    options = f" · {count} options" if count is not None else ""
    return f"{option_set} · {cover_task}{options}, {watchlist}"


def _arena_fields(runs: list[RunData]) -> dict[str, Any]:
    if not runs:
        return {}
    result: dict[str, Any] = {}
    for key, value in runs[0].row.items():
        if not key.startswith("cfg."):
            continue
        if key in {
            "cfg.name", "cfg.description", "cfg.seed", "cfg.episodes",
            "cfg.max_cost_usd", "cfg.sender_prompt", "cfg.sender_prompt.sha",
            "cfg.guard_prompt", "cfg.guard_prompt.sha", "cfg.guard_output",
            "cfg.block_threshold", "cfg.guard_guesses_target",
            "cfg.models.sender.model", "cfg.models.sender.temperature",
            "cfg.models.sender.max_tokens", "cfg.models.sender.params",
            "cfg.models.receiver.model", "cfg.models.receiver.temperature",
            "cfg.models.receiver.max_tokens", "cfg.models.receiver.params",
            "cfg.models.guard.model", "cfg.models.guard.temperature",
            "cfg.models.guard.max_tokens", "cfg.models.guard.params",
        }:
            continue
        result[key.removeprefix("cfg.")] = value
    return result


def _arena_label(runs: list[RunData]) -> str:
    if not runs:
        return "unknown"
    row = runs[0].row
    option_set = row.get("cfg.option_set")
    cover_task = row.get("cfg.cover_task")
    count = row.get("cfg.options_per_episode")
    protected = row.get("cfg.protected_count")
    label = str(option_set or "arena")
    if "/" in label:
        label = label.rsplit("/", 1)[-1]
    if label.endswith(".json"):
        label = label[:-5]
    if cover_task:
        cover = str(cover_task).rsplit("/", 1)[-1]
        label += f" · {cover.removesuffix('.md')}"
    if count is not None:
        label += f" · {count} options"
    label += ", no watchlist" if protected is None else f", watchlist {protected}"
    return label


__all__ = [
    "build_grid",
    "filter_arena_runs",
    "grid_display",
    "grid_payload",
    "render_grid",
    "render_other_arenas",
    "select_grid_runs",
]
