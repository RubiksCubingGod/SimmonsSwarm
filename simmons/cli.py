"""Thin CLI dispatcher that discovers every module in ``commands/``."""

from __future__ import annotations

import argparse
import importlib
import pkgutil
from pathlib import Path
from typing import Sequence

from . import __version__


def _discover_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    command_dir = Path(__file__).with_name("commands")
    for module_info in pkgutil.iter_modules([str(command_dir)]):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__package__}.commands.{module_info.name}")
        register = getattr(module, "register", None)
        if register is not None:
            register(subparsers)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="simmons")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")
    _discover_commands(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    result = handler(args)
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())

