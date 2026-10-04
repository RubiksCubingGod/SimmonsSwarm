"""Small checks for frozen contracts, record shapes, and hermetic boundaries."""

from __future__ import annotations

import copy
import importlib
import importlib.util
import inspect
import json
import lzma
import os
import socket
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import get_args, get_type_hints

import pytest
from jsonschema import ValidationError

from simmons.config import Config, ModelSpecConfig
from simmons.contracts import (
    EXPLAIN_MESSAGE_FIELDS,
    EXPLAIN_MESSAGE_INPUT_SCHEMA,
    EXPLAIN_MESSAGE_TOOL_NAME,
    SEND_MESSAGE_FIELDS,
    SEND_MESSAGE_INPUT_SCHEMA,
)
from simmons.metrics import summarize
from simmons.records import (
    SCHEMAS,
    AttemptRecord,
    EpisodeRecord,
    PlanRecord,
    RunRecord,
    SummaryRecord,
    append_jsonl,
    canonical_json,
    read_jsonl,
    read_requests_xz,
    request_sha256,
    validate_record,
)
from simmons.template_tags import (
    DEBRIEF_OUTCOME_SECTION_TAG,
    PLAIN_TARGET_SECTION_TAG,
    SENDER_DEBRIEF_TEMPLATE_NAME,
    TEMPLATE_TAG_NAMES,
)
from simmons.types import ModelSpec, PriorTurn, RoleRequest, TextPart, ToolSpec


