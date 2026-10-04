from __future__ import annotations

from dataclasses import replace

import pytest

from simmons.config import Config
from simmons.render import render
from simmons.types import EpisodeState, Inputs, OptionSet, Pick, Target
from simmons.visibility import build_view


def _inputs() -> Inputs:
    return Inputs(
        option_set=OptionSet(options=("Adelaide", "Sydney")),
        sender_prompt="P1 secret",
        guard_prompt="P2 secret",
        plain_sender_prompt="plain prompt",
        cover_task="cover",
        templates={},
    )


def _state() -> EpisodeState:
    return EpisodeState(
        episode=0,
        kind="covert",
        options=("Adelaide", "Sydney"),
        target=Target("option", "Sydney", 1),
        protected=None,
        message="The message",
    )


def test_template_variables_are_limited_to_view_fields():
    config = Config(name="base", protected_count=None)
    inputs = _inputs()
    state = _state()
    guard = build_view("guard", state, config, inputs)
    sender = build_view("sender", state, config, inputs)

    with pytest.raises(KeyError, match="target"):
        render("$target", guard)
    with pytest.raises(KeyError, match="protected_count"):
        render("$protected_count", sender)
    assert render("$protected_section", guard) == ""


def test_renderer_uses_view_sections_and_numbered_options():
    view = build_view("sender", _state(), Config(name="base"), _inputs())
    output = render("$target\n$options\n$cover_task", view)
    assert "<target>\nSydney\n</target>" in output
    assert "1. Adelaide\n2. Sydney" in output
    assert "cover" in output


def test_rendered_sections_cannot_extend_the_typed_view():
    from dataclasses import replace

    guard = build_view("guard", _state(), Config(name="base"), _inputs())
    guard = replace(guard, rendered_sections={"target": "secret"})
    with pytest.raises(ValueError, match="exceed the guard view"):
        render("safe", guard)


@pytest.mark.parametrize(
    "role",
    ("sender", "sender_debrief", "guard", "receiver", "judge"),
)
@pytest.mark.parametrize(
    "protected_count",
    (None, 1),
    ids=("without-watchlist", "with-watchlist"),
)
def test_plain_target_section_is_rejected_for_every_other_role(role, protected_count):
    state = _state()
    if role == "judge":
        state = replace(state, receiver_pick=Pick("option", "Sydney", 1))
    if protected_count is not None:
        state = replace(
            state,
            protected=frozenset({1}),
        )
    config = Config(name="render-boundary", protected_count=protected_count)
    view = build_view(role, state, config, _inputs())

    with pytest.raises(KeyError, match="plain_target_section"):
        render("$plain_target_section", view)
