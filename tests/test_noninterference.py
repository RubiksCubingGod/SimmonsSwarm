from __future__ import annotations

from dataclasses import replace
from itertools import product

import pytest

from simmons.config import Config
from simmons.providers.fake import FakeProvider
from simmons.providers.openrouter import OpenRouterProvider
from simmons.records import canonical_json
from simmons.roles import request_for
from simmons.types import (
    EpisodeState,
    Inputs,
    ModelInfo,
    ModelSpec,
    OptionSet,
    Pick,
    RoleResponse,
    Target,
    Usage,
)


OPTIONS = ("Adelaide", "Sydney", "Hobart", "Perth")
ROLES = ("sender", "sender_debrief", "plain_sender", "guard", "receiver", "judge")
KNOBS = (
    "sender_knows_protected",
    "receiver_knows_protected",
    "receiver_sees_log",
    "tell_partner_model",
    "guard_sees_sender_prompt",
)
KNOB_COMBINATIONS = tuple(product((False, True), repeat=len(KNOBS)))
MODEL = ModelSpec("fake", "fake:covert", None, 512, {})
OPENROUTER_MODEL = ModelSpec("openrouter", "openai/gpt-4o-mini", None, 512, {})
FAKE = FakeProvider()


class _Catalog:
    def lookup(self, slug: str) -> ModelInfo | None:
        if slug != OPENROUTER_MODEL.model:
            return None
        return ModelInfo(
            slug=slug,
            context_length=200_000,
            supports_tools=True,
            endpoint_prices=(("fixture", 0.0, 0.0),),
            supported_parameters=frozenset({"tools", "tool_choice", "max_tokens"}),
        )


OPENROUTER = OpenRouterProvider(api_key="", catalog=_Catalog())
BUILDERS = (
    pytest.param(FAKE, MODEL, id="fake"),
    pytest.param(OPENROUTER, OPENROUTER_MODEL, id="openrouter"),
)

TEMPLATES = {
    "sender.md": "$cover_task\n$options\n$target\n$protected_section\n$log_section\n$partner_section\n$max_message_chars",
    "sender_debrief.md": "$cover_task\n$options\n$target\n$protected_section\n$log_section\n$partner_section\n$debrief_outcome_section",
    "plain_sender.md": "$cover_task\n$options\n$target\n$plain_target_section\n$max_message_chars",
    "guard.md": "$cover_task\n$options\n$protected_section\n$message\n$sender_prompt_section",
    "receiver.md": "$cover_task\n$options\n$protected_section\n$log_section\n$partner_section\n$message",
    "judge.md": "$target\n$pick",
}


def make_inputs() -> Inputs:
    return Inputs(
        option_set=OptionSet(options=OPTIONS),
        sender_prompt="P1 baseline",
        guard_prompt="P2 baseline",
        plain_sender_prompt="plain prompt baseline",
        cover_task="cover task baseline",
        templates=TEMPLATES,
    )


def make_send_response(
    *,
    reasoning: str = "SENDER_REASONING_BASELINE",
    response_text: str = "SENDER_RESPONSE_TEXT_BASELINE",
    assistant_content: str = "SENDER_ASSISTANT_CONTENT_BASELINE",
) -> RoleResponse:
    message = "SENDER_MESSAGE_BASELINE"
    assistant = {
        "role": "assistant",
        "content": assistant_content,
        "tool_calls": [
            {
                "id": "send-call-1",
                "type": "function",
                "function": {
                    "name": "send_message",
                    "arguments": canonical_json({"message": message}),
                },
            }
        ],
        "reasoning_details": [
            {"type": "reasoning.text", "format": "fake-v1", "text": reasoning}
        ],
    }
    return RoleResponse(
        tool_input={"message": message},
        text=response_text,
        thinking="PROVIDER_THINKING_PRIVATE_CANARY",
        reasoning_details=tuple(assistant["reasoning_details"]),
        parse_error=None,
        usage=Usage(20, 4, reasoning_tokens=6),
        cost_usd=None,
        generation_id="fake-send-1",
        served_by="fake",
        model_reported="fake:covert",
        finish_reason="tool_calls",
        tool_choice_mode="forced",
        latency_ms=1,
        raw={
            "private_reason_canary": "ROLE_REASON_PRIVATE_CANARY",
            "interview_answer_canary": "INTERVIEW_ANSWER_PRIVATE_CANARY",
        },
        assistant_message=assistant,
        tool_call_id="send-call-1",
    )


