from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

from simmons.commands.view import _run

FIXTURES = Path(__file__).parent / "fixtures" / "runs"
HOSTILE = "</script><script>alert(1)</script>"


def _page_data(path: Path) -> dict:
    match = re.search(
        r'<script id="viewer-data" type="application/json">(.*?)</script>',
        path.read_text(encoding="utf-8"),
        re.DOTALL,
    )
    assert match is not None
    return json.loads(match.group(1))


def _stage(tmp_path: Path) -> Path:
    for name in ("sender-a__guard-a", "sender-b__guard-a", "sender-c__guard-b"):
        shutil.copytree(FIXTURES / "base" / name, tmp_path / "runs" / "base" / name)
    shutil.copytree(FIXTURES / "watchlist" / "all-kinds", tmp_path / "runs-local" / "watchlist" / "all-kinds")
    return tmp_path


def _args(**values) -> argparse.Namespace:
    return argparse.Namespace(**{"run_dir": None, "all": True, "open": False, **values})


def test_view_all_indexes_every_run_and_generates_missing_views(tmp_path: Path, monkeypatch) -> None:
    root = _stage(tmp_path)
    monkeypatch.setenv("SIMMONS_RUNS_ROOT", str(root))

    assert _run(_args()) == 0

    index = root / "runs-local" / "index.html"
    data = _page_data(index)
    runs = data["runs"]
    assert len(runs) == 4
    started = [run["started_at"] or "" for run in runs]
    assert started == sorted(started, reverse=True)
    for run in runs:
        assert run["href"] is not None
        assert (index.parent / run["href"]).resolve().is_file()
        assert run["fake"] is True
        assert set(run["models"]) == {"sender", "receiver", "guard", "plain_sender"}
        assert run["rates"]["hit"] is not None
    assert {run["root"] for run in runs} == {"runs", "runs-local"}


def test_runs_that_share_a_prompt_share_its_hash(tmp_path: Path, monkeypatch) -> None:
    root = _stage(tmp_path)
    monkeypatch.setenv("SIMMONS_RUNS_ROOT", str(root))
    assert _run(_args()) == 0

    data = _page_data(root / "runs-local" / "index.html")
    by_dir = {run["run_id"]: run for run in data["runs"]}
    guard_a = (FIXTURES / "base" / "sender-a__guard-a" / "inputs" / "prompts" / "guard.md").read_text(encoding="utf-8")
    p2_hashes = [run["prompts"]["p2"]["hash"] for run in by_dir.values()]
    shared = [digest for digest in set(p2_hashes) if data["prompts"][digest]["text"] == guard_a]

    assert len(shared) == 1
    assert p2_hashes.count(shared[0]) == 2
    assert data["prompts"][shared[0]]["runs"] == 2
    assert len({run["prompts"]["p1"]["hash"] for run in by_dir.values()}) == 3
    for run in by_dir.values():
        for slot in ("p1", "p2", "plain"):
            assert run["prompts"][slot]["hash"] in data["prompts"]


def test_view_needs_a_run_dir_or_all(capsys) -> None:
    assert _run(_args(all=False)) == 2
    assert _run(_args(run_dir=Path("x"))) == 2
    assert "pass a run directory or --all" in capsys.readouterr().err


def test_prompt_text_is_embedded_as_text_not_markup(tmp_path: Path, monkeypatch) -> None:
    root = _stage(tmp_path)
    prompt = root / "runs" / "base" / "sender-a__guard-a" / "inputs" / "prompts" / "guard.md"
    prompt.write_text(HOSTILE, encoding="utf-8")
    monkeypatch.setenv("SIMMONS_RUNS_ROOT", str(root))
    assert _run(_args()) == 0

    page = (root / "runs-local" / "index.html").read_text(encoding="utf-8")
    assert HOSTILE not in page
    assert HOSTILE.replace("<", "\\u003c").replace(">", "\\u003e") in page
    assert "http://" not in page and "https://" not in page


def test_index_rows_carry_descriptions_and_pacific_times(tmp_path: Path, monkeypatch) -> None:
    root = _stage(tmp_path)
    monkeypatch.setenv("SIMMONS_RUNS_ROOT", str(root))
    assert _run(_args()) == 0

    data = _page_data(root / "runs-local" / "index.html")
    descriptions = {
        json.loads(path.read_text(encoding="utf-8"))["run_id"]: json.loads(
            (path.parent / "config.json").read_text(encoding="utf-8")
        ).get("description")
        for path in root.rglob("run.json")
    }
    for run in data["runs"]:
        assert run["description"] == descriptions[run["run_id"]]
        assert run["started"].endswith(("PDT", "PST")) and " at " in run["started"]
    assert data["generated"].endswith(("PDT", "PST"))


def test_run_and_episode_pages_link_back_to_all_runs(tmp_path: Path, monkeypatch) -> None:
    from simmons.viewer import all_runs_index, generate

    root = _stage(tmp_path)
    monkeypatch.setenv("SIMMONS_RUNS_ROOT", str(root))
    assert _run(_args()) == 0
    run_dir = root / "runs" / "base" / "sender-a__guard-a"
    index_page = run_dir / "view" / "index.html"

    for page in (index_page, run_dir / "view" / "episode-000000.html"):
        href = _page_data(page)["index_href"]
        assert (page.parent / href).resolve() == (root / "runs-local" / "index.html").resolve()
    assert all_runs_index(run_dir) == (root / "runs-local" / "index.html").resolve()

    published = tmp_path / "published"
    generate(run_dir, output_dir=published)
    assert _page_data(published / "index.html")["index_href"] is None
    assert all_runs_index(tmp_path / "loose-run") is None
