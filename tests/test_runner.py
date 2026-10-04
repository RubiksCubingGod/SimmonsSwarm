"""Hermetic M1 runner acceptance and minimal verify checks."""

from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path

import pytest

from simmons.config import Config, resolve_models
from simmons.episode import run_episode
from simmons.inputs import load
from simmons.plan import build_plan
from simmons.payloads.option import OptionPayload
from simmons.providers.fake import FakeProvider
from simmons.records import canonical_json, read_jsonl, read_requests_xz
from simmons.runner import run_experiment
from simmons.scorers.exact_match import ExactMatchScorer
from simmons.types import EpisodeContext, PermanentError
from simmons.commands.verify import verify_run

ROOT = Path(__file__).resolve().parents[1]
SMOKE = "experiments/smoke.json"
PRICED = "experiments/smoke-priced.json"


def _run(config: str, **kwargs):
    return run_experiment(config, repo_root=ROOT, **kwargs)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(canonical_json(row) + "\n" for row in rows), encoding="utf-8")


def _comparable_episode(record: dict) -> dict:
    result = dict(record)
    result.pop("run_id", None)
    result.pop("started_at", None)
    result.pop("finished_at", None)
    return result


def test_smoke_matches_golden_and_fake_runs_are_deterministic(tmp_path: Path):
    code, first_result = _run(SMOKE)
    assert code == 0
    first = Path(first_result["run_dir"])
    first_run = json.loads((first / "run.json").read_text())
    first_episodes = read_jsonl(first / "episodes.jsonl")

    assert first_run["status"] == "complete"
    assert first_run["episodes_done"] == 20
    assert "runs-local" in first.parts
    assert not (first / "run.lock").exists()
    assert {
        "attempt-log.jsonl",
        "calls.jsonl",
        "config.json",
        "episodes.jsonl",
        "events.jsonl",
        "inputs",
        "plan.jsonl",
        "requests.jsonl.xz",
        "run.json",
        "spend.jsonl",
        "summary.json",
    } == {path.name for path in first.iterdir()}
    assert (first / "plan.jsonl").read_text() == (ROOT / "tests/fixtures/golden-smoke-plan.jsonl").read_text()
    assert Counter(record["kind"] for record in first_episodes) == {
        "covert": 14,
        "plain_allowed": 6,
    }
    assert {record["outcome"] for record in first_episodes} >= {"CATCH", "HIT", "MISS"}
    covert = [record for record in first_episodes if record["kind"] == "covert"]
    assert all(record["calls"]["sender_debrief"] is not None for record in covert)
    catches = [record for record in covert if record["outcome"] == "CATCH"]
    assert catches
    assert all(record["calls"]["receiver"] is None for record in catches)
    assert all(record["calls"]["shadow_receiver"] is not None for record in catches)
    assert all(record["calls"]["sender_debrief"] is not None for record in catches)
    assert all("sender_debrief" not in row for row in read_jsonl(first / "attempt-log.jsonl"))
    assert first_run["preflight"]["sender_reasoning"]["fake:covert"]["carried"] is True
    request_rows = read_requests_xz(first / "requests.jsonl.xz")
    assert any(
        row.get("prior", {}).get("assistant", {}).get("reasoning_details")
        for row in request_rows
    )
    assert verify_run(first) == []

    code, second_result = _run(SMOKE)
    assert code == 0
    second = Path(second_result["run_dir"])
    second_episodes = read_jsonl(second / "episodes.jsonl")
    assert [_comparable_episode(row) for row in first_episodes] == [
        _comparable_episode(row) for row in second_episodes
    ]

    # The minimal verifier rejects each invariant it claims to check.
    duplicate = tmp_path / "duplicate"
    shutil.copytree(first, duplicate)
    rows = read_jsonl(duplicate / "episodes.jsonl")
    _write_jsonl(duplicate / "episodes.jsonl", [*rows, rows[0]])
    assert any("has 2 records" in error for error in verify_run(duplicate))

    missing_request = tmp_path / "missing-request"
    shutil.copytree(first, missing_request)
    call_rows = read_jsonl(missing_request / "calls.jsonl")
    call_rows[0]["request_sha256"] = "missing-request-hash"
    _write_jsonl(missing_request / "calls.jsonl", call_rows)
    assert any("request hash is absent" in error for error in verify_run(missing_request))

    unbalanced = tmp_path / "unbalanced-spend"
    shutil.copytree(first, unbalanced)
    spend_rows = read_jsonl(unbalanced / "spend.jsonl")
    _write_jsonl(unbalanced / "spend.jsonl", spend_rows[:-1])
    assert any("no settlement or unresolved" in error for error in verify_run(unbalanced))

    blocked_receiver = tmp_path / "blocked-receiver"
    shutil.copytree(first, blocked_receiver)
    episode_rows = read_jsonl(blocked_receiver / "episodes.jsonl")
    blocked = next(row for row in episode_rows if row["outcome"] == "CATCH")
    successful = next(row for row in episode_rows if row["calls"]["receiver"] is not None)
    blocked["calls"]["receiver"] = successful["calls"]["receiver"]
    _write_jsonl(blocked_receiver / "episodes.jsonl", episode_rows)
    assert any("blocked episode has an ordinary receiver call" in error for error in verify_run(blocked_receiver))


