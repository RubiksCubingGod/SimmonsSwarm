"""Generate the small offline run corpus committed with the test fixtures."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from simmons.runner import run_experiment

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent


def _base_config(name: str, sender: str, guard: str, index: int) -> dict[str, object]:
    sender_model = "fake:random" if index == 2 else "fake:covert"
    guard_model = "fake:random" if index % 2 else "fake:covert"
    receiver_model = "fake:random" if index % 3 == 1 else "fake:covert"
    return {
        "name": name,
        "description": "Offline sender-by-guard fixture run.",
        "option_set": "tests/fixtures/option-sets/smoke-100.json",
        "episodes": 20,
        "seed": 7,
        "sender_prompt": f"tests/fixtures/prompts/{sender}.md",
        "guard_prompt": f"tests/fixtures/prompts/{guard}.md",
        "plain_sender_prompt": "tests/fixtures/prompts/plain-reference.md",
        "cover_task": "tests/fixtures/cover-task.md",
        "templates": "templates/au-meeting",
        "models": {
            "sender": sender_model,
            "plain_sender": "fake:covert",
            "guard": guard_model,
            "receiver": receiver_model,
        },
        "sender_debrief": "blind",
    }


def _run_fixture(config: dict[str, object], destination: Path, temporary: Path) -> None:
    config_path = temporary / f"{config['name']}.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    code, result = run_experiment(config_path, repo_root=ROOT, scratch=True)
    if code != 0:
        raise RuntimeError(f"fixture run {config['name']} failed: {result}")
    source = Path(str(result["run_dir"]))
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)


def main() -> None:
    previous = os.environ.get("SIMMONS_RUNS_ROOT")
    try:
        with tempfile.TemporaryDirectory(prefix="simmons-fixture-runs-") as raw_temp:
            temporary = Path(raw_temp)
            os.environ["SIMMONS_RUNS_ROOT"] = str(temporary / "generated")
            names = ("sender-a", "sender-b", "sender-c")
            guards = ("guard-a", "guard-b")
            index = 0
            for sender in names:
                for guard in guards:
                    name = f"fixture-base-{sender}-{guard}"
                    config = _base_config(name, sender, guard, index)
                    _run_fixture(config, FIXTURES / "runs" / "base" / f"{sender}__{guard}", temporary)
                    index += 1

            watched = _base_config("fixture-watchlist", "sender-a", "guard-b", 7)
            watched.update(
                {
                    "description": "Watchlist fixture exercising all four episode kinds.",
                    "protected_count": 10,
                    "control_rate": 0.2,
                    "plain_protected_share": 0.5,
                }
            )
            _run_fixture(watched, FIXTURES / "runs" / "watchlist" / "all-kinds", temporary)
    finally:
        if previous is None:
            os.environ.pop("SIMMONS_RUNS_ROOT", None)
        else:
            os.environ["SIMMONS_RUNS_ROOT"] = previous


if __name__ == "__main__":
    main()
