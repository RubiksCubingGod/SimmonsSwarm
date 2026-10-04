"""Regenerate a run summary from its frozen records."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .. import metrics
from ..config import PROTOCOL_VERSION
from ..records import read_jsonl, write_metadata


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

    summary_metrics = metrics.summarize_with_config(episodes, config=config)
    summary_metrics["counts"]["planned"] = int(
        run_record.get("episodes_planned", config.get("episodes", len(episodes)))
    )
    summary_metrics["counts"]["done"] = len(episodes)

    protocol_version = run_record.get(
        "protocol_version", config.get("protocol_version", PROTOCOL_VERSION)
    )
    summary: dict[str, Any] = {
        "schema": "simmons.summary/1",
        "run_id": run_record["run_id"],
        "experiment": run_record["experiment"],
        "status": run_record["status"],
        "ids": run_record.get("ids", {}),
        "matrix": run_record.get("matrix"),
        "labels": _labels(config),
        "source": {
            "episodes_sha256": hashlib.sha256(episode_bytes).hexdigest(),
            "protocol_version": protocol_version,
        },
        **summary_metrics,
    }
    write_metadata(run_dir / "summary.json", summary)
    print(f"Wrote {run_dir / 'summary.json'} ({len(episodes)} episodes)")
    return 0


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def _labels(config: Mapping[str, Any]) -> dict[str, str]:
    models = config.get("models", {})
    labels: dict[str, str] = {}
    for role in ("sender", "guard"):
        prompt = config.get(f"{role}_prompt")
        prompt_label = Path(prompt).stem if isinstance(prompt, str) else role
        model = models.get(role) if isinstance(models, Mapping) else models
        model_label = _model_label(model)
        labels[role] = f"{prompt_label} · {model_label}" if model_label else prompt_label
    return labels


def _model_label(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("model", value.get("id", ""))
    if not isinstance(value, str) or not value:
        return ""
    _, separator, model = value.partition(":")
    value = model if separator else value
    return value.rsplit("/", 1)[-1]
