"""T02a config validation, model resolution, and identity tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import simmons.config as config_module
from simmons.config import Config, ModelSpecConfig, compute_ids, resolve_models
from simmons.types import ModelSpec


def _make_project(root: Path) -> None:
    (root / "option-sets").mkdir(parents=True, exist_ok=True)
    (root / "option-sets" / "cities.json").write_text('["Sydney", "Perth"]\n', encoding="utf-8")
    for name, content in (
        ("sender.md", "sender prompt"),
        ("guard.md", "guard prompt"),
        ("plain.md", "plain prompt"),
    ):
        path = root / "prompts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (root / "cover-tasks").mkdir(exist_ok=True)
    (root / "cover-tasks" / "task.md").write_text("Cover task", encoding="utf-8")
    template_dir = root / "templates" / "meeting"
    template_dir.mkdir(parents=True, exist_ok=True)
    for role in ("sender", "plain_sender", "guard", "receiver"):
        (template_dir / f"{role}.md").write_text(f"<{role}>$cover_task</{role}>", encoding="utf-8")


def _config(**updates: object) -> Config:
    values: dict[str, object] = {
        "name": "sample",
        "option_set": "cities",
        "sender_prompt": "prompts/sender.md",
        "guard_prompt": "prompts/guard.md",
        "plain_sender_prompt": "prompts/plain.md",
        "cover_task": "cover-tasks/task.md",
        "templates": "templates/meeting",
        "models": "fake:covert",
    }
    values.update(updates)
    return Config(**values)


def test_config_defaults_and_run_ceiling_validation():
    config = Config(name="default")
    assert config.log_window == 20
    assert ModelSpecConfig(model="fake:covert").max_tokens == 16000

    with pytest.raises(ValidationError) as error:
        Config(name="over-budget", max_cost_usd=25)
    assert error.value.errors()[0]["loc"] == ("max_cost_usd",)

    with pytest.raises(ValidationError, match="max_tokens"):
        ModelSpecConfig(model="fake:covert", max_tokens=0)


def test_models_resolve_aliases_and_role_fallbacks():
    config = Config(
        name="models",
        models={
            "default": "fake:random",
            "sender": "claude-sonnet-5-5",
            "receiver": {"model": "fake:covert", "max_tokens": 512},
        },
    )
    models = resolve_models(config)

    assert models["sender"] == ModelSpec(
        "openrouter", "anthropic/claude-sonnet-5.5", None, 16000, {}
    )
    assert models["receiver"] == ModelSpec("fake", "covert", None, 512, {})
    assert models["guard"] == ModelSpec("fake", "random", None, 16000, {})
    assert models["plain_sender"] == models["guard"]
    assert models["judge"] is None

    equivalent = resolve_models(
        Config(name="alias", models={"sender": "anthropic/claude-sonnet-5.5"})
    )
    assert equivalent["sender"] == resolve_models(
        Config(name="alias", models={"sender": "claude-sonnet-5-5"})
    )["sender"]

    with pytest.raises(ValueError, match=r"models.sender: unknown model alias"):
        resolve_models(Config(name="unknown", models={"sender": "not-a-model"}))


def test_hashes_are_path_independent_and_use_resolved_model_and_option_values(tmp_path: Path):
    root_a = tmp_path / "one"
    root_b = tmp_path / "two"
    _make_project(root_a)
    _make_project(root_b)
    models_a = {
        "sender": {
            "model": "fake:covert",
            "temperature": 1,
            "params": {"top_p": 0.5, "seed": 7},
        },
        "default": "fake:random",
    }
    models_b = {
        "default": "fake:random",
        "sender": {
            "params": {"seed": 7, "top_p": 0.50},
            "temperature": 1.0,
            "model": "fake:covert",
        },
    }

    ids_a = compute_ids(_config(models=models_a), root_a)
    ids_b = compute_ids(_config(models=models_b), root_b)
    assert ids_a == ids_b
    assert all(len(value) == 8 for value in ids_a.values())

    implicit_pool = compute_ids(_config(), root_a)
    explicit_pool = compute_ids(_config(options_per_episode=2), root_a)
    assert implicit_pool == explicit_pool

    alias_ids = compute_ids(_config(models={"default": "claude-sonnet-5-5"}), root_a)
    slug_ids = compute_ids(
        _config(models={"default": "openrouter:anthropic/claude-sonnet-5.5"}), root_a
    )
    assert alias_ids == slug_ids


def test_hashes_follow_the_sender_guard_and_arena_partitions(tmp_path: Path):
    root = tmp_path / "project"
    _make_project(root)
    config = _config()
    base = compute_ids(config, root)

    (root / "prompts" / "sender.md").write_text("changed sender", encoding="utf-8")
    sender_changed = compute_ids(config, root)
    assert sender_changed["sender_id"] != base["sender_id"]
    assert sender_changed["guard_id"] == base["guard_id"]
    assert sender_changed["arena_hash"] == base["arena_hash"]
    assert sender_changed["config_hash"] != base["config_hash"]

    _make_project(root)
    (root / "prompts" / "guard.md").write_text("changed guard", encoding="utf-8")
    guard_changed = compute_ids(config, root)
    assert guard_changed["sender_id"] == base["sender_id"]
    assert guard_changed["guard_id"] != base["guard_id"]
    assert guard_changed["arena_hash"] == base["arena_hash"]
    assert guard_changed["config_hash"] != base["config_hash"]

    _make_project(root)
    (root / "cover-tasks" / "task.md").write_text("changed cover", encoding="utf-8")
    arena_changed = compute_ids(config, root)
    assert arena_changed["sender_id"] == base["sender_id"]
    assert arena_changed["guard_id"] == base["guard_id"]
    assert arena_changed["arena_hash"] != base["arena_hash"]
    assert arena_changed["config_hash"] != base["config_hash"]


def test_hashes_exclude_provenance_and_apply_conditional_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "project"
    _make_project(root)
    config = _config()
    base = compute_ids(config, root)

    renamed = config.model_copy(
        update={"name": "other", "description": "metadata", "seed": 99, "episodes": 80, "max_cost_usd": 10.0}
    )
    assert compute_ids(renamed, root) == base

    no_watchlist = config.model_copy(update={"protected_count": None})
    assert compute_ids(no_watchlist, root) == base

    verdict_a = config.model_copy(update={"guard_output": "verdict", "block_threshold": 0.1})
    verdict_b = config.model_copy(update={"guard_output": "verdict", "block_threshold": 0.9})
    assert compute_ids(verdict_a, root) == compute_ids(verdict_b, root)

    run_a = config.model_copy(update={"log_scope": "run", "episodes": 10})
    run_b = config.model_copy(update={"log_scope": "run", "episodes": 20})
    assert compute_ids(run_a, root)["arena_hash"] != compute_ids(run_b, root)["arena_hash"]

    run_without_history_a = config.model_copy(
        update={"log_scope": "run", "log_records": [], "receiver_sees_log": False, "episodes": 10}
    )
    run_without_history_b = run_without_history_a.model_copy(update={"episodes": 20})
    assert compute_ids(run_without_history_a, root) == compute_ids(run_without_history_b, root)

    monkeypatch.setattr(config_module, "PROTOCOL_VERSION", config_module.PROTOCOL_VERSION + 1)
    changed_protocol = compute_ids(config, root)
    assert changed_protocol["sender_id"] == base["sender_id"]
    assert changed_protocol["guard_id"] == base["guard_id"]
    assert changed_protocol["arena_hash"] != base["arena_hash"]
    assert changed_protocol["config_hash"] != base["config_hash"]


def test_config_hash_is_the_union_of_sender_guard_and_arena_fields(tmp_path: Path):
    root = tmp_path / "project"
    _make_project(root)
    config = _config(models={"default": "fake:random"})
    base = compute_ids(config, root)

    cases = (
        (config.model_copy(update={"models": {"default": "fake:random", "sender": "fake:covert"}}), "sender_id"),
        (config.model_copy(update={"models": {"default": "fake:random", "receiver": "fake:covert"}}), "sender_id"),
        (config.model_copy(update={"models": {"default": "fake:random", "guard": "fake:covert"}}), "guard_id"),
        (config.model_copy(update={"models": {"default": "fake:random", "plain_sender": "fake:covert"}}), "arena_hash"),
        (config.model_copy(update={"block_threshold": 0.75}), "guard_id"),
        (config.model_copy(update={"guard_guesses_target": False}), "guard_id"),
        (config.model_copy(update={"guard_sees_sender_prompt": True}), "arena_hash"),
        (config.model_copy(update={"log_window": 5}), "arena_hash"),
        (config.model_copy(update={"purpose": "eval"}), "arena_hash"),
    )
    components = ("sender_id", "guard_id", "arena_hash")
    for variant, changed_component in cases:
        changed = compute_ids(variant, root)
        assert changed[changed_component] != base[changed_component]
        assert changed["config_hash"] != base["config_hash"]
        for component in components:
            if component != changed_component:
                assert changed[component] == base[component]
