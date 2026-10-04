"""Hermetic M1 runner acceptance and minimal verify checks."""

from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path

import pytest

import simmons.runner as runner
from simmons.config import Config, resolve_models
from simmons.episode import _call_role, run_episode
from simmons.inputs import load
from simmons.plan import build_plan
from simmons.payloads.option import OptionPayload
from simmons.providers.fake import FakeProvider
from simmons.records import canonical_json, read_jsonl, read_requests_xz
from simmons import runner as runner_module
from simmons.runner import run as run_config
from simmons.runner import _make_state, run_experiment
from simmons.scorers.exact_match import ExactMatchScorer
from simmons.spend import Breaker
from simmons.types import (
    BudgetError,
    BreakerTripped,
    EpisodeContext,
    ModelInfo,
    PermanentError,
    ProviderStatus,
    TransientError,
)
from simmons.commands.verify import verify_run

ROOT = Path(__file__).resolve().parents[1]
SMOKE = "experiments/smoke.json"
PRICED = "experiments/smoke-priced.json"


def _run(config: str, **kwargs):
    return run_experiment(config, repo_root=ROOT, **kwargs)


def test_run_experiment_uses_local_trajectory_only_for_fake_or_scratch(
    monkeypatch: pytest.MonkeyPatch,
):
    raw = json.loads((ROOT / SMOKE).read_text(encoding="utf-8"))
    raw["models"] = "openrouter:openai/gpt-4o-mini"
    config = Config.model_validate(raw)

    class StubProvider:
        def __init__(self, provider: str):
            self.provider = provider

        def check(self) -> ProviderStatus:
            return ProviderStatus(self.provider, True, "test provider")

    def provider_instances(models):
        providers = {
            spec.provider: StubProvider(spec.provider)
            for spec in models.values()
            if spec is not None
        }
        return providers, {}

    observed_local: list[bool] = []

    def find_trajectory(root, *, local, config_hash, seed, log_scope):
        observed_local.append(local)
        return 0, [], []

    monkeypatch.setattr(runner, "_provider_instances", provider_instances)
    monkeypatch.setattr(runner, "_find_trajectory", find_trajectory)
    monkeypatch.setattr(runner, "load_catalog", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "estimate", lambda *args, **kwargs: 0.0)

    for overrides, expected in (({}, False), ({"scratch": True}, True), ({"fake": True}, True)):
        code, result = run_experiment(config, repo_root=ROOT, dry_run=True, **overrides)
        assert code == 0
        assert result["status"] == "dry_run"
        assert observed_local[-1] is expected

    assert observed_local == [False, True, True]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(canonical_json(row) + "\n" for row in rows), encoding="utf-8")


def _comparable_episode(record: dict) -> dict:
    result = dict(record)
    result.pop("run_id", None)
    result.pop("started_at", None)
    result.pop("finished_at", None)
    return result


