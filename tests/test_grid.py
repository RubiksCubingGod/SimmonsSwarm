from __future__ import annotations

from pathlib import Path

from simmons.dataset import scan_runs
from simmons.grid import build_grid, grid_payload, render_grid
from simmons.pivot import DEFAULT_METRICS, build_pivot, render_pivot


FIXTURES = Path(__file__).parent / "fixtures" / "runs"


def test_grid_is_a_split_sender_guard_pivot_and_has_two_arenas() -> None:
    runs, _ = scan_runs([FIXTURES], include_fake=True)
    grid = build_grid(runs)
    pivot = build_pivot(
        runs,
        rows="sender",
        cols="guard",
        split="arena",
        selected_metrics=DEFAULT_METRICS,
    )
    assert render_grid(grid, markdown=True) == render_pivot(pivot, markdown=True)

    payload = grid_payload(grid, runs)
    assert len(payload["arenas"]) == 2
    assert all(arena["cells"] for arena in payload["arenas"])
    assert all({"hit_rate", "catch_rate", "false_alarm_rate", "decode_gap", "auc"} <= set(cell)
               for arena in payload["arenas"] for cell in arena["cells"])
    assert any("@" in row["label"] for arena in payload["arenas"] for row in arena["rows"])


def test_grid_excludes_fake_runs_by_default() -> None:
    runs, skipped = scan_runs([FIXTURES])
    assert runs == []
    assert len(skipped) == 7
    assert render_grid(build_grid(runs)).count("%") == 0


def test_arena_prefix_and_where_filters() -> None:
    runs, _ = scan_runs([FIXTURES], include_fake=True, where=["option_set=tests/fixtures/option-sets/smoke-100.json"])
    grid = build_grid(runs, arena="db07141c")
    assert len(grid["slices"]) == 1
    assert grid["slices"][0]["split"]["value"] == "db07141c"
