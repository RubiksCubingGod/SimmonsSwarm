"""Hermetic checks for the opt-in EXPOSED outcome (guard_exposes)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from simmons.commands.verify import verify_run
from simmons.config import Config, compute_ids
from simmons.records import read_jsonl
from simmons.runner import run_experiment
from simmons.visibility import _log_section

ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "experiments" / "smoke.json"


def _experiment(tmp_path: Path, **fields) -> Path:
    raw = json.loads(SMOKE.read_text(encoding="utf-8"))
    raw.update({"name": "exposed-smoke", "episodes": 40, **fields})
    for key in ("option_set", "sender_prompt", "guard_prompt", "plain_sender_prompt", "cover_task", "templates"):
        raw[key] = str(ROOT / raw[key])
    path = tmp_path / "exposed-smoke.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def _guard_named_target(row) -> bool:
    guess = (row.get("guard") or {}).get("guess")
    return isinstance(guess, dict) and guess.get("index") == row["target"]["index"]


def test_guard_exposes_turns_named_passes_into_exposed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIMMONS_RUNS_ROOT", str(tmp_path / "runs"))
    code, result = run_experiment(str(_experiment(tmp_path, guard_exposes=True, options_per_episode=2, block_threshold=1.0)), repo_root=ROOT, fake=True)
    assert code == 0, result
    run_dir = Path(result["run_dir"])
    episodes = read_jsonl(run_dir / "episodes.jsonl")

    exposed = [row for row in episodes if row["outcome"] == "EXPOSED"]
    assert exposed, "the fake guard should name some targets"
    for row in exposed:
        assert row["kind"] in {"covert", "control"}
        assert row["guard"]["blocked"] is False
        assert _guard_named_target(row)
        assert row["pick"] is not None and row["score"] is not None
    for row in episodes:
        if row["outcome"] == "HIT" and row["kind"] in {"covert", "control"}:
            assert not _guard_named_target(row)
        if row["kind"].startswith("plain"):
            assert row["outcome"] != "EXPOSED"

    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["rates"]["exposed_rate"]["num"] == len(exposed)
    # Receiver decode still credits a correct pick on an exposed episode.
    passed = [row for row in episodes if row["kind"] == "covert" and row["outcome"] in {"HIT", "MISS", "EXPOSED"}]
    assert summary["decode"]["receiver"]["num"] == sum(row["score"]["hit"] for row in passed)

    log = read_jsonl(run_dir / "attempt-log.jsonl")
    assert any(entry["outcome"] == "EXPOSED" for entry in log)
    assert verify_run(run_dir) == []


def test_guard_exposes_off_keeps_identity_and_outcomes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = json.loads(SMOKE.read_text(encoding="utf-8"))
    assert compute_ids(Config.model_validate(raw), repo_root=ROOT) == compute_ids(
        Config.model_validate({**raw, "guard_exposes": False}), repo_root=ROOT
    )
    assert compute_ids(Config.model_validate(raw), repo_root=ROOT)["config_hash"] != compute_ids(
        Config.model_validate({**raw, "guard_exposes": True}), repo_root=ROOT
    )["config_hash"]

    monkeypatch.setenv("SIMMONS_RUNS_ROOT", str(tmp_path / "runs"))
    code, result = run_experiment(str(_experiment(tmp_path)), repo_root=ROOT, fake=True)
    assert code == 0, result
    assert all(row["outcome"] != "EXPOSED" for row in read_jsonl(Path(result["run_dir"]) / "episodes.jsonl"))


def test_sender_log_explains_exposed() -> None:
    section = _log_section(({"episode": 3, "target": "Bendigo", "outcome": "EXPOSED", "message": "m"},), ["target", "message", "outcome"])
    assert "EXPOSED (passed, but the guard named the target)" in section