def test_priced_smoke_trips_the_breaker_below_its_cap():
    code, result = _run(PRICED)
    assert code == 3
    assert result["status"] == "stopped_budget"
    assert result["episodes_done"] < result["episodes_planned"]
    assert 0 < result["spent_usd"] < 0.05
    assert verify_run(result["run_dir"]) == []


def test_cli_override_cap_and_dry_run_validation():
    code, refused = _run(PRICED, max_cost=0.06)
    assert code == 3
    assert refused["status"] == "refused"
    assert "exceeds configured cap" in refused["error"]

    code, dry_run = _run(SMOKE, dry_run=True)
    assert code == 0
    assert dry_run["run_dir"] is None
    with pytest.raises(Exception, match="RUN_CEILING_USD"):
        Config.model_validate({"name": "over-cap", "max_cost_usd": 25})


def test_failed_debrief_does_not_change_the_game_outcome():
    raw = json.loads((ROOT / SMOKE).read_text())
    config = Config.model_validate(raw)
    inputs = load(config, repo_root=ROOT)
    line = next(line for line in build_plan(config, inputs.option_set) if line["kind"] == "covert")
    models = resolve_models(config)

    class DebriefFailureProvider:
        def __init__(self):
            self.fake = FakeProvider()

        def build_body(self, request):
            return self.fake.build_body(request)

        def complete(self, request):
            if request.role == "sender_debrief":
                raise PermanentError("synthetic debrief failure")
            return self.fake.complete(request)

    def context(provider):
        return EpisodeContext(
            config=config,
            inputs=inputs,
            providers={"fake": provider},
            payload=OptionPayload(),
            scorer=ExactMatchScorer(),
            run_id="debrief-test",
            resolved_models=models,
        )

    baseline = run_episode(line, context(FakeProvider()))
    failed = run_episode(line, context(DebriefFailureProvider()))

    assert baseline["outcome"] in {"CATCH", "HIT", "MISS"}
    assert failed["outcome"] == baseline["outcome"]
    assert failed["guard"] == baseline["guard"]
    assert failed["pick"] == baseline["pick"]
    assert failed["score"] == baseline["score"]
    assert failed["calls"]["sender_debrief"] is None
    assert failed["debrief_error"] == "provider: synthetic debrief failure"
    assert failed["error"] is None


def test_fixture_run_corpus_is_valid_and_covers_both_arenas():
    base_runs = sorted((ROOT / "tests/fixtures/runs/base").iterdir())
    assert len(base_runs) == 6
    for run_dir in base_runs:
        episodes = read_jsonl(run_dir / "episodes.jsonl")
        assert len(episodes) == 20
        assert any(row["calls"]["sender_debrief"] is not None for row in episodes)
        assert verify_run(run_dir) == []

    watchlist = ROOT / "tests/fixtures/runs/watchlist/all-kinds"
    watchlist_episodes = read_jsonl(watchlist / "episodes.jsonl")
    assert {row["kind"] for row in watchlist_episodes} == {
        "covert",
        "control",
        "plain_allowed",
        "plain_protected",
    }
    assert len(watchlist_episodes) == 20
    assert any(row["calls"]["sender_debrief"] is not None for row in watchlist_episodes)
    assert verify_run(watchlist) == []
