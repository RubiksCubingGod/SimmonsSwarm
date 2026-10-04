"""Trajectory selection and run listing acceptance."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from simmons.cli import main
from simmons.commands.verify import verify_run
from simmons.records import canonical_json, read_jsonl
from simmons.runner import _find_trajectory, _forked_run_ids, _history_roots, _iter_run_metadata

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/runs/base/sender-a__guard-a"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(canonical_json(row) + "\n" for row in rows), encoding="utf-8")


def _copy_run(
    source: Path,
    runs_root: Path,
    run_id: str,
    *,
    trajectory: dict | None = None,
    selected: set[int] | None = None,
    offset: int = 0,
) -> Path:
    run = json.loads((source / "run.json").read_text(encoding="utf-8"))
    target = runs_root / "runs-local" / str(run["experiment"]) / run_id
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)

    episode_rows = read_jsonl(target / "episodes.jsonl")
    if selected is not None:
        episode_rows = [row for row in episode_rows if row["index"] in selected]
    for row in episode_rows:
        row["index"] += offset
        row["run_id"] = run_id
    _write_jsonl(target / "episodes.jsonl", episode_rows)

    attempt_rows = read_jsonl(target / "attempt-log.jsonl")
    if selected is not None:
        attempt_rows = [row for row in attempt_rows if row["episode"] in selected]
    for row in attempt_rows:
        row["episode"] += offset
    _write_jsonl(target / "attempt-log.jsonl", attempt_rows)

    run["run_id"] = run_id
    run["episodes_done"] = len(episode_rows)
    run["status"] = "complete"
    if trajectory is not None:
        run["trajectory"] = trajectory
    (target / "run.json").write_text(json.dumps(run), encoding="utf-8")
    return target


def _slot(source: Path = FIXTURE) -> tuple[str, int, int]:
    run = json.loads((source / "run.json").read_text(encoding="utf-8"))
    config = json.loads((source / "config.json").read_text(encoding="utf-8"))
    return run["ids"]["config"], config["seed"], len(read_jsonl(source / "attempt-log.jsonl"))


def test_trajectory_chooses_longest_fork_then_earlier_run_id(tmp_path: Path, monkeypatch):
    root = tmp_path / "storage"
    monkeypatch.setenv("SIMMONS_RUNS_ROOT", str(root))
    config_hash, seed, entries = _slot()
    parent_id = "20260101T000000Z-parent-0001"
    parent = _copy_run(FIXTURE, root, parent_id)
    inherited = [{"run_id": parent_id, "entries": entries}]
    long_a = _copy_run(
        parent,
        root,
        "20260101T000000Z-branch-a-0001",
        trajectory={"start": 20, "inherited_from": inherited},
        offset=20,
    )
    long_z = _copy_run(
        parent,
        root,
        "20260101T000000Z-branch-z-0001",
        trajectory={"start": 20, "inherited_from": inherited},
        offset=20,
    )
    short = _copy_run(
        parent,
        root,
        "20260101T000000Z-branch-short-0001",
        trajectory={"start": 20, "inherited_from": inherited},
        selected={0, 1, 2},
        offset=20,
    )

    start, attempts, lineage = _find_trajectory(
        root,
        local=True,
        config_hash=config_hash,
        seed=seed,
        log_scope="experiment",
    )

    assert start == 40
    assert [item["run_id"] for item in lineage] == [parent_id, long_a.name]
    assert lineage == [
        {"run_id": parent_id, "entries": entries},
        {"run_id": long_a.name, "entries": entries},
    ]
    assert [row["episode"] for row in attempts] == sorted(row["episode"] for row in attempts)
    metadata = _iter_run_metadata(_history_roots(root, local=True))
    assert {long_a.name, long_z.name, short.name} <= _forked_run_ids(metadata)


def test_trajectory_respects_local_committed_and_run_scopes(tmp_path: Path, monkeypatch):
    config_hash, seed, entries = _slot()
    local_root = tmp_path / "local-only"
    monkeypatch.setenv("SIMMONS_RUNS_ROOT", str(local_root))
    local = _copy_run(FIXTURE, local_root, "20260101T000000Z-local-0001")

    assert _find_trajectory(
        local_root,
        local=False,
        config_hash=config_hash,
        seed=seed,
        log_scope="experiment",
    ) == (0, [], [])
    start, attempts, lineage = _find_trajectory(
        local_root,
        local=True,
        config_hash=config_hash,
        seed=seed,
        log_scope="experiment",
    )
    assert start == 20
    assert len(attempts) == entries
    assert lineage == [{"run_id": local.name, "entries": entries}]
    assert _find_trajectory(
        local_root,
        local=True,
        config_hash=config_hash,
        seed=seed,
        log_scope="run",
    ) == (0, [], [])

    committed_root = tmp_path / "committed-only"
    monkeypatch.setenv("SIMMONS_RUNS_ROOT", str(committed_root))
    committed = committed_root / "runs" / "fixture" / "20260101T000000Z-committed-0001"
    committed.parent.mkdir(parents=True)
    shutil.copytree(FIXTURE, committed)
    run = json.loads((committed / "run.json").read_text(encoding="utf-8"))
    run["run_id"] = committed.name
    (committed / "run.json").write_text(json.dumps(run), encoding="utf-8")
    start, attempts, lineage = _find_trajectory(
        committed_root,
        local=True,
        config_hash=config_hash,
        seed=seed,
        log_scope="experiment",
    )
    assert start == 20
    assert len(attempts) == entries
    assert lineage == [{"run_id": committed.name, "entries": entries}]


def test_ls_reports_stale_status_forks_and_cost_by_matrix(tmp_path: Path, capsys):
    root = tmp_path / "listing"
    _config_hash, seed, entries = _slot()
    parent_id = "20260101T000000Z-parent-0001"
    parent = _copy_run(FIXTURE, root, parent_id)
    inherited = [{"run_id": parent_id, "entries": entries}]
    matrix_run = _copy_run(
        parent,
        root,
        "20260101T000000Z-matrix-a-0001",
        trajectory={"start": 20, "inherited_from": inherited},
        offset=20,
    )
    run = json.loads((matrix_run / "run.json").read_text(encoding="utf-8"))
    run["experiment"] = "monitoring-gap"
    run["matrix"] = {"name": "monitoring-gap", "cell": "sender-a__guard-a", "coords": {}, "seed": seed}
    run["spent_usd"] = 1.25
    (matrix_run / "run.json").write_text(json.dumps(run), encoding="utf-8")

    stale = _copy_run(
        parent,
        root,
        "20260101T000000Z-stale-0001",
        trajectory={"start": 20, "inherited_from": inherited},
        selected=set(),
        offset=20,
    )
    run = json.loads((stale / "run.json").read_text(encoding="utf-8"))
    run["status"] = "running"
    run["started_at"] = "2026-01-01T00:00:00Z"
    (stale / "run.json").write_text(json.dumps(run), encoding="utf-8")
    events = read_jsonl(stale / "events.jsonl")
    for event in events:
        event["ts"] = "2026-01-01T00:00:00Z"
    _write_jsonl(stale / "events.jsonl", events)

    assert main(["ls", "--runs-dir", str(root / "runs-local"), "--matrix", "monitoring-gap", "--cost", "--json"]) == 0
    filtered = json.loads(capsys.readouterr().out)
    assert len(filtered["runs"]) == 1
    assert filtered["runs"][0]["matrix_cell"] == "sender-a__guard-a"
    assert filtered["runs"][0]["forked"] is True
    assert filtered["cost"]["by_matrix"] == {"monitoring-gap": 1.25}

    assert main(["ls", "--runs-dir", str(root / "runs-local"), "--json"]) == 0
    all_runs = json.loads(capsys.readouterr().out)["runs"]
    stale_row = next(row for row in all_runs if row["run_id"] == stale.name)
    assert stale_row["status"] == "stale"
    assert stale_row["forked"] is True


def test_verify_checks_partial_continued_episode_bounds(tmp_path: Path):
    root = tmp_path / "partial"
    _config_hash, _seed, entries = _slot()
    parent_id = "20260101T000000Z-parent-0001"
    parent = _copy_run(FIXTURE, root, parent_id)
    partial = _copy_run(
        parent,
        root,
        "20260101T000000Z-partial-0001",
        trajectory={
            "start": 20,
            "inherited_from": [{"run_id": parent_id, "entries": entries}],
        },
        selected={0, 1, 2},
        offset=20,
    )
    run = json.loads((partial / "run.json").read_text(encoding="utf-8"))
    run["status"] = "stopped_budget"
    run["episodes_planned"] = 20
    run["episodes_done"] = 3
    (partial / "run.json").write_text(json.dumps(run), encoding="utf-8")
    episodes = read_jsonl(partial / "episodes.jsonl")
    assert [row["index"] for row in episodes] == [20, 21, 22]
    assert verify_run(partial) == []

    def errors_for(name: str, rows: list[dict]) -> list[str]:
        case_dir = tmp_path / name
        shutil.copytree(partial, case_dir)
        _write_jsonl(case_dir / "episodes.jsonl", rows)
        return verify_run(case_dir)

    missing_errors = errors_for(
        "partial-missing-index", [row for row in episodes if row["index"] != 20]
    )
    assert any("missing expected episode index 20" in error for error in missing_errors)

    completed_prefix_errors = errors_for(
        "partial-extra-completed-prefix", [*episodes[:2], {**episodes[2], "index": 23}]
    )
    assert any("beyond the stopped_budget run's completed prefix 20..22" in error for error in completed_prefix_errors)
    assert not any("outside 20..39" in error for error in completed_prefix_errors)

    planned_range_errors = errors_for(
        "partial-outside-planned-range", [*episodes[:2], {**episodes[2], "index": 40}]
    )
    assert any("outside 20..39" in error for error in planned_range_errors)

    duplicate_errors = errors_for("partial-duplicate-index", [*episodes, episodes[0]])
    assert any("index 20 has 2 records" in error for error in duplicate_errors)