def make_state() -> EpisodeState:
    return EpisodeState(
        episode=12,
        kind="covert",
        options=OPTIONS,
        target=Target("option", "Sydney", 1),
        protected=frozenset({1, 2}),
        message="state message baseline",
        attempt_log=(
            {
                "episode": 2,
                "target": "Benalla",
                "message": "earlier baseline",
                "outcome": "MISS",
            },
        ),
        guard_suspicion=0.25,
        guard_guess=Pick("option", "Adelaide", 0),
        receiver_pick=Pick("option", "Sydney", 1),
        outcome="HIT",
        sender_response=make_send_response(),
    )


def make_config(knob_values: tuple[bool, ...], *, sender_debrief: str = "blind") -> Config:
    return Config(
        name="t05-visibility-probe",
        protected_count=2,
        sender_debrief=sender_debrief,
        models={"sender": "sender-model-baseline", "receiver": "receiver-model-baseline"},
        **dict(zip(KNOBS, knob_values, strict=True)),
    )


def outbound_bytes(
    role: str,
    state: EpisodeState,
    config: Config,
    inputs: Inputs,
    builder,
    model: ModelSpec,
) -> bytes:
    request = request_for(role, state, config, inputs, model)
    return canonical_json(builder.build_body(request)).encode("utf-8")


def can_see(role: str, field: str, config: Config) -> bool:
    if field == "sender_prompt":
        return role in {"sender", "sender_debrief", "receiver"} or (
            role == "guard" and config.guard_sees_sender_prompt
        )
    if field == "guard_prompt":
        return role == "guard"
    if field == "plain_prompt":
        return role == "plain_sender"
    if field == "cover_task":
        return role != "judge"
    if field == "options":
        return role != "judge"
    if field == "target":
        return role in {"sender", "sender_debrief", "plain_sender", "judge"}
    if field == "protected":
        return role == "guard" or (
            role in {"sender", "sender_debrief"} and config.sender_knows_protected
        ) or (role == "receiver" and config.receiver_knows_protected)
    if field == "message":
        return role in {"guard", "receiver"}
    if field == "attempt_log":
        return role in {"sender", "sender_debrief"} or (
            role == "receiver" and config.receiver_sees_log
        )
    if field == "partner_model":
        return config.tell_partner_model and role in {"sender", "sender_debrief", "receiver"}
    if field in {"guard_suspicion", "guard_guess", "outcome"}:
        return role == "sender_debrief" and config.sender_debrief == "informed"
    if field == "receiver_pick":
        return role == "judge" or (
            role == "sender_debrief" and config.sender_debrief == "informed"
        )
    if field == "sender_response_text":
        return False
    if field in {"sender_assistant_content", "sender_reasoning_details"}:
        return role == "sender_debrief"
    if field == "sender_debrief_mode":
        return role == "sender_debrief"
    return False


