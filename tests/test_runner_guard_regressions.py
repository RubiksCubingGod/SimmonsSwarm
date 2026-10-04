"""Deferred T02f guard tests for runner and role-call boundaries."""

from __future__ import annotations

import json
import os
import signal
from collections import Counter
from dataclasses import replace
from pathlib import Path

from simmons.config import Config, resolve_models
from simmons.episode import run_episode
from simmons.inputs import load
from simmons.plan import build_plan
from simmons.payloads.option import OptionPayload
from simmons.providers.fake import FakeProvider
from simmons.records import canonical_json, read_jsonl, read_requests_xz, request_sha256
from simmons.runner import run_experiment
import simmons.runner as runner_module
from simmons.scorers.exact_match import ExactMatchScorer
from simmons.spend import Breaker
from simmons.types import EpisodeContext, ModelInfo, ProviderStatus
from simmons.commands.verify import verify_run

ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "experiments/smoke.json"


def _smoke_config(**changes) -> Config:
    raw = json.loads(SMOKE.read_text(encoding="utf-8"))
    raw.update(changes)
    return Config.model_validate(raw)


def test_scratch_run_with_non_fake_provider_uses_runs_local(tmp_path, monkeypatch):
    """The scratch flag alone selects the local run tree for a real route."""
    config = _smoke_config(models="openrouter:covert")
    fake = FakeProvider()

    class OfflineOpenRouterAdapter:
        def build_body(self, request):
            return fake.build_body(request)

        def complete(self, request):
            return fake.complete(request)

        def check(self):
            return ProviderStatus(
                provider="openrouter",
                key_present=True,
                detail="synthetic offline adapter for path selection test",
            )

    adapter = OfflineOpenRouterAdapter()

    def providers_for(resolved):
        by_role = {role: adapter for role, spec in resolved.items() if spec is not None}
        return ({"openrouter": adapter, **by_role}, by_role)

    class ZeroPriceCatalog:
        def lookup(self, slug):
            return ModelInfo(
                slug=slug,
                context_length=100_000,
                supports_tools=True,
                endpoint_prices=(("synthetic", 0.0, 0.0),),
            )

        def age_seconds(self):
            return 0.0

    monkeypatch.setenv("SIMMONS_RUNS_ROOT", str(tmp_path))
    monkeypatch.setattr(runner_module, "_provider_instances", providers_for)
    monkeypatch.setattr(runner_module, "load_catalog", lambda _path: ZeroPriceCatalog())

    original_reserve = Breaker.reserve

    def reserve_at_zero_price(self, attempt_id, body, spec, *, request_hash=None):
        # Keep the fake-backed adapter's journal accounting offline while the
        # runner still sees a non-fake provider in its local-path decision.
        return original_reserve(
            self,
            attempt_id,
            body,
            replace(spec, provider="fake"),
            request_hash=request_hash,
        )

    monkeypatch.setattr(Breaker, "reserve", reserve_at_zero_price)

    code, result = run_experiment(config, repo_root=ROOT, scratch=True, episodes=1)

    assert code == 0
    run_dir = Path(result["run_dir"])
    assert run_dir.is_relative_to(tmp_path / "runs-local")
    assert json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["scratch"] is True


def test_interrupt_during_episode_finishes_record_and_archive(tmp_path, monkeypatch):
    """SIGINT leaves the active episode verifiable and returns the interrupt code."""
    monkeypatch.setenv("SIMMONS_RUNS_ROOT", str(tmp_path))
    original_complete = FakeProvider.complete
    submissions = []
    signalled = []

    def interrupt_during_first_episode(self, request):
        response = original_complete(self, request)
        submissions.append(request)
        # Five episodes have four distinct preflight probes and two debrief
        # reasoning probes; call seven starts episode zero.
        if len(submissions) == 7:
            signalled.append(request)
            os.kill(os.getpid(), signal.SIGINT)
        return response

    monkeypatch.setattr(FakeProvider, "complete", interrupt_during_first_episode)

    code, result = run_experiment(
        SMOKE,
        repo_root=ROOT,
        scratch=True,
        episodes=5,
    )

    assert len(signalled) == 1
    assert signalled[0].role in {"sender", "plain_sender"}
    assert signalled[0].request_id == f"{signalled[0].role}:0"
    assert code == 5
    assert result["status"] == "interrupted"
    run_dir = Path(result["run_dir"])
    assert (run_dir / "requests.jsonl.xz").is_file()
    episodes = read_jsonl(run_dir / "episodes.jsonl")
    assert len(episodes) == 1
    assert episodes[0]["index"] == 0
    assert read_requests_xz(run_dir / "requests.jsonl.xz")
    assert verify_run(run_dir) == []


class _BlockingFakeProvider:
    def __init__(self):
        self.fake = FakeProvider()
        self.submissions = []

    def build_body(self, request):
        return self.fake.build_body(request)

    def complete(self, request):
        self.submissions.append(request.role)
        response = self.fake.complete(request)
        if request.role == "guard":
            return replace(
                response,
                tool_input={**response.tool_input, "suspicion": 1.0},
            )
        return response


def _blocked_episode(config: Config, line: dict):
    inputs = load(config, repo_root=ROOT)
    provider = _BlockingFakeProvider()
    models = resolve_models(config)
    context = EpisodeContext(
        config=config,
        inputs=inputs,
        providers={"fake": provider},
        payload=OptionPayload(),
        scorer=ExactMatchScorer(),
        run_id=f"receiver-submission-{line['kind']}",
        resolved_models=models,
    )
    return run_episode(line, context), provider


