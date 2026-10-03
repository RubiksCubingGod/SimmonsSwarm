from __future__ import annotations

from simmons.config import Config
from simmons.types import EpisodeState, Inputs, OptionSet, Pick, Target
from simmons.visibility import VISIBILITY_TABLE, build_view


OPTIONS = ("Adelaide", "Sydney", "Hobart")


def make_inputs() -> Inputs:
    return Inputs(
        option_set=OptionSet(options=OPTIONS),
        sender_prompt="shared P1",
        guard_prompt="private P2",
        plain_sender_prompt="plain prompt",
        cover_task="write foreign affairs news",
        templates={},
    )


def make_state(*, protected: frozenset[int] | None = None) -> EpisodeState:
    return EpisodeState(
        episode=4,
        kind="covert",
        options=OPTIONS,
        target=Target("option", "Sydney", 1),
        protected=protected,
        message="A story",
        attempt_log=({"episode": 2, "message": "Earlier story", "outcome": "MISS"},),
        guard_suspicion=0.2,
        guard_guess=Pick("option", "Adelaide", 0),
        receiver_pick=Pick("option", "Sydney", 1),
        outcome="HIT",
    )


def test_visibility_table_covers_every_role_and_forbids_guard_target():
    assert set(VISIBILITY_TABLE) == {
        "sender", "sender_debrief", "plain_sender", "guard", "receiver", "judge"
    }
    assert VISIBILITY_TABLE["guard"]["target"] is False
    views = {
        role: build_view(role, make_state(), Config(name="base"), make_inputs())
        for role in VISIBILITY_TABLE
    }
    guard = views["guard"]
    assert not hasattr(guard, "target")
    assert not hasattr(views["plain_sender"], "protected_section")
    assert not hasattr(views["judge"], "protected_count")


def test_no_watchlist_has_no_protected_list_or_count_for_any_game_view():
    config = Config(name="base", protected_count=None)
    state = make_state(protected=None)
    inputs = make_inputs()

    for role in ("sender", "sender_debrief", "plain_sender", "guard", "receiver"):
        view = build_view(role, state, config, inputs)
        assert view.protected_count is None
        if role != "plain_sender":
            assert view.protected_section == ""

    plain = build_view("plain_sender", state, config, inputs)
    assert plain.plain_target_section == ""


def test_watchlist_visibility_respects_knobs_and_plain_sender_still_gets_no_list():
    config = Config(
        name="watched",
        protected_count=1,
        sender_knows_protected=True,
        receiver_knows_protected=True,
    )
    state = make_state(protected=frozenset({1}))
    inputs = make_inputs()

    sender = build_view("sender", state, config, inputs)
    debrief = build_view("sender_debrief", state, config, inputs)
    plain = build_view("plain_sender", state, config, inputs)
    guard = build_view("guard", state, config, inputs)
    receiver = build_view("receiver", state, config, inputs)

    assert sender.protected_count == debrief.protected_count == plain.protected_count == 1
    assert "Sydney" in sender.protected_section and "Sydney" in debrief.protected_section
    assert "Sydney" in guard.protected_section and "Sydney" in receiver.protected_section
    assert not hasattr(plain, "protected_section")
    assert "Name the meeting city, Sydney" in plain.plain_target_section
    assert not hasattr(guard, "target")


def test_knob_off_sections_stay_empty_and_sender_only_gets_its_own_log():
    config = Config(
        name="base",
        protected_count=None,
        sender_knows_protected=True,
        receiver_knows_protected=False,
        receiver_sees_log=False,
        guard_sees_sender_prompt=False,
        tell_partner_model=False,
    )
    state = make_state(protected=None)
    inputs = make_inputs()

    sender = build_view("sender", state, config, inputs)
    guard = build_view("guard", state, config, inputs)
    receiver = build_view("receiver", state, config, inputs)
    assert sender.protected_section == ""
    assert sender.partner_section == ""
    assert "<attempt_log>" in sender.log_section and "Earlier story" in sender.log_section
    assert guard.protected_section == ""
    assert guard.sender_prompt_section == ""
    assert receiver.protected_section == receiver.log_section == receiver.partner_section == ""


def test_debrief_outcome_is_empty_when_blind_and_informed_when_enabled():
    state = make_state(protected=None)
    blind = build_view("sender_debrief", state, Config(name="blind", sender_debrief="blind"), make_inputs())
    informed = build_view(
        "sender_debrief", state, Config(name="informed", sender_debrief="informed"), make_inputs()
    )
    assert blind.debrief_outcome_section == ""
    assert "HIT" in informed.debrief_outcome_section
    assert "0.2" in informed.debrief_outcome_section
    assert "Adelaide" in informed.debrief_outcome_section
    assert "Sydney" in informed.debrief_outcome_section
