"""T02a input loading and snapshot tests using isolated temporary assets."""

from __future__ import annotations

from pathlib import Path

import pytest

from simmons.config import Config
from simmons.inputs import load, snapshot
from simmons.template_tags import SENDER_DEBRIEF_TEMPLATE_NAME
from simmons.types import Inputs


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _make_project(
    root: Path,
    *,
    options: bytes = b'["Sydney", "Perth"]\n',
    sender: bytes = b"",
    guard: bytes = b"guard prompt\r\nsecond line",
    plain: bytes = b"plain prompt",
    cover: bytes = b"# Cover\r\nWrite a story.",
) -> Config:
    _write(root / "assets" / "options.json", options)
    _write(root / "assets" / "sender.md", sender)
    _write(root / "assets" / "guard.md", guard)
    _write(root / "assets" / "plain.md", plain)
    _write(root / "assets" / "cover.md", cover)
    for role in ("sender", "plain_sender", "guard", "receiver"):
        _write(root / "assets" / "templates" / f"{role}.md", f"<{role}>\r\n$cover_task".encode())
    _write(
        root / "assets" / "templates" / SENDER_DEBRIEF_TEMPLATE_NAME,
        b"<sender_debrief>\r\n$target",
    )
    return Config(
        name="inputs-test",
        option_set="assets/options.json",
        options_per_episode=2,
        sender_prompt="assets/sender.md",
        guard_prompt="assets/guard.md",
        plain_sender_prompt="assets/plain.md",
        cover_task="assets/cover.md",
        templates="assets/templates",
    )


def test_load_preserves_role_inputs_and_snapshot_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = _make_project(tmp_path)
    _write(tmp_path / "assets" / "templates" / "notes" / "brief.txt", b"template asset\r\n")
    _write(tmp_path / "assets" / "templates" / "notes" / "brief.txt~", b"editor backup")
    _write(tmp_path / "assets" / "templates" / ".DS_Store", b"machine-local")
    monkeypatch.chdir(tmp_path)
    inputs = load(config)

    assert isinstance(inputs, Inputs)
    assert inputs.option_set.options == ("Sydney", "Perth")
    assert inputs.option_set.name is None
    assert inputs.sender_prompt == ""  # empty P1 becomes an empty system prompt
    assert inputs.guard_prompt == "guard prompt\r\nsecond line"
    assert inputs.plain_sender_prompt == "plain prompt"
    assert inputs.cover_task == "# Cover\r\nWrite a story."
    assert inputs.templates["sender"] == "<sender>\r\n$cover_task"

    destination = tmp_path / "run" / "inputs"
    assert snapshot(inputs, destination) == destination
    expected = {
        "option-set.json": b'["Sydney", "Perth"]\n',
        "prompts/sender.md": b"",
        "prompts/guard.md": b"guard prompt\r\nsecond line",
        "prompts/plain_sender.md": b"plain prompt",
        "cover-task.md": b"# Cover\r\nWrite a story.",
        "templates/sender.md": b"<sender>\r\n$cover_task",
        "templates/plain_sender.md": b"<plain_sender>\r\n$cover_task",
        "templates/guard.md": b"<guard>\r\n$cover_task",
        "templates/receiver.md": b"<receiver>\r\n$cover_task",
        "templates/sender_debrief.md": b"<sender_debrief>\r\n$target",
        "templates/notes/brief.txt": b"template asset\r\n",
    }
    for relative, content in expected.items():
        assert (destination / relative).read_bytes() == content
    assert not (destination / "templates" / ".DS_Store").exists()
    assert not (destination / "templates" / "notes" / "brief.txt~").exists()


def test_option_sets_reject_normalization_collisions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = _make_project(tmp_path, options='["Cairns", "  CAIRNS  "]\n'.encode())
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match=r"option_set\[1\].*collides.*option_set\[0\]"):
        load(config)


@pytest.mark.parametrize("mode", ["blind", "informed"])
def test_load_exposes_configured_exit_interview_template(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
):
    config = _make_project(tmp_path).model_copy(update={"sender_debrief": mode})
    monkeypatch.chdir(tmp_path)

    inputs = load(config)

    assert inputs.templates[SENDER_DEBRIEF_TEMPLATE_NAME] == "<sender_debrief>\r\n$target"


def test_load_may_omit_exit_interview_template_when_debrief_is_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    config = _make_project(tmp_path).model_copy(update={"sender_debrief": "off"})
    (tmp_path / "assets" / "templates" / SENDER_DEBRIEF_TEMPLATE_NAME).unlink()
    monkeypatch.chdir(tmp_path)

    inputs = load(config)

    assert SENDER_DEBRIEF_TEMPLATE_NAME not in inputs.templates


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (b'[]', "options_per_episode: exceeds the option_set size"),
        (b'["Sydney"]', "options_per_episode: exceeds the option_set size"),
        (b'{"options": ["Sydney"]}', "lands in v1.1"),
        (b'["Sydney", "Perth\\nTown"]', "must not contain a newline"),
        (b'[" ", "Perth"]', "empty after normalization"),
    ],
)
def test_option_set_validation_is_clear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    options: bytes,
    message: str,
):
    config = _make_project(tmp_path, options=options)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match=message):
        load(config)


def test_whole_pool_requires_at_least_two_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = _make_project(tmp_path, options=b'["Sydney"]')
    config = config.model_copy(update={"options_per_episode": None})
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="options_per_episode: must be at least 2"):
        load(config)


def test_input_requirements_and_pool_size_are_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = _make_project(tmp_path, plain=b"  ")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="plain_sender_prompt: must be non-empty"):
        load(config)

    config = _make_project(tmp_path, cover=b"")
    with pytest.raises(ValueError, match="cover_task: must be non-empty"):
        load(config)

    config = _make_project(tmp_path).model_copy(update={"options_per_episode": 3})
    with pytest.raises(ValueError, match="options_per_episode: exceeds the option_set size"):
        load(config)
