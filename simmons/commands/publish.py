"""Publish run views to the unlisted hosted viewer."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..env import load_env
from ..viewer import publish


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    command = subparsers.add_parser(
        "publish",
        help="publish committed runs and chosen scratch runs to the unlisted hosted viewer",
        description=(
            "Render every run under runs/ plus the scratch runs listed in "
            "runs-local/published.json, then deploy them to an unlisted URL."
        ),
    )
    command.add_argument(
        "run_dirs",
        type=Path,
        nargs="*",
        help="scratch run directories to add to the published list",
    )
    command.add_argument(
        "--remove",
        type=Path,
        action="append",
        default=[],
        metavar="RUN_DIR",
        help="take a scratch run off the published list (repeatable)",
    )
    command.add_argument(
        "--build-only",
        action="store_true",
        help="render the site into runs-local/.publish/site without deploying",
    )
    command.set_defaults(handler=_run)


def _run(args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    load_env(repo_root)
    try:
        state = publish.read_state(repo_root)
        keys = [publish.scratch_key(repo_root, path) for path in args.run_dirs]
        removed = {_removal_key(repo_root, path) for path in args.remove}
        runs = list(dict.fromkeys(k for k in [*state.runs, *keys] if k and k not in removed))
        if runs != state.runs or not state.saved:
            state.runs = runs
            path = publish.write_state(repo_root, state)
            print(f"Published list: {path} ({len(runs)} scratch runs)")
        target = publish.target_from_env(state)
        build = publish.build_site(repo_root, runs, target.path)
        print(f"Built {build.index} with {len(build.runs)} runs")
        for note in build.skipped:
            print(f"  skipped {note}", file=sys.stderr)
        if args.build_only:
            return 0
        publish.deploy(build, target)
        status = publish.check_live(target)
        if status is None:
            print(f"Deployed, but {target.url} is not reachable from here yet (new DNS can take minutes)")
        elif status != 200:
            print(f"simmons publish: {target.url} answered {status} after deploy", file=sys.stderr)
            return 1
        print(f"Published {target.url}")
    except publish.PublishError as exc:
        print(f"simmons publish: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"simmons publish: {exc}", file=sys.stderr)
        return 2
    return 0


def _removal_key(repo_root: Path, path: Path) -> str | None:
    """Accept a run directory, or the listed key of a run no longer on disk."""
    if (path / "run.json").is_file():
        return publish.scratch_key(repo_root, path)
    key = path.as_posix()
    return key.removeprefix("runs-local/").strip("/")


__all__ = ["register"]
