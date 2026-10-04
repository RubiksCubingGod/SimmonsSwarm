from __future__ import annotations

import json
import shutil
from pathlib import Path

from simmons.cli import main
from simmons.config import Config

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"
FIXTURE_RUNS = FIXTURE_ROOT / "runs"


def test_pivot_cli_refuses_mixed_fixture_and_names_every_field(capsys):
    assert (
        main(
            [
                "pivot",
                "--rows",
                "sender",
                "--cols",
                "guard",
                "--runs-dir",
                str(FIXTURE_RUNS),
                "--include-fake",
            ]
        )
        == 2
    )
    refusal_text = capsys.readouterr().out
    assert "cfg.control_rate" in refusal_text

    assert (
        main(
            [
                "pivot",
                "--rows",
                "sender",
                "--cols",
                "guard",
                "--runs-dir",
                str(FIXTURE_RUNS),
                "--include-fake",
                "--json",
            ]
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "mixed"
    fields = {item["field"] for item in payload["mixed"]}
    assert {"cfg.control_rate", "cfg.protected_count"} <= fields
    assert all(field in refusal_text for field in fields)


def test_protocol_version_is_identity_unless_split_or_mixed(tmp_path: Path, capsys):
    runs_root = tmp_path / "runs"
    source = FIXTURE_RUNS / "base" / "sender-a__guard-a"
    for version in (1, 2):
        run_dir = runs_root / f"protocol-{version}"
        shutil.copytree(source, run_dir)
        run_path = run_dir / "run.json"
        run = json.loads(run_path.read_text(encoding="utf-8"))
        run["run_id"] = f"protocol-{version}"
        run["protocol_version"] = version
        run["ids"]["config"] = f"config-{version}"
        run_path.write_text(json.dumps(run), encoding="utf-8")

        config_path = run_dir / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["protocol_version"] = version
        config_path.write_text(json.dumps(config), encoding="utf-8")

        episodes_path = run_dir / "episodes.jsonl"
        episodes = [
            json.loads(line)
            for line in episodes_path.read_text(encoding="utf-8").splitlines()
        ]
        for episode in episodes:
            episode["run_id"] = f"protocol-{version}"
        episodes_path.write_text(
            "".join(json.dumps(episode) + "\n" for episode in episodes),
            encoding="utf-8",
        )

    argv = [
        "pivot",
        "--rows",
        "sender",
        "--cols",
        "guard",
        "--runs-dir",
        str(runs_root),
        "--include-fake",
    ]
    assert main(argv) == 2
    assert "protocol_version" in capsys.readouterr().out

    assert main([*argv, "--where", "protocol_version=1", "--json"]) == 0
    filtered_payload = json.loads(capsys.readouterr().out)
    assert len(filtered_payload.get("cells", [])) == 1, (
        "Expected protocol-1 cell; matching run was excluded",
        filtered_payload,
    )
    assert filtered_payload["cells"][0]["n_runs"] == 1
    assert filtered_payload["cells"][0]["run_ids"] == ["protocol-1"]

    assert main([*argv, "--split", "protocol_version", "--json"]) == 0
    split_payload = json.loads(capsys.readouterr().out)
    assert split_payload["split"] == "protocol_version"
    assert len(split_payload["pivots"]) == 2

    assert main([*argv, "--allow-mixed", "--json"]) == 0
    mixed_payload = json.loads(capsys.readouterr().out)
    assert any(
        item["field"] == "protocol_version" for item in mixed_payload["frame"]["mixed"]
    )


def test_grid_cli_matches_golden_and_split_pivot(capsys):
    grid_argv = [
        "grid",
        "--runs-dir",
        str(FIXTURE_RUNS),
        "--include-fake",
        "--md",
    ]
    assert main(grid_argv) == 0
    grid_stdout = capsys.readouterr().out
    assert grid_stdout == (FIXTURE_ROOT / "grid.golden.md").read_text(encoding="utf-8")

    pivot_argv = [
        "pivot",
        "--rows",
        "sender",
        "--cols",
        "guard",
        "--split",
        "arena",
        "--runs-dir",
        str(FIXTURE_RUNS),
        "--include-fake",
        "--md",
    ]
    assert main(pivot_argv) == 0
    assert capsys.readouterr().out == grid_stdout


def test_grid_arena_and_where_filters_match_split_pivot(capsys):
    filters = (
        ["--arena", "db07141c"],
        ["--where", "protected_count=10"],
        ["--arena", "50562993", "--where", "protected_count=10"],
    )
    for selection in filters:
        grid_argv = [
            "grid",
            "--runs-dir",
            str(FIXTURE_RUNS),
            "--include-fake",
            "--md",
            *selection,
        ]
        assert main(grid_argv) == 0
        grid_stdout = capsys.readouterr().out

        pivot_argv = [
            "pivot",
            "--rows",
            "sender",
            "--cols",
            "guard",
            "--split",
            "arena",
            "--runs-dir",
            str(FIXTURE_RUNS),
            "--include-fake",
            "--md",
            *selection,
        ]
        assert main(pivot_argv) == 0
        assert capsys.readouterr().out == grid_stdout


def test_reference_arena_grid_and_split_pivot_share_the_cli_view(
    tmp_path: Path, monkeypatch, capsys
):
    fixture_copy = tmp_path / "tests" / "fixtures"
    shutil.copytree(FIXTURE_ROOT, fixture_copy)
    templates = Path(__file__).resolve().parents[1] / "templates" / "au-meeting"
    shutil.copytree(templates, tmp_path / "templates" / "au-meeting")
    monkeypatch.chdir(tmp_path)

    reference_run = fixture_copy / "runs" / "watchlist" / "all-kinds"
    raw_config = json.loads((reference_run / "config.json").read_text(encoding="utf-8"))
    config = Config.model_validate(
        {key: value for key, value in raw_config.items() if key in Config.model_fields}
    )
    experiments = tmp_path / "experiments"
    experiments.mkdir()
    (experiments / "reference.json").write_text(
        json.dumps(config.model_dump(mode="json")),
        encoding="utf-8",
    )

    grid_argv = [
        "grid",
        "--runs-dir",
        str(fixture_copy / "runs"),
        "--include-fake",
        "--md",
    ]
    assert main(grid_argv) == 0
    grid_stdout = capsys.readouterr().out
    assert "Other arenas (runs):" in grid_stdout
    assert grid_stdout.count("| sender ↓ |") == 1

    pivot_argv = [
        "pivot",
        "--rows",
        "sender",
        "--cols",
        "guard",
        "--split",
        "arena",
        "--runs-dir",
        str(fixture_copy / "runs"),
        "--include-fake",
        "--md",
    ]
    assert main(pivot_argv) == 0
    assert capsys.readouterr().out == grid_stdout


def test_all_innocent_grid_and_split_pivot_share_empty_selection(
    tmp_path: Path, capsys
):
    run_dir = tmp_path / "runs" / "all-innocent"
    source = FIXTURE_RUNS / "base" / "sender-a__guard-a"
    shutil.copytree(source, run_dir)

    episode_path = run_dir / "episodes.jsonl"
    episodes = [
        json.loads(line)
        for line in episode_path.read_text(encoding="utf-8").splitlines()
    ]
    innocent = [episode for episode in episodes if episode.get("kind") != "covert"]
    assert innocent
    episode_path.write_text(
        "".join(json.dumps(episode) + "\n" for episode in innocent),
        encoding="utf-8",
    )
    run_path = run_dir / "run.json"
    run = json.loads(run_path.read_text(encoding="utf-8"))
    run["episodes_done"] = len(innocent)
    run_path.write_text(json.dumps(run), encoding="utf-8")

    grid_argv = ["grid", "--runs-dir", str(tmp_path / "runs"), "--include-fake", "--md"]
    assert main(grid_argv) == 0
    grid_stdout = capsys.readouterr().out
    assert "No runs selected." in grid_stdout

    pivot_argv = [
        "pivot",
        "--rows",
        "sender",
        "--cols",
        "guard",
        "--split",
        "arena",
        "--runs-dir",
        str(tmp_path / "runs"),
        "--include-fake",
        "--md",
    ]
    assert main(pivot_argv) == 0
    assert capsys.readouterr().out == grid_stdout


def test_selected_metrics_keep_each_denominator_and_run_count(capsys):
    argv = [
        "pivot",
        "--rows",
        "sender",
        "--cols",
        "guard",
        "--split",
        "arena",
        "--runs-dir",
        str(FIXTURE_RUNS),
        "--include-fake",
        "--metric",
        "decode_gap,false_alarm_rate",
    ]
    for output_flag in ([], ["--md"]):
        assert main([*argv, *output_flag]) == 0
        stdout = capsys.readouterr().out
        assert "decode_gap=1.000 (n=8)" in stdout
        assert "false_alarm_rate=0%" in stdout
        assert "(n=6)" in stdout
        assert "(r=1)" in stdout
        if output_flag:
            assert "n metric denominator" in stdout