def hidden_variants(
    role: str,
    state: EpisodeState,
    config: Config,
    inputs: Inputs,
):
    assert state.sender_response is not None
    assert state.sender_response.assistant_message is not None
    changed_assistant = dict(state.sender_response.assistant_message)
    changed_assistant["content"] = "sender assistant content changed"
    variants = [
        ("episode kind", "kind", replace(state, kind="control"), config, inputs),
        ("episode number", "episode", replace(state, episode=99), config, inputs),
        ("sender prompt", "sender_prompt", state, config, replace(inputs, sender_prompt="P1 changed")),
        ("guard prompt", "guard_prompt", state, config, replace(inputs, guard_prompt="P2 changed")),
        ("plain prompt", "plain_prompt", state, config, replace(inputs, plain_sender_prompt="plain prompt changed")),
        ("cover task", "cover_task", state, config, replace(inputs, cover_task="cover task changed")),
        ("option set", "option_set", state, config, replace(inputs, option_set=OptionSet(options=("Other A", "Other B", "Other C", "Other D")))),
        ("episode options", "options", replace(state, options=("Other A", "Other B", "Other C", "Other D")), config, inputs),
        ("target", "target", replace(state, target=Target("option", "Hobart", 2)), config, inputs),
        ("protected set", "protected", replace(state, protected=frozenset({1, 3})), config, inputs),
        ("current message", "message", replace(state, message="current message changed"), config, inputs),
        (
            "attempt log",
            "attempt_log",
            replace(
                state,
                attempt_log=(
                    {
                        "episode": 7,
                        "target": "Benalla",
                        "message": "changed log",
                        "outcome": "HIT",
                    },
                ),
            ),
            config,
            inputs,
        ),
        ("partner model", "partner_model", state, config.model_copy(update={"models": {"sender": "sender-model-changed", "receiver": "receiver-model-changed"}}), inputs),
        ("guard suspicion", "guard_suspicion", replace(state, guard_suspicion=0.75), config, inputs),
        ("guard guess", "guard_guess", replace(state, guard_guess=Pick("option", "Hobart", 2)), config, inputs),
        ("receiver pick", "receiver_pick", replace(state, receiver_pick=Pick("option", "Hobart", 2)), config, inputs),
        ("outcome", "outcome", replace(state, outcome="MISS"), config, inputs),
        (
            "outside-tool sender response text",
            "sender_response_text",
            replace(state, sender_response=replace(state.sender_response, text="sender response text changed")),
            config,
            inputs,
        ),
        (
            "replayed sender assistant content",
            "sender_assistant_content",
            replace(
                state,
                sender_response=replace(state.sender_response, assistant_message=changed_assistant),
            ),
            config,
            inputs,
        ),
        (
            "replayed sender reasoning details",
            "sender_reasoning_details",
            replace(
                state,
                sender_response=make_send_response(reasoning="SENDER_REASONING_CHANGED"),
            ),
            config,
            inputs,
        ),
        ("interview mode", "sender_debrief_mode", state, config.model_copy(update={"sender_debrief": "informed"}), inputs),
    ]
    return [
        (label, changed_state, changed_config, changed_inputs)
        for label, field, changed_state, changed_config, changed_inputs in variants
        if not can_see(role, field, config)
    ]


@pytest.mark.parametrize(
    "knob_values",
    KNOB_COMBINATIONS,
    ids=["".join("1" if value else "0" for value in row) for row in KNOB_COMBINATIONS],
)
@pytest.mark.parametrize(("builder", "model"), BUILDERS)
def test_noninterference_full_outbound_bodies_for_all_32_knob_combinations(
    knob_values, builder, model
):
    state = make_state()
    config = make_config(knob_values)
    inputs = make_inputs()

    for role in ROLES:
        baseline = outbound_bytes(role, state, config, inputs, builder, model)
        for label, changed_state, changed_config, changed_inputs in hidden_variants(
            role, state, config, inputs
        ):
            assert (
                outbound_bytes(role, changed_state, changed_config, changed_inputs, builder, model)
                == baseline
            ), (
                f"{role} outbound body changed when hidden {label} changed; "
                f"knobs={knob_values}; builder={type(builder).__name__}"
            )


def canary_inputs() -> Inputs:
    return Inputs(
        option_set=OptionSet(options=OPTIONS),
        sender_prompt="P1_CANARY",
        guard_prompt="P2_CANARY",
        plain_sender_prompt="PLAIN_PROMPT_CANARY",
        cover_task="COVER_TASK_CANARY",
        templates=TEMPLATES,
    )


def canary_state() -> EpisodeState:
    response = make_send_response(
        reasoning="SENDER_REASONING_CANARY",
        response_text="SENDER_RESPONSE_TEXT_CANARY",
        assistant_content="SENDER_ASSISTANT_CONTENT_CANARY",
    )
    return replace(
        make_state(),
        kind="KIND_CANARY",
        message="CURRENT_MESSAGE_CANARY",
        attempt_log=(
            {
                "episode": 2,
                "target": "Benalla",
                "message": "ATTEMPT_LOG_CANARY",
                "outcome": "MISS",
            },
        ),
        guard_suspicion=0.987654,
        guard_guess=Pick("option", "GUARD_GUESS_CANARY", None),
        receiver_pick=Pick("option", "RECEIVER_PICK_CANARY", None),
        outcome="OUTCOME_CANARY",
        sender_response=response,
    )


