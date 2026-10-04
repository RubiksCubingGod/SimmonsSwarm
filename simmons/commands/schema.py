"""Print the experiment config's JSON Schema."""

from __future__ import annotations

import argparse
import json

from ..config import Config


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    command = subparsers.add_parser("schema", help="print the experiment config JSON Schema")
    command.set_defaults(handler=_run)


def _run(_: argparse.Namespace) -> int:
    print(json.dumps(Config.model_json_schema(), indent=2, ensure_ascii=False))
    return 0


__all__ = ["register"]
