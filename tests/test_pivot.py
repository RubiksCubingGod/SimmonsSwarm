from __future__ import annotations

import copy
from pathlib import Path

from simmons.dataset import scan_runs
from simmons.pivot import _pairing, _pool_cell, _uses_multiple_hosts, build_pivot, render_pivot


FIXTURES = Path(__file__).parent / "fixtures" / "runs"


def _runs():
    runs, skipped = scan_runs([FIXTURES], include_fake=True)
    assert skipped == []
    return runs


def test_unframed_pivot_refuses_mixed_arena_buckets() -> None:
    report = build_pivot(_runs(), rows="sender", cols="guard")
    assert report["refused"]
    assert any(item["field"] in {"cfg.protected_count", "cfg.control_rate"} for item in report["refused"])
    rendered = render_pivot(report)
    for item in report["refused"]:
        assert f"({len(item['buckets'])} buckets)" in rendered


def test_split_arena_resolves_mixed_frame_and_pools_rates() -> None:
    report = build_pivot(
        _runs(),
        rows="sender",
        cols="guard",
        split="arena",
        selected_metrics=("hit_rate", "catch_rate", "decode_gap", "auc"),
    )
    assert report["refused"] == []
    assert len(report["slices"]) == 2
    assert report["slices"][1]["paired"]["status"] == "paired"
    assert len(report["slices"][1]["frame"]["held"]) > 30
    cell = next(item for item in report["slices"][1]["cells"] if item["hit_rate"]["den"])
    assert cell["hit_rate"]["num"] == 8
    assert cell["hit_rate"]["den"] == 14
    assert cell["n_covert"] == 14
    assert cell["catch_rate"]["den"] == 14
    assert "value" in cell["decode_gap"]
    assert "value" in cell["auc"]


def test_cell_pools_runs_with_the_same_config() -> None:
    original = _runs()[0]
    duplicate = copy.deepcopy(original)
    duplicate.run["run_id"] = "fixture-repeat"
    duplicate.row["run_id"] = "fixture-repeat"
    for episode in duplicate.episodes:
        episode["run_id"] = "fixture-repeat"

    report = build_pivot([original, duplicate], rows="sender", cols="guard", selected_metrics=("hit_rate",))
    cell = report["slices"][0]["cells"][0]
    assert cell["n_runs"] == 2
    assert cell["n_covert"] == 28
    assert cell["hit_rate"]["num"] == 16
    assert cell["hit_rate"]["den"] == 28


def test_allow_mixed_pools_but_marks_mixed_fields() -> None:
    report = build_pivot(_runs(), rows="sender", cols="guard", allow_mixed=True)
    assert report["refused"] == []
    assert report["frame"]["mixed"]
    rendered = render_pivot(report)
    assert "also varies:" in rendered
    assert "cfg." in rendered
    assert "*" in rendered


def test_splitting_a_confounded_field_resolves_the_frame() -> None:
    report = build_pivot(_runs(), rows="sender", cols="guard", split="control_rate")
    assert report["refused"] == []
    assert len(report["slices"]) == 2


def test_pairing_reports_a_seed_missing_every_plan_hash() -> None:
    runs = []
    for seed, plan in ((1, "shared-plan"), (2, None)):
        for config_id in ("cfg-a", "cfg-b"):
            run = copy.deepcopy(_runs()[0])
            run.run["run_id"] = f"{config_id}-{seed}"
            run.config["seed"] = seed
            run.row["id.config"] = config_id
            run.row["id.plan"] = plan
            runs.append(run)

    paired = _pairing(runs)

    assert paired["status"] == "partial"
    assert paired["seeds"] == [1]
    assert paired["unpaired_seeds"] == [2]
    assert paired["columns"] == ["cfg-a", "cfg-b"]


def test_pairing_does_not_let_a_duplicate_run_hide_a_missing_hash() -> None:
    runs = []
    for config_id in ("cfg-a", "cfg-b"):
        run = copy.deepcopy(_runs()[0])
        run.run["run_id"] = config_id
        run.config["seed"] = 7
        run.row["id.config"] = config_id
        run.row["id.plan"] = "shared-plan"
        runs.append(run)
    duplicate = copy.deepcopy(runs[0])
    duplicate.run["run_id"] = "cfg-a-without-plan"
    duplicate.row["id.plan"] = None
    runs.append(duplicate)

    paired = _pairing(runs)

    assert paired["status"] == "no"
    assert paired["unpaired_seeds"] == [7]
    assert paired["columns"] == ["cfg-a"]


def test_decode_gap_is_stable_when_guard_guess_policy_varies() -> None:
    original = copy.deepcopy(_runs()[0])
    alternate = copy.deepcopy(original)
    alternate.config["guard_guesses_target"] = not original.config["guard_guesses_target"]
    alternate.row["id.config"] = "alternate-config"
    alternate.run["run_id"] = "fixture-z-alternate"

    forward = _pool_cell([original, alternate], ("receiver_decode", "guard_decode", "decode_gap"))
    reverse = _pool_cell([alternate, original], ("receiver_decode", "guard_decode", "decode_gap"))

    assert forward["decode_gap"]["value"] is None
    assert forward["guard_decode"]["value"] is None
    assert forward == reverse


def test_decode_gap_all_is_stable_when_shadow_decode_varies() -> None:
    original = copy.deepcopy(_runs()[0])
    alternate = copy.deepcopy(original)
    alternate.config["shadow_decode"] = not original.config["shadow_decode"]
    alternate.row["id.config"] = "alternate-config"
    alternate.run["run_id"] = "fixture-z-alternate"

    forward = _pool_cell([original, alternate], ("decode_gap_all",))
    reverse = _pool_cell([alternate, original], ("decode_gap_all",))

    assert forward["decode_gap_all"]["value"] is None
    assert forward == reverse


def test_multiple_host_marker_tracks_one_unpinned_model_with_multiple_hosts() -> None:
    first = copy.deepcopy(_runs()[0])
    second = copy.deepcopy(first)
    second.run["run_id"] = "fixture-z-second"

    for run, sender_host in ((first, "host-s"), (second, "host-s-2")):
        for episode in run.episodes:
            for call_name, call in episode["calls"].items():
                if not isinstance(call, dict):
                    continue
                role = {"sender_debrief": "sender", "shadow_receiver": "receiver"}.get(call_name, call_name)
                call["served_by"] = {
                    "sender": sender_host,
                    "receiver": "host-r",
                    "guard": "host-g",
                }.get(role, "host-j")

    assert len({
        call["served_by"]
        for call in first.episodes[0]["calls"].values()
        if isinstance(call, dict)
    }) > 1
    assert not _uses_multiple_hosts([first])
    assert _uses_multiple_hosts([first, second])