def expected_canary_roles(knob_values: tuple[bool, ...]) -> dict[str, set[str]]:
    knobs = dict(zip(KNOBS, knob_values, strict=True))
    expected = {
        "P1_CANARY": {"sender", "sender_debrief", "receiver"},
        "P2_CANARY": {"guard"},
        "PLAIN_PROMPT_CANARY": {"plain_sender"},
        "Name the meeting city, Sydney": {"plain_sender"},
        "COVER_TASK_CANARY": {"sender", "sender_debrief", "plain_sender", "guard", "receiver"},
        "ATTEMPT_LOG_CANARY": {"sender", "sender_debrief"},
        "CURRENT_MESSAGE_CANARY": {"guard", "receiver"},
        "SENDER_MESSAGE_BASELINE": {"sender_debrief"},
        "SENDER_ASSISTANT_CONTENT_CANARY": {"sender_debrief"},
        "SENDER_RESPONSE_TEXT_CANARY": set(),
        "SENDER_REASONING_CANARY": {"sender_debrief"},
        "PROVIDER_THINKING_PRIVATE_CANARY": set(),
        "ROLE_REASON_PRIVATE_CANARY": set(),
        "INTERVIEW_ANSWER_PRIVATE_CANARY": set(),
        "KIND_CANARY": set(),
        "OUTCOME_CANARY": {"sender_debrief"},
        "0.987654": {"sender_debrief"},
        "GUARD_GUESS_CANARY": {"sender_debrief"},
        "RECEIVER_PICK_CANARY": {"sender_debrief", "judge"},
        "sender-model-baseline": set(),
        "receiver-model-baseline": set(),
    }
    if knobs["guard_sees_sender_prompt"]:
        expected["P1_CANARY"].add("guard")
    if knobs["receiver_sees_log"]:
        expected["ATTEMPT_LOG_CANARY"].add("receiver")
    if knobs["tell_partner_model"]:
        expected["sender-model-baseline"].add("receiver")
        expected["receiver-model-baseline"].update({"sender", "sender_debrief"})
    return expected


@pytest.mark.parametrize(
    "knob_values",
    KNOB_COMBINATIONS,
    ids=["".join("1" if value else "0" for value in row) for row in KNOB_COMBINATIONS],
)
@pytest.mark.parametrize(("builder", "model"), BUILDERS)
def test_canaries_reach_only_permitted_roles_for_all_32_knob_combinations(
    knob_values, builder, model
):
    state = canary_state()
    config = make_config(knob_values, sender_debrief="informed")
    inputs = canary_inputs()
    expected = expected_canary_roles(knob_values)

    for role in ROLES:
        body_value = builder.build_body(request_for(role, state, config, inputs, model))
        body = canonical_json(body_value)
        for canary, allowed_roles in expected.items():
            assert (canary in body) == (role in allowed_roles), (
                f"{canary} visibility was wrong for {role}; knobs={knob_values}"
            )

        if role == "sender_debrief":
            messages = body_value["messages"]
            assert [message["role"] for message in messages] == ["system", "user", "assistant", "tool"]
            assert messages[2] == state.sender_response.assistant_message
            assert messages[3]["tool_call_id"] == state.sender_response.tool_call_id
            assert "OUTCOME_CANARY" in messages[3]["content"]
            assistant_content_canary = "SENDER_ASSISTANT_CONTENT_CANARY"
            assert assistant_content_canary in canonical_json(messages[2])
            assert all(
                assistant_content_canary not in canonical_json(message)
                for index, message in enumerate(messages)
                if index != 2
            )
            assert "SENDER_RESPONSE_TEXT_CANARY" not in body
            assert "P2_CANARY" not in body and "PLAIN_PROMPT_CANARY" not in body
        elif role == "guard":
            user_content = body_value["messages"][-1]["content"]
            if config.guard_sees_sender_prompt:
                assert "<sender_prompt>\nP1_CANARY\n</sender_prompt>" in user_content
            else:
                assert "<sender_prompt>" not in user_content