def test_frozen_types_and_exit_interview_contract():
    names = (
        "ModelSpec", "TextPart", "FilePart", "Part", "ToolSpec", "PriorTurn", "RoleRequest", "Usage",
        "RoleResponse", "TransientError", "PermanentError", "BudgetError", "BreakerTripped", "ProviderStatus",
        "Provider", "Target", "Pick", "Score", "PlanContext", "JudgeCaller", "ModelInfo", "Catalog",
        "Reservation", "Breaker", "Scorer", "Payload", "SenderView", "PlainSenderView", "GuardView",
        "ReceiverView", "JudgeView", "SenderDebriefView", "EpisodeState", "EpisodeContext", "Inputs", "OptionSet",
    )
    types = importlib.import_module("simmons.types")
    assert all(hasattr(types, name) for name in names)
    assert set(PriorTurn.__dataclass_fields__) == {"tool", "assistant", "tool_call_id", "tool_result"}
    assert get_type_hints(PriorTurn)["tool_call_id"] is str

    model = ModelSpec("fake", "covert", None, 32, {})
    tool = ToolSpec("send_message", "send", {"type": "object"})
    request = RoleRequest("r1", "sender", model, "", (), tool)
    assert request.prior is None
    assert request.attempt == 1
    assert RoleRequest("r2", "sender", model, "", (), tool, attempt=2).attempt == 2

    response_fields = types.RoleResponse.__dataclass_fields__
    assert {"reasoning_details", "assistant_message", "tool_call_id"} <= set(response_fields)
    assert response_fields["assistant_message"].default is None
    assert response_fields["tool_call_id"].default is None
    assert types.EpisodeState.__dataclass_fields__["sender_response"].default is None
    view_types = (
        types.SenderView,
        types.PlainSenderView,
        types.GuardView,
        types.ReceiverView,
        types.SenderDebriefView,
    )
    assert get_type_hints(types.EpisodeState)["protected"] == frozenset[int] | None and all(
        get_type_hints(view_type)["protected_count"] == int | None for view_type in view_types
    )

    assert Config(name="contract").sender_debrief == "blind"
    for mode in ("off", "blind", "informed"):
        assert Config(name="contract", sender_debrief=mode).sender_debrief == mode

    assert SEND_MESSAGE_FIELDS == ("message",)
    assert set(SEND_MESSAGE_INPUT_SCHEMA["properties"]) == {"message"}
    assert "reason" not in SEND_MESSAGE_INPUT_SCHEMA["required"]
    assert EXPLAIN_MESSAGE_TOOL_NAME == "explain_message"
    assert EXPLAIN_MESSAGE_FIELDS == ("scheme", "encoding", "expected_decode", "confidence")
    assert EXPLAIN_MESSAGE_INPUT_SCHEMA == {
        "type": "object",
        "properties": {
            "scheme": {"type": "string", "minLength": 1, "maxLength": 200},
            "encoding": {"type": "string", "minLength": 1, "maxLength": 3000},
            "expected_decode": {"type": "string", "maxLength": 1500},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["scheme", "encoding", "expected_decode", "confidence"],
        "additionalProperties": False,
    }
    assert "debrief_outcome_section" in types.SenderDebriefView.__dataclass_fields__
    assert DEBRIEF_OUTCOME_SECTION_TAG == "debrief_outcome_section"
    assert SENDER_DEBRIEF_TEMPLATE_NAME == "sender_debrief.md"
    assert (
        PLAIN_TARGET_SECTION_TAG == "plain_target_section"
        and PLAIN_TARGET_SECTION_TAG in TEMPLATE_TAG_NAMES
        and "plain_target_section" in types.PlainSenderView.__dataclass_fields__
    )
    assert Config(name="contract").log_window == 20 and ModelSpecConfig(model="fake:covert").max_tokens == 16000
    model_info = types.ModelInfo("fake:model", 1, True, ())
    assert get_type_hints(types.ModelInfo)["supported_parameters"] == frozenset[str] and model_info.supported_parameters == frozenset()
    assert "*.egg-info/" in Path(".gitignore").read_text(encoding="utf-8").splitlines()
    assert importlib.import_module("simmons.spend").RUN_CEILING_USD == 20.0


def test_frozen_seams_are_importable_with_declared_signatures():
    expected = {
        "simmons.inputs": {"load": ("config", "repo_root"), "snapshot": ("inputs", "destination")},
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

    plan_start = inspect.signature(importlib.import_module("simmons.plan").build_plan).parameters["start"]
    assert plan_start.default == 0
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


def test_socket_key_dotenv_and_live_boundaries():
    secret_names = (
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_MANAGEMENT_KEY",
        "ANTHROPIC_AUTH_TOKEN",
    )
    assert all(key not in os.environ for key in secret_names)
    assert os.environ["SIMMONS_DISABLE_DOTENV"] == "1"
    assert os.environ["SIMMONS_AUTHOR"] == "test"
    assert os.path.isdir(os.environ["SIMMONS_RUNS_ROOT"])

    with pytest.raises(OSError, match="network access is disabled"):
        socket.create_connection(("127.0.0.1", 1))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        with pytest.raises(OSError, match="network access is disabled"):
            sock.sendto(b"inert test", ("127.0.0.1", 1))

    project_root = Path(__file__).resolve().parents[1]
    probe = Path(__file__).with_name(f"_hermetic_import_probe_{os.getpid()}.py")
    probe.write_text(
        """import os, socket, pytest
from pathlib import Path

SECRET_NAMES = ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_MANAGEMENT_KEY", "ANTHROPIC_AUTH_TOKEN")
assert all(name not in os.environ for name in SECRET_NAMES)
assert os.environ.get("SIMMONS_DISABLE_DOTENV") == "1"
try:
    socket.create_connection(("127.0.0.1", 1), timeout=0.01)
except OSError as error:
    assert "network access is disabled" in str(error)
else:
    raise AssertionError("network access succeeded during test-module import")

def test_env_loader_cannot_restore_a_key(tmp_path):
    from simmons.env import load_env
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=inert-dotenv-sentinel", encoding="utf-8")
    load_env(tmp_path)
    assert "OPENROUTER_API_KEY" not in os.environ

def test_udp_send_is_denied():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        with pytest.raises(OSError, match="network access is disabled"):
            sock.sendto(b"inert subprocess test", ("127.0.0.1", 1))
""",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("SIMMONS_DISABLE_DOTENV", None)
    for key in secret_names:
        env[key] = "inert-hermetic-sentinel"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        result = subprocess.run(
            [sys.executable, "-B", "-m", "pytest", "-q", str(probe.relative_to(project_root))],
            cwd=project_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        probe.unlink(missing_ok=True)
    assert result.returncode == 0, result.stdout + result.stderr

    spec = importlib.util.spec_from_file_location("simmons_test_conftest", Path(__file__).with_name("conftest.py"))
    assert spec is not None and spec.loader is not None
    conftest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conftest)
    assert conftest._is_live_opt_in(SimpleNamespace(option=SimpleNamespace(markexpr="live")))
    assert not conftest._is_live_opt_in(SimpleNamespace(option=SimpleNamespace(markexpr="not live")))
    pytest_config = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]
    assert "not live" in pytest_config["addopts"]
    assert any(marker.startswith("live:") for marker in pytest_config["markers"])


def _spec_episode_example():
    spec = (Path(__file__).resolve().parents[1] / "docs" / "SPEC.md").read_text(encoding="utf-8")
    start = spec.index("### 12.4 `episodes.jsonl`")
    code_start = spec.index("```json", start) + len("```json")
    code_end = spec.index("```", code_start)
    episode = json.loads(spec[code_start:code_end])
    episode["protected"] = [item for item in episode["protected"] if isinstance(item, dict)]
    return episode


def test_episode_schema_accepts_spec_example_and_early_error():
    episode = _spec_episode_example()
    validate_record(episode)

    without_watchlist = copy.deepcopy(episode)
    without_watchlist["protected"] = None
    validate_record(without_watchlist)

    without_optional = copy.deepcopy(episode)
    without_optional["calls"]["sender_debrief"].pop("prior_reasoning")
    without_optional.pop("debrief_error")
    validate_record(without_optional)

    debrief_failed = copy.deepcopy(without_optional)
    debrief_failed["debrief_error"] = "exit interview unavailable"
    validate_record(debrief_failed)

    early_error = copy.deepcopy(without_optional)
    early_error["outcome"] = "ERROR"
    early_error["calls"]["sender"] = None
    early_error["calls"]["guard"] = None
    early_error["calls"]["sender_debrief"] = None
    early_error["guard"] = None
    early_error["messages"] = []
    early_error["pick"] = None
    early_error["score"] = None
    early_error["error"] = {"role": "sender", "message": "provider failed"}
    validate_record(early_error)

    episode_schema = SCHEMAS["episode"]
    calls_schema = episode_schema["properties"]["calls"]
    assert "sender_debrief" in calls_schema["properties"]
    assert "sender_debrief" in calls_schema["required"]
    assert "reasoning_details" in calls_schema["properties"]["sender"]["properties"]
    assert "reasoning_details" not in calls_schema["properties"]["sender"]["required"]
    assert "prior_reasoning" in calls_schema["properties"]["sender_debrief"]["properties"]
    assert "debrief_error" in EpisodeRecord.__annotations__
    assert "debrief_error" in episode_schema["properties"]


def _valid_request_record():
    tool = {"name": "send_message", "description": "send", "input_schema": {"type": "object"}}
    prior_tool = {"name": "send_message", "description": "send", "input_schema": {"type": "object"}}
    return {
        "schema": "simmons.request/1",
        "request_sha256": "a" * 64,
        "model": {"provider": "fake", "model": "covert", "temperature": None, "max_tokens": 32, "params": {}},
        "system": "system",
        "user": [{"text": "send this"}],
        "tool": tool,
        "prior": {
            "tool": prior_tool,
            "assistant": {"content": None, "tool_calls": [], "reasoning_details": []},
            "tool_call_id": "call-1",
            "tool_result": [{"text": "explain the message"}],
        },
    }


def test_record_schemas_reject_invalid_shapes_and_declare_summary_fields(tmp_path):
    summary_fields = {"labels", "source", "decode", "curve", "cost", "tokens", "messages", "latency_ms"}
    assert summary_fields <= set(SummaryRecord.__annotations__)
    assert summary_fields <= set(SCHEMAS["summary"]["properties"])
    assert {"ids", "spent_usd"} <= set(RunRecord.__annotations__)
    assert {"ids", "spent_usd"} <= set(SCHEMAS["run"]["properties"])
    assert {"episode", "target", "message", "outcome"} <= set(AttemptRecord.__annotations__)
    assert type(None) in get_args(get_type_hints(PlanRecord)["protected"])
    assert type(None) in get_args(get_type_hints(EpisodeRecord)["protected"])
    validate_record({
        "schema": "simmons.plan/1", "index": 0, "kind": "covert",
        "options": ["Adelaide"], "protected": None, "target": 0,
    })

    example = _spec_episode_example()
    invalid_episode = copy.deepcopy(example)
    invalid_episode["target"] = {}
    invalid_episode["messages"] = [42]
    invalid_episode["guard"] = {}
    invalid_episode["calls"] = {"sender_debrief": None}

    bad_records = (
        invalid_episode,
        {"schema": "simmons.attempt/1", "episode": 0, "target": "Townsville", "message": {}, "outcome": "not an outcome"},
        {
            "schema": "simmons.summary/1", "run_id": "r", "experiment": "x", "status": "complete",
            "matrix": None, "counts": {}, "rates": {"hit_rate": 7},
        },
        {"schema": "simmons.spend/1", "attempt_id": "a", "event": "reserve", "usd": 0.1, "ts": "now"},
        {**_valid_request_record(), "user": [42], "prior": 42},
    )
    for record in bad_records:
        with pytest.raises(ValidationError):
            validate_record(record)

    valid_summary = {
        "schema": "simmons.summary/1", "run_id": "r", "experiment": "x", "status": "complete",
        "matrix": None, "counts": {}, "rates": {"hit_rate": {"value": 0.25, "num": 1, "den": 4}},
        "labels": {"sender": "reference", "guard": "reference"}, "source": {"episodes_sha256": "abc"},
        "decode": {}, "curve": {}, "cost": {}, "tokens": {}, "messages": {}, "latency_ms": {},
    }
    validate_record(valid_summary)
    validate_record({"schema": "simmons.spend/1", "attempt_id": "a", "event": "settle", "usd": 0.1, "generation_id": "g", "ts": "now"})

    request_path = tmp_path / "requests.jsonl.xz"
    with lzma.open(request_path, "wt", encoding="utf-8") as stream:
        stream.write(canonical_json(_valid_request_record()) + "\n")
    assert read_requests_xz(request_path) == [_valid_request_record()]


def test_attempt_log_schema_accepts_any_log_record_subset():
    attempt_schema = SCHEMAS["attempt"]
    assert attempt_schema["required"] == ["schema", "episode"]
    assert {"receiver_pick", "guard_suspicion", "guard_guess"} <= set(AttemptRecord.__annotations__)
    validate_record({
        "schema": "simmons.attempt/1", "episode": 0, "message": "JAKARTA news story", "outcome": "HIT",
    })
    validate_record({
        "schema": "simmons.attempt/1", "episode": 1, "target": "Sydney", "outcome": "MISS",
        "guard_guess": "Canberra",
    })


def test_run_spent_usd_schema_rejects_non_numeric_value():
    with pytest.raises(ValidationError):
        validate_record({
            "schema": "simmons.run/1", "run_id": "r", "experiment": "x", "status": "complete",
            "matrix": None, "spent_usd": "not money", "ids": {},
        })


def test_run_ids_schema_rejects_non_string_values():
    with pytest.raises(ValidationError):
        validate_record({
            "schema": "simmons.run/1", "run_id": "r", "experiment": "x", "status": "complete",
            "matrix": None, "spent_usd": 1.0, "ids": {"sender": 7},
        })


def test_settle_requires_generation_id_but_allows_null():
    with pytest.raises(ValidationError):
        validate_record({
            "schema": "simmons.spend/1", "attempt_id": "a", "event": "settle", "usd": 0.1, "ts": "now",
        })
    validate_record({
        "schema": "simmons.spend/1", "attempt_id": "a", "event": "settle", "usd": 0.1,
        "generation_id": None, "ts": "now",
    })


def test_plain_target_section_is_scoped_to_plain_sender():
    types = importlib.import_module("simmons.types")
    assert "plain_target_section" in types.PlainSenderView.__dataclass_fields__
    other_views = (
        types.SenderView,
        types.GuardView,
        types.ReceiverView,
        types.JudgeView,
        types.SenderDebriefView,
    )
    assert all("plain_target_section" not in view.__dataclass_fields__ for view in other_views)


def test_request_hash_covers_prior_but_excludes_request_id_and_attempt():
    model = ModelSpec("fake", "covert", None, 32, {})
    tool = ToolSpec("send_message", "send", {"type": "object"})
    prior = PriorTurn(tool, {"reasoning_details": [{"signature": "opaque-a"}]}, "call-1", (TextPart("explain"),))
    first = RoleRequest("req-a", "sender_debrief", model, "system", (TextPart("send"),), tool, prior, attempt=1)
    same_request = RoleRequest("req-b", "other-role", model, "system", (TextPart("send"),), tool, prior, attempt=2)
    different_prior = PriorTurn(tool, {"reasoning_details": [{"signature": "opaque-b"}]}, "call-1", (TextPart("explain"),))
    changed_request = RoleRequest("req-c", "sender_debrief", model, "system", (TextPart("send"),), tool, different_prior, attempt=1)
    assert request_sha256(first) == request_sha256(same_request)
    assert request_sha256(first) != request_sha256(changed_request)

    mapping = {
        "request_id": "req-a", "role": "sender_debrief", "attempt": 1,
        "model": model, "system": "system", "user": (TextPart("send"),), "tool": tool, "prior": prior,
    }
    changed_labels = {**mapping, "request_id": "req-b", "role": "guard", "attempt": 2}
    assert request_sha256(mapping) == request_sha256(changed_labels)


def test_jsonl_reads_physical_lines_and_drops_only_a_torn_final_tail(tmp_path):
    path = tmp_path / "attempts.jsonl"
    valid = {"schema": "simmons.attempt/1", "episode": 0, "target": "Townsville", "message": "ordinary", "outcome": "HIT"}
    append_jsonl(path, valid)
    with path.open("ab") as stream:
        stream.write(b'{"schema":"simmons.attempt/1","episode":1,"target":"Sydney","message":"caf\xc3')
    assert read_jsonl(path) == [valid]

    path.write_bytes((canonical_json(valid) + "\n").encode("utf-8") + b'{"schema":"simmons.attempt/1","episode":1')
    assert read_jsonl(path) == [valid]

    unicode_record = {**valid, "message": "first paragraph\u2028second paragraph"}
    path.unlink()
    append_jsonl(path, unicode_record)
    assert read_jsonl(path) == [unicode_record]

    path.write_bytes((canonical_json(valid) + "\n").encode("utf-8") + b'{"schema":"simmons.attempt/1","episode":1,"target":"Sydney","message":"caf\xc3')
    assert read_jsonl(path) == [valid]

    path.write_text('{"schema":"simmons.attempt/2","episode":0}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported record schema"):
        read_jsonl(path)


def test_minimal_rates_use_receiver_eligible_denominators_and_exclude_errors():
    rows = ([{"kind": "covert", "outcome": outcome} for outcome, count in (("CATCH", 61), ("HIT", 18), ("MISS", 60), ("ERROR", 1)) for _ in range(count)])
    rows += [{"kind": "control", "outcome": outcome} for outcome, count in (("CATCH", 4), ("HIT", 9), ("MISS", 27), ("ERROR", 1)) for _ in range(count)]
    rows += [{"kind": "plain_allowed", "outcome": outcome} for outcome, count in (("CATCH", 1), ("HIT", 9), ("ERROR", 1)) for _ in range(count)]
    rows += [{"kind": "plain_protected", "outcome": outcome} for outcome, count in (("CATCH", 10), ("ERROR", 1)) for _ in range(count)]
    rates = summarize(rows)["rates"]
    assert rates["hit_rate"]["num"] == 18
    assert rates["hit_rate"]["den"] == 139
    assert rates["hit_rate"]["value"] == pytest.approx(18 / 139)
    assert rates["catch_rate"] == {"value": pytest.approx(61 / 139), "num": 61, "den": 139}
    assert rates["false_alarm_rate"] == {"value": pytest.approx(5 / 50), "num": 5, "den": 50}
    assert rates["sanity_catch_rate"] == {"value": 1.0, "num": 10, "den": 10}

    all_caught = summarize([{"kind": "covert", "outcome": "CATCH"}] * 2)["rates"]["hit_rate"]
    assert all_caught == {"value": 0.0, "num": 0, "den": 2}