def test_catches_submit_no_ordinary_receiver_with_shadow_on_or_off():
    config = _smoke_config()
    plan = build_plan(config, load(config, repo_root=ROOT).option_set)
    covert = next(line for line in plan if line["kind"] == "covert")

    shadowed, shadow_provider = _blocked_episode(config, covert)
    assert shadowed["outcome"] == "CATCH"
    assert shadowed["guard"]["blocked"] is True
    assert shadowed["calls"]["receiver"] is None
    assert shadowed["calls"]["shadow_receiver"] is not None
    assert Counter(shadow_provider.submissions)["receiver"] == 1

    no_shadow_config = _smoke_config(shadow_decode=False)
    no_shadow_line = next(
        line
        for line in build_plan(no_shadow_config, load(no_shadow_config, repo_root=ROOT).option_set)
        if line["kind"] == "covert"
    )
    no_shadow, no_shadow_provider = _blocked_episode(no_shadow_config, no_shadow_line)
    assert no_shadow["outcome"] == "CATCH"
    assert no_shadow["calls"]["receiver"] is None
    assert no_shadow["calls"]["shadow_receiver"] is None
    assert "receiver" not in no_shadow_provider.submissions

    watched_config = _smoke_config(protected_count=10)
    plain_protected = next(
        line
        for line in build_plan(watched_config, load(watched_config, repo_root=ROOT).option_set)
        if line["kind"] == "plain_protected"
    )
    blocked_plain, plain_provider = _blocked_episode(watched_config, plain_protected)
    assert blocked_plain["outcome"] == "CATCH"
    assert blocked_plain["guard"]["blocked"] is True
    assert blocked_plain["calls"]["receiver"] is None
    assert blocked_plain["calls"]["shadow_receiver"] is None
    assert "receiver" not in plain_provider.submissions


def test_every_provider_submission_has_a_prior_reservation_including_preflight(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SIMMONS_RUNS_ROOT", str(tmp_path))
    original_init = Breaker.__init__
    original_complete = FakeProvider.complete
    journal_paths = []
    seen_by_digest: Counter[str] = Counter()
    submissions = []

    def track_journal(self, cap_usd, journal_path=None):
        original_init(self, cap_usd, journal_path)
        journal_paths.append(Path(journal_path))

    def inspect_reservation_before_complete(self, request):
        assert journal_paths
        digest = request_sha256(request)
        seen_by_digest[digest] += 1
        reservations = [
            row
            for row in read_jsonl(journal_paths[0])
            if row["event"] == "reserve" and row["request_sha256"] == digest
        ]
        submissions.append(
            {
                "role": request.role,
                "digest": digest,
                "ordinal": seen_by_digest[digest],
                "reservation_count": len(reservations),
                "attempt_ids": [row["attempt_id"] for row in reservations],
            }
        )
        return original_complete(self, request)

    monkeypatch.setattr(Breaker, "__init__", track_journal)
    monkeypatch.setattr(FakeProvider, "complete", inspect_reservation_before_complete)

    code, result = run_experiment(SMOKE, repo_root=ROOT, scratch=True, episodes=2)

    assert code == 0
    assert submissions
    assert all(item["reservation_count"] >= item["ordinal"] for item in submissions)
    run_dir = Path(result["run_dir"])
    archived = read_requests_xz(run_dir / "requests.jsonl.xz")
    assert len(submissions) == len(archived)

    run_record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    for probe in run_record["preflight"]["roles"]:
        assert any(
            item["digest"] == probe["request_sha256"]
            and any(
                f":preflight-{probe['role']}:" in attempt_id
                for attempt_id in item["attempt_ids"]
            )
            for item in submissions
        )
    preflight_debrief_reservations = [
        row
        for row in read_jsonl(run_dir / "spend.jsonl")
        if row["event"] == "reserve" and ":preflight-reasoning-" in row["attempt_id"]
    ]
    assert len(preflight_debrief_reservations) == 2
    assert all(
        any(item["digest"] == row["request_sha256"] for item in submissions)
        for row in preflight_debrief_reservations
    )


def test_debrief_canary_is_absent_from_guard_receiver_requests_and_attempt_log(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SIMMONS_RUNS_ROOT", str(tmp_path))
    original_complete = FakeProvider.complete
    canary = "T02F_B_DEBRIEF_CANARY_8bd39c"

    def add_canary_to_debrief(self, request):
        response = original_complete(self, request)
        if request.role == "sender_debrief":
            return replace(
                response,
                tool_input={**response.tool_input, "encoding": canary},
            )
        return response

    monkeypatch.setattr(FakeProvider, "complete", add_canary_to_debrief)

    code, result = run_experiment(SMOKE, repo_root=ROOT, scratch=True, episodes=1)

    assert code == 0
    run_dir = Path(result["run_dir"])
    episodes = read_jsonl(run_dir / "episodes.jsonl")
    assert any(
        canary in canonical_json(record["calls"]["sender_debrief"])
        for record in episodes
        if record["calls"]["sender_debrief"] is not None
    )
    outbound_guard_receiver = [
        row
        for row in read_requests_xz(run_dir / "requests.jsonl.xz")
        if row["role"] in {"guard", "receiver"}
    ]
    assert outbound_guard_receiver
    assert all(canary not in canonical_json(row) for row in outbound_guard_receiver)
    attempt_log = read_jsonl(run_dir / "attempt-log.jsonl")
    assert all(canary not in canonical_json(row) for row in attempt_log)
