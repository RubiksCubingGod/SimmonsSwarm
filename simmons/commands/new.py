"""Create a new experiment containing only fields that differ from defaults."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from ..config import Config
from .validate import validate_experiment


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    command = subparsers.add_parser("new", help="create an experiment config")
    command.add_argument("name", help="new experiment name")
    command.add_argument("--from", dest="source", help="copy non-default fields from an experiment")
    command.set_defaults(handler=_run)


def _run(args: argparse.Namespace) -> int:
    root = Path.cwd().resolve()
    try:
        config = _source_config(args.source, root) if args.source else Config(name=args.name)
        created = Config(name=args.name)
    except (OSError, ValueError, ValidationError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    destination = root / "experiments" / f"{args.name}.json"
    if destination.exists():
        print(f"experiment already exists: {destination.relative_to(root)}", file=sys.stderr)
        return 2

    fields = config.model_dump(mode="json", exclude_defaults=True)
    fields["name"] = created.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(fields, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Created {destination.relative_to(root)}")
    return 0


def _source_config(value: str, root: Path) -> Config:
    if not value.strip():
        raise ValueError("--from requires a non-empty experiment name or JSON path")
    candidate = Path(value)
    if not candidate.parts:
        raise ValueError("--from requires an experiment name or JSON path")
    if not candidate.suffix:
        candidate = candidate.with_suffix(".json")
    if not candidate.is_absolute():
        if candidate.parts[0] == "experiments":
            candidate = root / candidate
        elif not candidate.exists():
            candidate = root / "experiments" / candidate
        else:
            candidate = root / candidate
    report = validate_experiment(candidate, str(candidate), root)
    if report.errors:
        details = "; ".join(f"{error['path']}: {error['message']}" for error in report.errors)
        raise ValueError(f"source experiment is invalid: {details}")
    if report.config is None:
        raise ValueError("source experiment has no config")
    return report.config


__all__ = ["register"]