def test_smoke_matches_golden_and_fake_runs_continue_trajectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
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
    shadow_records = [
        record for record in first_episodes
        if record["calls"]["shadow_receiver"] is not None
    ]
    assert shadow_records
    assert all(record["outcome"] == "CATCH" for record in shadow_records)
    assert all(record["guard"]["blocked"] for record in shadow_records)
    assert any(record["shadow_pick"] == record["target"] for record in shadow_records)
    assert all(record["calls"]["sender_debrief"] is not None for record in catches)
    assert all("sender_debrief" not in row for row in read_jsonl(first / "attempt-log.jsonl"))
    assert first_run["preflight"]["sender_reasoning"]["fake:covert"]["carried"] is True
    request_rows = read_requests_xz(first / "requests.jsonl.xz")
    reserve_hashes = {
        row["request_sha256"]
        for row in read_jsonl(first / "spend.jsonl")
        if row["event"] == "reserve"
    }
    request_hashes = {row["request_sha256"] for row in request_rows}
    call_hashes = {row["request_sha256"] for row in read_jsonl(first / "calls.jsonl")}
    assert reserve_hashes
    assert reserve_hashes <= request_hashes
    assert reserve_hashes <= call_hashes
    assert any(
        row.get("prior", {}).get("assistant", {}).get("reasoning_details")
        for row in request_rows
    )
    assert verify_run(first) == []

    code, second_result = _run(SMOKE)
    assert code == 0
    second = Path(second_result["run_dir"])
    second_run = json.loads((second / "run.json").read_text())
    second_episodes = read_jsonl(second / "episodes.jsonl")
    assert verify_run(second) == []

    complete_continuation_cases = (
        (
            "continued-missing-index",
            [row for row in second_episodes if row["index"] != 20],
            ("missing expected episode index 20",),
        ),
        (
            "continued-extra-index",
            [*second_episodes, {**second_episodes[0], "index": 40}],
            ("beyond the complete run's completed prefix 20..39", "outside 20..39"),
        ),
        (
            "continued-duplicate-index",
            [*second_episodes, second_episodes[0]],
            ("index 20 has 2 records",),
        ),
    )
    for name, episode_rows, expected_errors in complete_continuation_cases:
        case_dir = tmp_path / name
        shutil.copytree(second, case_dir)
        _write_jsonl(case_dir / "episodes.jsonl", episode_rows)
        errors = verify_run(case_dir)
        for expected_error in expected_errors:
            assert any(expected_error in error for error in errors)

    first_attempts = read_jsonl(first / "attempt-log.jsonl")
    assert second_run["trajectory"] == {
        "start": 20,
        "inherited_from": [{"run_id": first_run["run_id"], "entries": len(first_attempts)}],
    }
    assert [row["index"] for row in read_jsonl(second / "plan.jsonl")] == list(range(20, 40))
    assert [row["index"] for row in second_episodes] == list(range(20, 40))
    assert second_episodes[0]["log_seen"]["sender"] == [row["episode"] for row in first_attempts]
    second_requests = read_requests_xz(second / "requests.jsonl.xz")
    sender_preflight_hash = next(
        row["request_sha256"]
        for row in second_run["preflight"]["roles"]
        if row["role"] == "sender"
    )
    sender_preflight = next(
        row for row in second_requests if row["request_sha256"] == sender_preflight_hash
    )
    preflight_user = "\n".join(part["text"] for part in sender_preflight["user"])
    assert first_attempts[0]["message"] in preflight_user

    monkeypatch.setenv("SIMMONS_RUNS_ROOT", str(tmp_path / "deterministic-runs"))
    code, replay_result = _run(SMOKE)
    assert code == 0
    replay_episodes = read_jsonl(Path(replay_result["run_dir"]) / "episodes.jsonl")
    assert [_comparable_episode(row) for row in first_episodes] == [
        _comparable_episode(row) for row in replay_episodes
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

    missing_episode = tmp_path / "missing-episode"
    shutil.copytree(first, missing_episode)
    episode_rows = read_jsonl(missing_episode / "episodes.jsonl")
    missing_index = 7
    _write_jsonl(
        missing_episode / "episodes.jsonl",
        [row for row in episode_rows if row["index"] != missing_index],
    )
    assert any(f"missing expected episode index {missing_index}" in error for error in verify_run(missing_episode))

    missing_nested_hash = tmp_path / "missing-nested-hash"
    shutil.copytree(first, missing_nested_hash)
    episode_rows = read_jsonl(missing_nested_hash / "episodes.jsonl")
    first_call = next(call for call in episode_rows[0]["calls"].values() if call is not None)
    first_call.pop("request_sha256")
    _write_jsonl(missing_nested_hash / "episodes.jsonl", episode_rows)
    assert any("request hash is absent or empty" in error for error in verify_run(missing_nested_hash))

    missing_log_hash = tmp_path / "missing-log-hash"
    shutil.copytree(first, missing_log_hash)
    call_rows = read_jsonl(missing_log_hash / "calls.jsonl")
    call_rows[0].pop("request_sha256")
    _write_jsonl(missing_log_hash / "calls.jsonl", call_rows)
    assert any("request_sha256" in error for error in verify_run(missing_log_hash))

    missing_reserve_request = tmp_path / "missing-reserve-request"
    shutil.copytree(first, missing_reserve_request)
    spend_rows = read_jsonl(missing_reserve_request / "spend.jsonl")
    reserve_row = next(row for row in spend_rows if row["event"] == "reserve")
    reserve_row["request_sha256"] = "missing-reserve-request-hash"
    _write_jsonl(missing_reserve_request / "spend.jsonl", spend_rows)
    assert any(
        "reserve request hash is absent from requests.jsonl.xz" in error
        for error in verify_run(missing_reserve_request)
    )


def test_priced_smoke_trips_the_breaker_below_its_cap():
    code, result = _run(PRICED)
    assert code == 3
    assert result["status"] == "stopped_budget"
    assert result["episodes_done"] < result["episodes_planned"]
    assert 0 < result["spent_usd"] < 0.05
    assert verify_run(result["run_dir"]) == []


def test_provider_budget_error_stops_without_recording_unfinished_episode(monkeypatch):
    original_complete = FakeProvider.complete
    guard_calls = 0

    def refuse_guard_after_first_episode(self, request):
        nonlocal guard_calls
        if request.role == "guard":
            guard_calls += 1
            if guard_calls == 3:
                raise BudgetError("synthetic HTTP 402 budget refusal")
        return original_complete(self, request)

    monkeypatch.setattr(FakeProvider, "complete", refuse_guard_after_first_episode)
    code, result = _run(SMOKE, episodes=5)

    assert code == 3
    assert result["status"] == "stopped_budget"
    assert result["episodes_done"] == 1
    assert guard_calls == 3
    run_dir = Path(result["run_dir"])
    assert [row["index"] for row in read_jsonl(run_dir / "episodes.jsonl")] == [0]
    assert verify_run(run_dir) == []


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


def test_openrouter_key_limit_is_parsed_and_each_provider_is_checked_once():
    class _Checker:
        def __init__(self, detail: str):
            self.detail = detail
            self.checks = 0

        def check(self):
            self.checks += 1
            return ProviderStatus("openrouter", True, self.detail)

    assert runner_module._limit_remaining("label=team; limit_remaining=2.5; usage=1") == 2.5
    assert runner_module._limit_remaining("limit_remaining=None") is None
    with pytest.raises(ValueError, match="invalid limit_remaining"):
        runner_module._limit_remaining("limit_remaining=-1")

    first = _Checker("limit_remaining=3")
    second = _Checker("limit_remaining=2")
    remaining = runner_module._check_provider_limits(
        {"openrouter": first, "sender": first, "guard": second}
    )

    assert remaining == 2
    assert first.checks == second.checks == 1


def test_key_limit_refuses_before_run_directory_is_created(tmp_path, monkeypatch):
    raw = json.loads((ROOT / SMOKE).read_text())
    raw.update(
        {
            "name": "key-limit-refusal",
            "max_cost_usd": 0.5,
            "models": {
                "default": {
                    "model": "fake:covert",
                    "max_tokens": 100,
                    "params": {"price_out": 120},
                }
            },
        }
    )
    config = Config.model_validate(raw)
    monkeypatch.setenv("SIMMONS_RUNS_ROOT", str(tmp_path))
    monkeypatch.setattr(runner_module, "_check_provider_limits", lambda _providers: 0.000001)

    code, result = run_experiment(config, repo_root=ROOT, scratch=True, episodes=2)

    assert code == 3
    assert result["status"] == "refused"
    assert "remaining credit" in result["error"]
    assert not (tmp_path / "runs-local" / config.name).exists()


def test_real_estimate_refuses_before_run_directory_is_created(tmp_path, monkeypatch):
    class _Catalog:
        def lookup(self, slug: str):
            assert slug == "vendor/model"
            return ModelInfo(
                slug=slug,
                context_length=16_000,
                supports_tools=True,
                endpoint_prices=(("host", 100_000, 100_000),),
            )

    class _KeyCheck:
        def __init__(self):
            self.checks = 0

        def check(self):
            self.checks += 1
            return ProviderStatus("openrouter", True, "limit_remaining=10")

    raw = json.loads((ROOT / SMOKE).read_text())
    raw.update(
        {
            "name": "real-price-refusal",
            "max_cost_usd": 0.000001,
            "models": "openrouter:vendor/model",
        }
    )
    config = Config.model_validate(raw)
    provider = _KeyCheck()
    monkeypatch.setenv("SIMMONS_RUNS_ROOT", str(tmp_path))
    monkeypatch.setattr(
        runner_module,
        "_provider_instances",
        lambda _models: ({"openrouter": provider}, {"sender": provider}),
    )
    monkeypatch.setattr(runner_module, "load_catalog", lambda _path: _Catalog())

    code, result = run_experiment(config, repo_root=ROOT, scratch=True, episodes=1)

    assert code == 3
    assert result["status"] == "refused"
    assert "estimated spend" in result["error"]
    assert provider.checks == 1
    assert not (tmp_path / "runs-local" / config.name).exists()


def test_key_limit_refresh_between_waves_stops_before_the_next_episode(tmp_path, monkeypatch):
    raw = json.loads((ROOT / SMOKE).read_text())
    raw["name"] = "key-limit-waves"
    config = Config.model_validate(raw)
    limits = iter((1.0, 0.0))
    checks: list[int] = []
    monkeypatch.setenv("SIMMONS_RUNS_ROOT", str(tmp_path))

    def remaining(_providers):
        checks.append(1)
        return next(limits)

    monkeypatch.setattr(runner_module, "_check_provider_limits", remaining)

    code, result = run_experiment(config, repo_root=ROOT, scratch=True, episodes=2)

    assert code == 3
    assert result["status"] == "stopped_budget"
    assert result["episodes_done"] == 1
    assert len(checks) == 2


def test_transient_key_limit_refresh_retains_last_limit_and_completes(tmp_path, monkeypatch):
    raw = json.loads((ROOT / SMOKE).read_text())
    raw["name"] = "transient-key-limit-refresh"
    config = Config.model_validate(raw)
    refreshes: list[int] = []
    monkeypatch.setenv("SIMMONS_RUNS_ROOT", str(tmp_path))

    def remaining(_providers):
        refreshes.append(1)
        if len(refreshes) == 1:
            return 1.0
        raise TransientError("OpenRouter key refresh timed out")

    monkeypatch.setattr(runner_module, "_check_provider_limits", remaining)

    code, result = run_experiment(config, repo_root=ROOT, scratch=True, episodes=2)

    assert code == 0
    assert result["status"] == "complete"
    assert result["episodes_done"] == 2
    assert len(refreshes) == 2
    events = read_jsonl(Path(result["run_dir"]) / "events.jsonl")
    refresh_errors = [event for event in events if event["event"] == "provider_limit_refresh_failed"]
    assert len(refresh_errors) == 1
    assert refresh_errors[0]["level"] == "warning"
    assert refresh_errors[0]["error_type"] == "TransientError"
    assert refresh_errors[0]["fallback"] == "last_known_limit"


def test_frozen_runner_run_seam_accepts_config_and_returns_cli_shape():
    config = Config.model_validate(json.loads((ROOT / SMOKE).read_text()))
    result = run_config(config, fake=True, scratch=True, episodes=1, json_output=True)
    assert result["status"] == "complete"
    assert result["exit_code"] == 0
    assert result["summary"]["status"] == "complete"
    assert result["summary"] == json.loads((Path(result["run_dir"]) / "summary.json").read_text())


def test_submitted_transient_keeps_reservation_unresolved_and_counts_toward_cap(tmp_path):
    raw = json.loads((ROOT / SMOKE).read_text())
    raw["models"] = {
        "default": {
            "model": "fake:covert",
            "max_tokens": 100,
            "params": {"price_out": 120},
        }
    }
    config = Config.model_validate(raw)
    inputs = load(config, repo_root=ROOT)
    line = next(line for line in build_plan(config, inputs.option_set) if line["kind"] == "covert")
    models = resolve_models(config)
    fake = FakeProvider()

    class TimeoutProvider:
        def build_body(self, request):
            return fake.build_body(request)

        def complete(self, request):
            raise TransientError("response timed out")

    provider = TimeoutProvider()
    breaker = Breaker(0.02, tmp_path / "spend.jsonl")
    ctx = EpisodeContext(
        config=config,
        inputs=inputs,
        providers={"fake": provider, "sender": provider},
        payload=OptionPayload(),
        scorer=ExactMatchScorer(),
        run_id="timeout-spend-test",
        resolved_models=models,
        breaker=breaker,
        request_log_path=str(tmp_path / "requests.jsonl"),
        calls_log_path=str(tmp_path / "calls.jsonl"),
    )

    with pytest.raises(BreakerTripped):
        _call_role("sender", _make_state(line, config, inputs, ()), ctx)

    spend_rows = read_jsonl(tmp_path / "spend.jsonl")
    assert [row["event"] for row in spend_rows] == ["reserve", "unresolved"]
    assert spend_rows[1]["usd"] == spend_rows[0]["usd"]
    assert breaker.spent_usd() == pytest.approx(0.012)


def test_keyboard_interrupt_during_preflight_closes_submitted_reservation(tmp_path, monkeypatch):
    raw = json.loads((ROOT / SMOKE).read_text())
    raw["models"] = {
        "default": {
            "model": "fake:covert",
            "max_tokens": 100,
            "params": {"price_out": 120},
        }
    }
    raw["max_cost_usd"] = 0.05
    config = Config.model_validate(raw)

    def interrupt(_self, _request):
        raise KeyboardInterrupt

    monkeypatch.setattr(FakeProvider, "complete", interrupt)
    code, result = run_experiment(
        config,
        repo_root=ROOT,
        fake=True,
        scratch=True,
        episodes=1,
    )

    assert code == 5
    assert result["status"] == "interrupted"
    run_dir = Path(result["run_dir"])
    spend_rows = read_jsonl(run_dir / "spend.jsonl")
    assert [row["event"] for row in spend_rows] == ["reserve", "unresolved"]
    assert verify_run(run_dir) == []


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


@pytest.mark.parametrize(
    "relative_path",
    ("plan.jsonl", "attempt-log.jsonl", "summary.json", "events.jsonl"),
)
def test_verify_rejects_unknown_schema_in_each_record_file(
    tmp_path: Path, relative_path: str
):
    source = ROOT / "tests/fixtures/runs/base/sender-a__guard-a"
    copied = tmp_path / "corrupt-schema"
    shutil.copytree(source, copied)
    path = copied / relative_path

    if relative_path.endswith(".json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        record["schema"] = "simmons.unknown/1"
        path.write_text(json.dumps(record), encoding="utf-8")
    else:
        rows = read_jsonl(path)
        rows[0]["schema"] = "simmons.unknown/1"
        _write_jsonl(path, rows)

    assert any(error.startswith(f"{relative_path}:") for error in verify_run(copied))


def test_verify_accepts_committed_run_without_calls_or_events_logs(tmp_path: Path):
    source = ROOT / "tests/fixtures/runs/base/sender-a__guard-a"
    copied = tmp_path / "committed-run"
    shutil.copytree(source, copied)
    (copied / "calls.jsonl").unlink()
    (copied / "events.jsonl").unlink()

    assert verify_run(copied) == []
