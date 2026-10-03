"""Small checks for the frozen contract and hermetic test boundary."""

from __future__ import annotations

import importlib
import inspect
import json
import os
import socket

import pytest

from simmons.config import Config
from simmons.contracts import SEND_MESSAGE_FIELDS, SEND_MESSAGE_INPUT_SCHEMA
from simmons.records import read_jsonl
from simmons.types import ModelSpec, PriorTurn, RoleRequest


def test_frozen_types_and_exit_interview_contract():
    names = (
        "ModelSpec", "TextPart", "FilePart", "ToolSpec", "PriorTurn", "RoleRequest", "Usage",
        "RoleResponse", "TransientError", "PermanentError", "BudgetError", "ProviderStatus",
        "Provider", "Target", "Pick", "Score", "PlanContext", "JudgeCaller", "ModelInfo",
        "Catalog", "Reservation", "Breaker", "Scorer", "Payload", "SenderView",
        "PlainSenderView", "GuardView", "ReceiverView", "JudgeView", "SenderDebriefView",
        "EpisodeState", "EpisodeContext", "Inputs", "OptionSet",
    )
    types = importlib.import_module("simmons.types")
    assert all(hasattr(types, name) for name in names)

    model = ModelSpec("fake", "covert", None, 32, {})
    request = RoleRequest("r1", "sender", model, "", (), object())
    assert request.prior is None
    assert Config(name="contract").sender_debrief == "blind"
    assert SEND_MESSAGE_FIELDS == ("message",)
    assert "reason" not in SEND_MESSAGE_INPUT_SCHEMA["properties"]
    assert "reason" not in SEND_MESSAGE_INPUT_SCHEMA["required"]
    assert PriorTurn.__dataclass_fields__["assistant"].type


def test_frozen_seams_are_importable_with_declared_signatures():
    expected = {
        "simmons.inputs": {"load": ("config",), "snapshot": ("inputs", "destination")},
        "simmons.plan": {"build_plan": ("config", "options", "start")},
        "simmons.visibility": {"build_view": ("role", "state", "config", "inputs")},
        "simmons.render": {"render": ("template", "view")},
        "simmons.roles": {
            "tool_for": ("role", "config", "view"),
            "validate": ("role", "tool_input", "view"),
            "request_for": ("role", "state", "config", "inputs", "model"),
        },
        "simmons.episode": {"run_episode": ("line", "ctx")},
        "simmons.catalog": {"load_catalog": ("path",)},
        "simmons.metrics": {"summarize": ("episodes",)},
        "simmons.spend": {
            "estimate": ("config", "plan", "inputs", "catalog"),
            "check_before_run": ("config", "estimate_usd", "remaining_usd"),
            "between_waves": ("breaker", "remaining_usd"),
        },
    }
    for module_name, functions in expected.items():
        module = importlib.import_module(module_name)
        for name, parameters in functions.items():
            function = getattr(module, name)
            assert tuple(inspect.signature(function).parameters) == parameters

    assert tuple(inspect.signature(importlib.import_module("simmons.catalog").Catalog.lookup).parameters) == ("self", "slug")
    spend_breaker = importlib.import_module("simmons.spend").Breaker
    for name, parameters in {
        "reserve": ("self", "attempt_id", "body", "spec"),
        "settle": ("self", "reservation", "response"),
        "unresolved": ("self", "reservation", "reason"),
        "tripped": ("self",),
        "spent_usd": ("self",),
    }.items():
        member = getattr(spend_breaker, name)
        if isinstance(member, property):
            member = member.fget
        assert tuple(inspect.signature(member).parameters) == parameters
    request_for = importlib.import_module("simmons.roles").request_for
    assert inspect.signature(request_for).return_annotation is RoleRequest
    for module_name, name in (
        ("simmons.episode", "run_episode"),
        ("simmons.runner", "run"),
        ("simmons.events", "emit"),
        ("simmons.env", "load_env"),
        ("simmons.providers.fake", "FakeProvider"),
        ("simmons.providers.openrouter", "OpenRouterProvider"),
    ):
        assert getattr(importlib.import_module(module_name), name)


def test_socket_and_provider_keys_are_blocked():
    with pytest.raises(OSError, match="network access is disabled"):
        socket.create_connection(("127.0.0.1", 1))
    assert "OPENROUTER_API_KEY" not in os.environ
    assert "OPENAI_API_KEY" not in os.environ
    assert os.environ["SIMMONS_AUTHOR"] == "test"
    assert os.path.isdir(os.environ["SIMMONS_RUNS_ROOT"])


def test_jsonl_reader_refuses_unknown_major_and_drops_torn_tail(tmp_path):
    path = tmp_path / "episodes.jsonl"
    path.write_text(
        json.dumps({"schema": "simmons.attempt/1", "episode": 0}) + "\n" + '{"schema":"simmons.attempt/1"',
        encoding="utf-8",
    )
    assert read_jsonl(path) == [{"schema": "simmons.attempt/1", "episode": 0}]

    path.write_text('{"schema":"simmons.attempt/2","episode":0}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported record schema"):
        read_jsonl(path)
