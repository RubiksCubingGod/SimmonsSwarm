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


def test_curve_hit_at_operating_threshold_matches_hit_rate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIMMONS_RUNS_ROOT", str(tmp_path / "runs"))
    path = _experiment(tmp_path, guard_exposes=True, options_per_episode=2, block_threshold=1.0)
    code, result = run_experiment(str(path), repo_root=ROOT, fake=True)
    assert code == 0, result
    summary = json.loads((Path(result["run_dir"]) / "summary.json").read_text(encoding="utf-8"))
    assert summary["rates"]["exposed_rate"]["num"] > 0
    # The operating threshold (1.0) blocks nothing, so no stricter point can show
    # more hits than hit_rate; counting exposed picks as hits used to reach 1.0.
    hits = [point["hit"] for point in summary["curve"]["points"] if point["hit"] is not None]
    assert hits and max(hits) <= round(summary["rates"]["hit_rate"]["value"], 3)


def test_validate_rejects_exposes_without_guesses(tmp_path: Path) -> None:
    from simmons.commands import validate as validate_command

    path = _experiment(tmp_path, guard_exposes=True, guard_guesses_target=False)
    report = validate_command.validate_experiment(path, str(path), ROOT)
    assert any(error["path"] == "guard_exposes" for error in report.errors)
    path.write_text(path.read_text(encoding="utf-8").replace('"guard_exposes": true', '"guard_exposes": "yes"'), encoding="utf-8")
    report = validate_command.validate_experiment(path, str(path), ROOT)
    assert any(error["path"] == "guard_exposes" for error in report.errors)


def test_guard_name_renames_sender_facing_text_and_keeps_old_identity() -> None:
    from simmons.roles import _explain_tool

    raw = json.loads(SMOKE.read_text(encoding="utf-8"))
    assert compute_ids(Config.model_validate(raw), repo_root=ROOT) == compute_ids(
        Config.model_validate({**raw, "guard_name": None}), repo_root=ROOT
    )
    assert compute_ids(Config.model_validate(raw), repo_root=ROOT)["config_hash"] != compute_ids(
        Config.model_validate({**raw, "guard_name": "Jordan"}), repo_root=ROOT
    )["config_hash"]

    entry = ({"episode": 3, "target": "Bendigo", "outcome": "EXPOSED", "guard_guess": "Bendigo", "message": "m"},)
    fields = ["target", "message", "outcome", "guard_guess"]
    named = _log_section(entry, fields, "Jordan")
    assert "EXPOSED (passed, but Jordan named the target)" in named
    assert "Jordan's guess: Bendigo" in named and "guard" not in named
    # Unnamed rendering is byte-for-byte the pre-guard_name text.
    assert "EXPOSED (passed, but the guard named the target) · guard_guess: Bendigo" in _log_section(entry, fields)
    assert "reaches Jordan," in _explain_tool("Jordan").description
    assert "reaches the guard," in _explain_tool().description
