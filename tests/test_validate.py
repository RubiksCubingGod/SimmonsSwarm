"""Validation command coverage against isolated fixture projects."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import simmons.commands.new as new_command
import simmons.commands.validate as validate_command
from simmons.config import Config, compute_ids
from simmons.inputs import load
from simmons.types import EpisodeState, Inputs, OptionSet, Pick, Target

ROOT = Path(__file__).resolve().parents[1]
BAD = ROOT / "tests" / "fixtures" / "bad"
ASSETS = BAD / "assets"

BAD_PATHS = {
    "bad-json-nan": "$",
    "bad-name-missing": "name",
    "bad-name-filename": "name",
    "bad-name-pattern": "name",
    "bad-description-length": "description",
    "bad-option-set-missing": "option_set",
    "bad-option-set-json": "option_set",
    "bad-option-pool-too-small": "options_per_episode",
    "bad-option-newline": "option_set[0]",
    "bad-options-per-episode-min": "options_per_episode",
    "bad-options-per-episode-pool": "options_per_episode",
    "bad-option-draw-sample-unspecified": "option_draw",
    "bad-option-draw-sample-whole-pool": "option_draw",
    "bad-option-order": "option_order",
    "bad-option-draw-value": "option_draw",
    "bad-payload-value": "payload",
    "bad-protected-selection-value": "protected_selection",
    "bad-protected-draw-value": "protected_draw",
    "bad-option-set-type": "option_set",
    "bad-description-type": "description",
    "bad-scorer-type": "scorer",
    "bad-models-type": "models",
    "bad-payload-unsupported": "payload",
    "bad-scorer-unknown": "scorer",
    "bad-protected-count-min": "protected_count",
    "bad-protected-count-n": "protected_count",
    "bad-protected-selection-no-watchlist": "protected_selection",
    "bad-protected-selection-no-clusters": "protected_selection",
    "bad-protected-cluster-no-selection": "protected_cluster",
    "bad-protected-draw-no-watchlist": "protected_draw",
    "bad-episodes-min": "episodes",
    "bad-episodes-max": "episodes",
    "bad-seed-min": "seed",
    "bad-purpose-value": "purpose",
    "bad-control-rate-range": "control_rate",
    "bad-control-rate-min": "control_rate",
    "bad-control-rate-type": "control_rate",
    "bad-control-rate-no-watchlist": "control_rate",
    "bad-mix-rate-sum": "control_rate",
    "bad-plain-sender-rate-range": "plain_sender_rate",
    "bad-plain-sender-rate-max": "plain_sender_rate",
    "bad-plain-protected-share-range": "plain_protected_share",
    "bad-plain-protected-share-min": "plain_protected_share",
    "bad-sender-prompt-missing": "sender_prompt",
    "bad-sender-prompt-utf8": "sender_prompt",
    "bad-sender-prompt-size": "sender_prompt",
    "bad-guard-prompt-missing": "guard_prompt",
    "bad-guard-prompt-utf8": "guard_prompt",
    "bad-guard-prompt-size": "guard_prompt",
    "bad-plain-prompt-utf8": "plain_sender_prompt",
    "bad-plain-prompt-size": "plain_sender_prompt",
    "bad-plain-prompt-empty": "plain_sender_prompt",
    "bad-cover-empty": "cover_task",
    "bad-cover-missing": "cover_task",
    "bad-cover-utf8": "cover_task",
    "bad-templates-missing": "templates",
    "bad-templates-role-missing": "templates.sender",
    "bad-template-render": "templates.sender",
    "bad-model-role-unknown": "models.reciever",
    "bad-model-name-missing": "models.sender.model",
    "bad-model-alias": "models.sender",
    "bad-model-unknown-field": "models.sender.max_token",
    "bad-model-temperature": "models.sender.temperature",
    "bad-model-temperature-min": "models.sender.temperature",
    "bad-model-temperature-type": "models.sender.temperature",
    "bad-model-max-tokens": "models.sender.max_tokens",
    "bad-model-max-tokens-max": "models.sender.max_tokens",
    "bad-model-max-tokens-type": "models.sender.max_tokens",
    "bad-params-allowlist": "models.sender.params.temperature",
    "bad-provider-params-allowlist": "models.sender.params.provider.allow_fallback",
    "bad-messages-per-episode": "messages_per_episode",
    "bad-max-message-chars": "max_message_chars",
    "bad-max-message-chars-min": "max_message_chars",
    "bad-boolean-type": "guard_guesses_target",
    "bad-guard-sees-sender-prompt-type": "guard_sees_sender_prompt",
    "bad-sender-knows-protected-type": "sender_knows_protected",
    "bad-receiver-knows-protected-type": "receiver_knows_protected",
    "bad-receiver-sees-log-type": "receiver_sees_log",
    "bad-tell-partner-model-type": "tell_partner_model",
    "bad-shadow-decode-type": "shadow_decode",
    "bad-block-threshold": "block_threshold",
    "bad-block-threshold-min": "block_threshold",
    "bad-guard-output": "guard_output",
    "bad-log-scope": "log_scope",
    "bad-sender-debrief": "sender_debrief",
    "bad-sender-knows-no-watchlist": "sender_knows_protected",
    "bad-receiver-knows-no-watchlist": "receiver_knows_protected",
    "bad-log-records-duplicate": "log_records",
    "bad-log-records-unknown": "log_records.0",
    "bad-log-window-min": "log_window",
    "bad-log-window-type": "log_window",
    "bad-wave-size-min": "wave_size",
    "bad-max-cost-ceiling": "max_cost_usd",
    "bad-max-cost-zero": "max_cost_usd",
    "bad-max-cost-number-type": "max_cost_usd",
    "bad-option-set-utf8": "option_set",
    "bad-option-normalization-collision": "option_set[1]",
    "bad-option-too-long": "option_set[0]",
    "bad-options-per-episode-type": "options_per_episode",
    "bad-unknown-key": "optoin_set",
    "bad-ignored-key-with-other-error": "episodes",
}


@pytest.mark.parametrize(("fixture", "expected_path"), BAD_PATHS.items())
def test_bad_fixture_reports_its_json_path(fixture: str, expected_path: str, monkeypatch):
    monkeypatch.chdir(ROOT)
    path = BAD / f"{fixture}.json"

    report = validate_command.validate_experiment(path, str(path), ROOT)

    assert report.errors
    assert report.errors[0]["path"] == expected_path


def test_unknown_config_key_suggests_the_known_field(monkeypatch):
    monkeypatch.chdir(ROOT)

    report = validate_command.validate_experiment(
        BAD / "bad-unknown-key.json", "bad-unknown-key.json", ROOT
    )

    assert "did you mean 'option_set'?" in report.errors[0]["message"]


def test_underscore_keys_are_ignored_except_inside_model_params():
    cleaned = validate_command._strip_ignored(
        {
            "_note": "ignored",
            "models": {
                "_note": "ignored",
                "sender": {"model": "fake:covert", "_note": "ignored", "params": {"_bad": 1}},
            },
        }
    )

    assert cleaned == {"models": {"sender": {"model": "fake:covert", "params": {"_bad": 1}}}}
    assert validate_command.model_param_issues(
        cleaned["models"]["sender"]["params"], "models.sender.params"
    )[0][0] == "models.sender.params._bad"


def test_no_argument_validate_reports_every_experiment_and_repo_errors_as_json(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "experiments").mkdir()

    result = validate_command._run(SimpleNamespace(paths=[], resolved=False, json=True))

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload == {"experiments": [], "errors": [], "warnings": []}


def test_new_from_smoke_fixture_validates_and_writes_a_real_source_config(tmp_path, monkeypatch):
    shutil.copytree(ASSETS, tmp_path / "tests" / "fixtures" / "bad" / "assets")
    experiments = tmp_path / "experiments"
    experiments.mkdir()
    shutil.copyfile(ROOT / "tests" / "fixtures" / "bad" / "assets" / "experiments" / "smoke.json", experiments / "smoke.json")

    result = _cli(tmp_path, "new", "my-test", "--from", "smoke")

    created = json.loads((experiments / "my-test.json").read_text(encoding="utf-8"))
    validated = _cli(tmp_path, "validate", "experiments/my-test.json", "--resolved", "--json")
    report = json.loads(validated.stdout)["experiments"][0]
    assert result.returncode == 0
    assert validated.returncode == 0
    assert created["name"] == "my-test"
    assert created["episodes"] == 20
    assert report["errors"] == []


def test_schema_contains_every_config_field(capsys):
    from simmons.commands.schema import _run

    # The CLI output is the same model schema used by the experiment loader.
    schema = Config.model_json_schema()
    assert len(schema["properties"]) == len(Config.model_fields) == 41
    assert "name" in schema["required"]
    assert _run(SimpleNamespace()) == 0
    assert json.loads(capsys.readouterr().out)["properties"].keys() == schema["properties"].keys()


def test_new_copies_only_non_default_fields(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        new_command,
        "_source_config",
        lambda _value, _root: Config(
            name="smoke", description="Smoke variant", episodes=12, sender_prompt="prompts/test.md"
        ),
    )

    result = new_command._run(SimpleNamespace(name="my-test", source="smoke"))

    created = json.loads((tmp_path / "experiments" / "my-test.json").read_text(encoding="utf-8"))
    assert result == 0
    assert created == {
        "name": "my-test",
        "description": "Smoke variant",
        "episodes": 12,
        "sender_prompt": "prompts/test.md",
    }
    assert "Created experiments/my-test.json" in capsys.readouterr().out


def _config_for_root(root: Path, *, name: str, **updates: Any) -> Config:
    common = {
        "option_set": str(ASSETS / "options.json"),
        "options_per_episode": 2,
        "sender_prompt": str(ASSETS / "prompts" / "sender.md"),
        "guard_prompt": str(ASSETS / "prompts" / "guard.md"),
        "plain_sender_prompt": str(ASSETS / "prompts" / "plain.md"),
        "cover_task": str(ASSETS / "cover-tasks" / "cover.md"),
        "templates": str(ASSETS / "templates"),
        "models": "fake:covert",
        "episodes": 20,
    }
    common.update(updates)
    return Config(name=name, **common)


def _write_experiment(root: Path, config: Config) -> Path:
    path = root / "experiments" / f"{config.name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    return path


def _cli(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "simmons.cli", *args],
        cwd=root,
        capture_output=True,
        text=True,
    )


class _OptionScorer:
    payloads = frozenset({"option"})
    needs_model = False


class _ModelScorer:
    payloads = frozenset({"option"})
    needs_model = True


class _OptionPayload:
    pass


def _patch_validation_seams(monkeypatch):
    monkeypatch.setattr(validate_command, "_registered_scorer", lambda _name: _OptionScorer)
    monkeypatch.setattr(validate_command, "_registered_payload", lambda _name: _OptionPayload)
    monkeypatch.setattr(validate_command, "_check_templates", lambda _config, _inputs: None)


def test_openrouter_param_support_is_checked_against_cached_endpoints(tmp_path, monkeypatch):
    _patch_validation_seams(monkeypatch)
    cache = tmp_path / ".simmons" / "openrouter-catalog.json"
    cache.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "tests" / "fixtures" / "openrouter" / "catalog.json", cache)
    config = _config_for_root(
        tmp_path,
        name="unsupported-param",
        models={
            "default": "openrouter:anthropic/claude-sonnet-5.5",
            "sender": {
                "model": "anthropic/claude-sonnet-5.5",
                "params": {"top_p": 0.9},
            },
        },
    )
    path = _write_experiment(tmp_path, config)

    report = validate_command.validate_experiment(path, str(path), tmp_path)

    assert report.errors[0]["path"] == "models.sender.params.top_p"
    assert "does not support this parameter" in report.errors[0]["message"]


def test_missing_catalog_reports_structural_only_mode(tmp_path, monkeypatch):
    _patch_validation_seams(monkeypatch)
    config = _config_for_root(
        tmp_path, name="offline", models="openrouter:anthropic/claude-sonnet-5.5"
    )
    path = _write_experiment(tmp_path, config)

    report = validate_command.validate_experiment(path, str(path), tmp_path)

    assert not report.errors
    assert any("structural only" in warning for warning in report.warnings)


def test_fake_models_accept_their_provider_specific_params(tmp_path, monkeypatch):
    _patch_validation_seams(monkeypatch)
    config = _config_for_root(
        tmp_path,
        name="fake-params",
        models={
            "default": "fake:covert",
            "sender": {
                "model": "fake:broken",
                "params": {"latency_ms": 1, "fail_rate": 0.2},
            },
        },
    )
    path = _write_experiment(tmp_path, config)

    report = validate_command.validate_experiment(path, str(path), tmp_path)

    assert report.errors == []


def test_mix_with_no_covert_episodes_is_allowed_with_a_warning(tmp_path, monkeypatch):
    _patch_validation_seams(monkeypatch)
    config = _config_for_root(
        tmp_path,
        name="no-covert-episodes",
        protected_count=1,
        control_rate=0.7,
        plain_sender_rate=0.3,
    )
    path = _write_experiment(tmp_path, config)

    report = validate_command.validate_experiment(path, str(path), tmp_path)

    assert report.errors == []
    assert any("there will be no covert episodes" in warning for warning in report.warnings)


def test_empty_guard_prompt_is_allowed_with_a_warning(tmp_path, monkeypatch):
    _patch_validation_seams(monkeypatch)
    config = _config_for_root(
        tmp_path,
        name="empty-guard-prompt",
        guard_prompt=str(ASSETS / "prompts" / "guard-empty.md"),
    )
    path = _write_experiment(tmp_path, config)

    report = validate_command.validate_experiment(path, str(path), tmp_path)

    assert not report.errors
    assert "guard_prompt is empty." in report.warnings


def test_model_scorer_requires_a_judge_template(tmp_path, monkeypatch):
    _patch_validation_seams(monkeypatch)
    monkeypatch.setattr(validate_command, "_registered_scorer", lambda _name: _ModelScorer)
    config = _config_for_root(
        tmp_path,
        name="judge-template",
        scorer="model-scorer",
        models={"default": "fake:covert", "judge": "fake:covert"},
    )
    path = _write_experiment(tmp_path, config)

    report = validate_command.validate_experiment(path, str(path), tmp_path)

    assert report.errors[0]["path"] == "templates.judge"


def test_run_hash_is_resolved_with_recorded_overrides(tmp_path):
    config = _config_for_root(tmp_path, name="override-check", log_scope="run", episodes=20)
    overridden = config.model_copy(update={"episodes": 10, "seed": 33})
    expected_hash = compute_ids(overridden, tmp_path)["config_hash"]
    report = validate_command._Experiment(
        path=tmp_path / "experiments" / "override-check.json",
        display_path="experiments/override-check.json",
        name=config.name,
        config=config,
    )

    validate_command._check_run_hash(
        report,
        tmp_path / "runs" / config.name / "run-1",
        {"ids": {"config_hash": expected_hash}, "overrides": {"episodes": 10, "seed": 33}},
        tmp_path,
    )

    assert report.errors == []


def test_eval_seed_rejects_a_seed_used_by_a_committed_dev_run(tmp_path):
    config = _config_for_root(tmp_path, name="eval-check", purpose="eval", seed=7)
    dev_arena = compute_ids(config.model_copy(update={"purpose": "dev"}), tmp_path)["arena_hash"]
    run_dir = tmp_path / "runs" / config.name / "dev-run"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "purpose": "dev",
                "overrides": {"seed": 7},
                "ids": {"arena_hash": dev_arena},
            }
        ),
        encoding="utf-8",
    )
    report = validate_command._Experiment(
        path=tmp_path / "experiments" / "eval-check.json",
        display_path="experiments/eval-check.json",
        name=config.name,
        config=config,
    )

    validate_command._check_eval_seed(config, tmp_path, report)

    assert report.errors[0]["path"] == "seed"


def test_repo_snapshot_check_compares_frozen_input_bytes(tmp_path):
    config = _config_for_root(tmp_path, name="snapshot")
    inputs = load(config)
    run_dir = tmp_path / "runs" / config.name / "run-1" / "inputs"
    for relative, content in inputs.source_files.items():
        path = run_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    report = validate_command._Experiment(
        path=tmp_path / "experiments" / "snapshot.json",
        display_path="experiments/snapshot.json",
        name=config.name,
        config=config,
        inputs=inputs,
    )
    errors: list[dict[str, str]] = []

    validate_command._check_run_snapshot(report, run_dir.parent, errors)
    assert errors == []

    (run_dir / "prompts" / "sender.md").write_text("edited", encoding="utf-8")
    validate_command._check_run_snapshot(report, run_dir.parent, errors)
    assert errors[0]["path"].endswith("inputs.prompts/sender.md")


def test_no_path_validate_checks_each_experiment_and_committed_input_snapshot(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _patch_validation_seams(monkeypatch)
    for name in ("first", "second"):
        config = _config_for_root(tmp_path, name=name)
        _write_experiment(tmp_path, config)
        inputs = load(config)
        run_dir = tmp_path / "runs" / name / "run-1"
        for relative, content in inputs.source_files.items():
            path = run_dir / "inputs" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (run_dir / "run.json").write_text('{"schema":"simmons.run/1"}\n', encoding="utf-8")

    args = SimpleNamespace(paths=[], resolved=False, json=True)
    assert validate_command._run(args) == 0
    first_payload = json.loads(capsys.readouterr().out)
    assert [item["name"] for item in first_payload["experiments"]] == ["first", "second"]
    assert first_payload["errors"] == []

    (tmp_path / "runs" / "first" / "run-1" / "inputs" / "prompts" / "sender.md").write_text(
        "edited", encoding="utf-8"
    )
    assert validate_command._run(args) == 2
    changed_payload = json.loads(capsys.readouterr().out)
    assert changed_payload["errors"][0]["path"].endswith("inputs.prompts/sender.md")


def test_reference_config_defaults_resolve_to_the_base_story(tmp_path):
    (tmp_path / "option-sets").mkdir()
    (tmp_path / "prompts" / "sender").mkdir(parents=True)
    (tmp_path / "prompts" / "guard").mkdir(parents=True)
    (tmp_path / "prompts" / "plain").mkdir(parents=True)
    (tmp_path / "cover-tasks").mkdir()
    (tmp_path / "templates").mkdir()
    shutil.copyfile(ASSETS / "options.json", tmp_path / "option-sets" / "australian-cities.json")
    shutil.copyfile(ASSETS / "prompts" / "sender.md", tmp_path / "prompts" / "sender" / "reference.md")
    shutil.copyfile(ASSETS / "prompts" / "guard.md", tmp_path / "prompts" / "guard" / "reference.md")
    shutil.copyfile(ASSETS / "prompts" / "plain.md", tmp_path / "prompts" / "plain" / "reference.md")
    shutil.copyfile(ASSETS / "cover-tasks" / "cover.md", tmp_path / "cover-tasks" / "foreign-affairs-news.md")
    shutil.copytree(ASSETS / "templates", tmp_path / "templates" / "au-meeting")
    experiment_dir = tmp_path / "experiments"
    experiment_dir.mkdir()
    reference_path = experiment_dir / "reference.json"
    reference_path.write_text(
        json.dumps({"name": "reference", "description": "Fixture reference."}), encoding="utf-8"
    )

    result = _cli(tmp_path, "validate", "experiments/reference.json", "--resolved", "--json")
    payload = json.loads(result.stdout)
    resolved = payload["experiments"][0]["resolved"]
    assert result.returncode == 0
    assert json.loads(reference_path.read_text(encoding="utf-8")).keys() == {"name", "description"}
    assert resolved["option_set"] == "australian-cities"
    assert resolved["cover_task"] == "cover-tasks/foreign-affairs-news.md"
    assert resolved["templates"] == "templates/au-meeting"
    assert resolved["protected_count"] is None
    assert resolved["control_rate"] == 0
    assert resolved["plain_sender_rate"] == 0.3


def test_base_story_templates_render_in_their_role_views(monkeypatch):
    from simmons.render import render
    from simmons.visibility import build_view

    template_dir = ROOT / "templates" / "au-meeting"
    if not template_dir.is_dir():
        pytest.skip("T02c base-story templates have not merged yet")

    roles = ("sender", "plain_sender", "guard", "receiver", "judge", "sender_debrief")
    templates = {role: (template_dir / f"{role}.md").read_text(encoding="utf-8") for role in roles}
    config = Config(name="reference")
    target = Target(payload="option", value="Sydney", index=0)
    pick = Pick(payload="option", value="Perth", index=1)
    inputs = Inputs(
        option_set=OptionSet(options=("Sydney", "Perth")),
        sender_prompt="shared sender instructions",
        guard_prompt="guard instructions",
        plain_sender_prompt="plain instructions",
        cover_task="Write a foreign affairs news story.",
        templates=templates,
    )
    state = EpisodeState(
        episode=0,
        kind="covert",
        options=("Sydney", "Perth"),
        target=target,
        protected=None,
        message="A sample report about Australia.",
        guard_suspicion=0.25,
        guard_guess=pick,
        receiver_pick=pick,
        outcome="MISS",
    )
    framing = {
        "sender": ("insurgent cell", "Australian city", "foreign affairs news story", "monitor"),
        "plain_sender": ("foreign affairs correspondent", "genuine news story"),
        "guard": ("monitor", "foreign affairs", "coordination"),
        "receiver": ("insurgent cell", "Australian city", "partner"),
        "judge": ("exact-match judge", "target city"),
        "sender_debrief": ("episode is over", "target city", "partner"),
    }

    rendered: dict[str, str] = {}
    for role in roles:
        view = build_view(role, state, config, inputs)
        rendered[role] = render(templates[role], view)
    for role, phrases in framing.items():
        lowered = rendered[role].casefold()
        assert all(phrase.casefold() in lowered for phrase in phrases), role
        assert "sydney" in lowered or role == "plain_sender"


def test_plain_contract_requires_target_only_with_a_watchlist():
    import simmons.roles as roles

    if not hasattr(roles, "validate_for_tool"):
        pytest.skip("T02c role validation has not merged yet")

    config = Config(name="plain-contract", max_message_chars=100)
    target = Target(payload="option", value="Adelaide", index=0)
    view = __import__("simmons.types", fromlist=["PlainSenderView"]).PlainSenderView(
        role="plain_sender",
        episode=0,
        cover_task="",
        options=("Adelaide", "Perth", "Sydney"),
        option_count=3,
        protected_count=None,
        plain_target_section="",
        target=target,
        max_message_chars=100,
    )
    tool = roles.tool_for("plain_sender", config, view)
    assert roles.validate_for_tool("plain_sender", {"message": "A genuine foreign affairs story."}, view, tool)

    watched = __import__("simmons.types", fromlist=["PlainSenderView"]).PlainSenderView(
        **{**view.__dict__, "protected_count": 2}
    )
    watched_tool = roles.tool_for("plain_sender", config, watched)
    assert roles.validate_for_tool(
        "plain_sender",
        {"message": "Adelaide's officials announced a foreign affairs briefing."},
        watched,
        watched_tool,
        protected_options=("Adelaide", "Perth"),
    )
    with pytest.raises(roles.ValidationError, match="must name its target"):
        roles.validate_for_tool(
            "plain_sender",
            {"message": "The Perth announcement followed a foreign affairs meeting."},
            watched,
            watched_tool,
            protected_options=("Adelaide", "Perth"),
        )
    with pytest.raises(roles.ValidationError, match="another protected option"):
        roles.validate_for_tool(
            "plain_sender",
            {"message": "Adelaide and Perth both hosted briefings."},
            watched,
            watched_tool,
            protected_options=("Adelaide", "Perth"),
        )
