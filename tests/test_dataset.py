from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from simmons.dataset import dataset_columns, scan_runs, write_dataset


FIXTURES = Path(__file__).parent / "fixtures" / "runs"


def test_fake_runs_are_excluded_unless_requested() -> None:
    runs, skipped = scan_runs([FIXTURES])
    assert runs == []
    assert len(skipped) == 7
    assert all(item["reason"] == "fake provider excluded" for item in skipped)

    runs, skipped = scan_runs([FIXTURES], include_fake=True)
    assert len(runs) == 7
    assert skipped == []


def test_run_level_rows_cover_config_and_metric_columns() -> None:
    runs, _ = scan_runs([FIXTURES], include_fake=True)
    columns = dataset_columns(run.row for run in runs)
    assert len(runs) == 7
    assert all(
        run.row["protocol_version"] == run.run["protocol_version"] for run in runs
    )
    assert {
        "run_id",
        "protocol_version",
        "id.config",
        "id.arena",
        "label.sender",
        "label.guard",
    } <= set(columns)
    assert {"cfg.option_set", "cfg.option_set.sha", "cfg.models.guard.model"} <= set(columns)
    assert {"hit_rate", "hit_rate.num", "decode_gap", "decode_gap.den", "auc"} <= set(columns)
    assert "axis.pair" not in columns  # Matrix axes belong to T21.


def test_stale_summary_is_recomputed_from_episode_bytes() -> None:
    runs, _ = scan_runs([FIXTURES], include_fake=True)
    run = runs[0]
    raw_hash = hashlib.sha256((run.run_dir / "episodes.jsonl").read_bytes()).hexdigest()
    assert run.summary["source"]["episodes_sha256"] == raw_hash
    assert run.summary["rates"]["hit_rate"]["den"] > 0


def test_corrupt_summary_is_recomputed_from_episodes(tmp_path: Path) -> None:
    original = next(path.parent for path in FIXTURES.rglob("run.json"))
    run_dir = tmp_path / original.name
    shutil.copytree(original, run_dir)
    (run_dir / "summary.json").write_text("{broken", encoding="utf-8")

    runs, skipped = scan_runs([tmp_path], include_fake=True)
    assert skipped == []
    assert len(runs) == 1
    assert runs[0].summary["rates"]["hit_rate"]["den"] > 0


def test_invalid_episode_record_is_reported_as_skipped(tmp_path: Path) -> None:
    original = next(path.parent for path in FIXTURES.rglob("run.json"))
    run_dir = tmp_path / original.name
    shutil.copytree(original, run_dir)
    episode_path = run_dir / "episodes.jsonl"
    episodes = [json.loads(line) for line in episode_path.read_text().splitlines()]
    episodes[0]["index"] = "invalid"
    episode_path.write_text("".join(json.dumps(row) + "\n" for row in episodes), encoding="utf-8")

    runs, skipped = scan_runs([tmp_path], include_fake=True)
    assert runs == []
    assert len(skipped) == 1
    assert "invalid" in skipped[0]["reason"]


def test_runs_are_deduplicated_across_roots(tmp_path: Path) -> None:
    original = next(path.parent for path in FIXTURES.rglob("run.json"))
    duplicate_root = tmp_path / "other-runs"
    duplicate = duplicate_root / original.name
    duplicate.parent.mkdir(parents=True)
    shutil.copytree(original, duplicate)

    runs, skipped = scan_runs([FIXTURES, duplicate_root], include_fake=True)
    assert len(runs) == 7
    assert len(skipped) == 1
    assert "duplicate run_id" in skipped[0]["reason"]


def test_where_and_csv_jsonl_writers(tmp_path: Path) -> None:
    runs, _ = scan_runs([FIXTURES], include_fake=True, where=["protected_count=null"])
    assert len(runs) == 6

    jsonl_path, csv_path, columns = write_dataset([run.row for run in runs], tmp_path)
    assert jsonl_path is not None and csv_path is not None
    jsonl_rows = [json.loads(line) for line in jsonl_path.read_text().splitlines()]
    assert len(jsonl_rows) == 6
    header = csv_path.read_text().splitlines()[0].split(",")
    assert header == columns
    assert sum(name.startswith("cfg.") for name in header) > 30

    bool_runs, _ = scan_runs([FIXTURES], include_fake=True, where=["guard_guesses_target=true"])
    assert len(bool_runs) == 7
