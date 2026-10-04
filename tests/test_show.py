"""T16 command tests use only the committed offline run fixtures."""

from __future__ import annotations

import json
import lzma
import re
import shutil
from pathlib import Path

from simmons.cli import main
from simmons.records import read_requests_xz

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "runs"
BASE_RUN = FIXTURES / "base" / "sender-a__guard-a"
WATCHLIST_RUN = FIXTURES / "watchlist" / "all-kinds"


def _invoke(capsys, run_dir: Path, *args: str) -> tuple[int, str, str]:
    code = main(["show", str(run_dir), *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_show_run_overview(capsys):
    code, stdout, stderr = _invoke(capsys, BASE_RUN)

    assert code == 0
    assert stderr == ""
    assert "fixture-base-sender-a-guard-a" in stdout
    assert "Status: complete" in stdout
    assert "Episodes: 20/20" in stdout
    assert "CATCH" in stdout and "HIT" in stdout and "MISS" in stdout


def test_show_json_episode_record(capsys):
    code, stdout, stderr = _invoke(capsys, BASE_RUN, "--episode", "3", "--json")

    assert code == 0
    assert stderr == ""
    episode = json.loads(stdout)
    assert episode["index"] == 3
    assert episode["outcome"] == "CATCH"
    assert episode["target"]["value"] == "Shepparton"


def test_show_guard_request_matches_hash_and_has_no_target(capsys):
    code, stdout, stderr = _invoke(
        capsys, BASE_RUN, "--episode", "3", "--role", "guard", "--prompt"
    )

    assert code == 0
    assert stderr == ""
    assert stdout.rstrip().endswith("sha256 matches")
    assert "Story under review:" in stdout
    assert "<target>" not in stdout


def test_show_json_sender_debrief_includes_prior_and_verified_request(capsys):
    code, stdout, stderr = _invoke(
        capsys,
        BASE_RUN,
        "--episode",
        "3",
        "--role",
        "sender_debrief",
        "--prompt",
        "--json",
    )

    assert code == 0
    assert stderr == ""
    episode = json.loads(stdout)
    assert episode["index"] == 3
    assert episode["request"]["sha256_matches"] is True
    assert episode["request"]["prior"]["tool_result"][0]["text"].startswith(
        "The episode is over."
    )


def test_show_sample_prints_cover_task_and_requested_number_of_messages(capsys):
    code, stdout, stderr = _invoke(capsys, BASE_RUN, "--sample", "5")

    assert code == 0
    assert stderr == ""
    assert "Cover task:" in stdout
    assert "# Foreign affairs news" in stdout
    assert len(re.findall(r"^Episode \d+:$", stdout, re.MULTILINE)) == 5


def test_show_watchlist_episode_displays_protected_options(capsys):
    code, stdout, stderr = _invoke(capsys, WATCHLIST_RUN, "--episode", "0")

    assert code == 0
    assert stderr == ""
    assert "Episode 0" in stdout
    assert "Protected:" in stdout
    assert "none" not in stdout.split("Protected:", 1)[1].splitlines()[0]


def test_show_rejects_unknown_metadata_major_versions(capsys, tmp_path):
    for record_name in ("run", "summary"):
        run_dir = tmp_path / record_name
        shutil.copytree(BASE_RUN, run_dir)
        record_path = run_dir / f"{record_name}.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["schema"] = f"simmons.{record_name}/999"
        record_path.write_text(json.dumps(record), encoding="utf-8")

        code, _, stderr = _invoke(capsys, run_dir, "--json")

        assert code == 2
        assert f"unsupported record schema: simmons.{record_name}/999" in stderr


def test_show_request_hash_mismatch_is_invalid_input(capsys, tmp_path):
    run_dir = tmp_path / "run"
    shutil.copytree(BASE_RUN, run_dir)
    episodes = [
        json.loads(line)
        for line in (run_dir / "episodes.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    episode = next(row for row in episodes if row["index"] == 3)
    digest = episode["calls"]["guard"]["request_sha256"]
    request_path = run_dir / "requests.jsonl.xz"
    requests = read_requests_xz(request_path)
    for request in requests:
        if request["request_sha256"] == digest:
            request["system"] += "\nTampered after the run was recorded."
            break
    else:
        raise AssertionError(f"request {digest} was not found")
    with lzma.open(request_path, mode="wt", encoding="utf-8") as stream:
        for request in requests:
            stream.write(json.dumps(request, ensure_ascii=False) + "\n")

    code, stdout, stderr = _invoke(
        capsys, run_dir, "--episode", "3", "--role", "guard", "--prompt"
    )

    assert code == 2
    assert stderr == ""
    assert stdout.rstrip().endswith("sha256 mismatch")

    code, stdout, stderr = _invoke(
        capsys,
        run_dir,
        "--episode",
        "3",
        "--role",
        "guard",
        "--prompt",
        "--json",
    )

    assert code == 2
    assert stderr == ""
    assert json.loads(stdout)["request"]["sha256_matches"] is False
