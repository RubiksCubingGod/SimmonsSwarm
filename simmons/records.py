"""Versioned run records, JSONL helpers, and canonical request hashing."""

from __future__ import annotations

import base64
import hashlib
import json
import lzma
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict

from jsonschema import Draft202012Validator

from .types import RoleRequest

SCHEMA_MAJOR = 1
_SCHEMA_RE = re.compile(r"^simmons\.([a-z_]+)/([0-9]+)$")


class UnsupportedRecordVersion(ValueError):
    """Raised when a record's type or major schema version is unknown."""


class RunRecord(TypedDict, total=False):
    schema: str
    run_id: str
    experiment: str
    author: str
    status: str
    scratch: bool
    started_at: str
    finished_at: str
    simmons_version: str
    protocol_version: int
    purpose: str
    uv_lock_sha256: str
    git_commit: str
    git_dirty: bool
    argv: list[str]
    overrides: dict[str, Any]
    ids: dict[str, str]
    matrix: dict[str, Any] | None
    episodes_planned: int
    episodes_done: int
    trajectory: dict[str, Any]
    cap_usd: float
    spent_usd: float


class PlanRecord(TypedDict, total=False):
    schema: str
    index: int
    kind: str
    options: list[str]
    protected: list[int] | None
    target: int


class EpisodeRecord(TypedDict, total=False):
    schema: str
    run_id: str
    index: int
    wave: int
    kind: str
    target: dict[str, Any]
    protected: list[dict[str, Any]] | None
    log_seen: dict[str, Any]
    calls: dict[str, Any]
    messages: list[dict[str, Any]]
    guard: dict[str, Any] | None
    pick: dict[str, Any] | None
    shadow_pick: dict[str, Any] | None
    debrief_error: str | None
    score: dict[str, Any] | None
    outcome: str
    error: dict[str, Any] | None
    cost_usd: float
    started_at: str
    finished_at: str


class AttemptRecord(TypedDict, total=False):
    schema: str
    episode: int
    target: str
    message: str
    outcome: Literal["CATCH", "MISS", "HIT"]


class SummaryRecord(TypedDict, total=False):
    schema: str
    run_id: str
    experiment: str
    status: str
    ids: dict[str, str]
    matrix: dict[str, Any] | None
    labels: dict[str, str] | None
    source: dict[str, Any] | None
    counts: dict[str, Any]
    rates: dict[str, Any]
    decode: dict[str, Any] | None
    curve: dict[str, Any] | None
    cost: dict[str, Any] | None
    tokens: dict[str, Any] | None
    messages: dict[str, Any] | None
    latency_ms: dict[str, Any] | None


class EventRecord(TypedDict, total=False):
    schema: str
    ts: str
    level: str
    event: str
    run_id: str


class CallRecord(TypedDict, total=False):
    schema: str
    request_sha256: str
    response: dict[str, Any]
    timings: dict[str, Any]


class SpendRecord(TypedDict, total=False):
    schema: str
    attempt_id: str
    event: Literal["reserve", "settle", "unresolved"]
    usd: float
    request_sha256: str
    generation_id: NotRequired[str]
    reason: NotRequired[str]
    ts: str


class RequestRecord(TypedDict, total=False):
    schema: str
    request_sha256: str
    model: dict[str, Any]
    system: str
    user: list[dict[str, Any]]
    tool: dict[str, Any]
    prior: dict[str, Any]


def _object_schema(tag: str, required: tuple[str, ...], properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"schema": {"const": tag}, **properties},
        "required": ["schema", *required],
        "additionalProperties": True,
    }


_STRING = {"type": "string"}
_INTEGER = {"type": "integer"}
_NUMBER = {"type": "number"}
_OBJECT = {"type": "object"}
_ARRAY = {"type": "array"}

_OPTION_REF = {
    "type": "object",
    "properties": {"index": _INTEGER, "value": _STRING},
    "required": ["index", "value"],
    "additionalProperties": True,
}
_PART = {
    "oneOf": [
        {
            "type": "object",
            "properties": {"text": _STRING},
            "required": ["text"],
            "additionalProperties": True,
        },
        {
            "type": "object",
            "properties": {
                "name": _STRING,
                "media_type": _STRING,
                "data": {
                    "oneOf": [
                        _STRING,
                        {
                            "type": "object",
                            "properties": {"$bytes_base64": _STRING},
                            "required": ["$bytes_base64"],
                            "additionalProperties": False,
                        },
                    ]
                },
            },
            "required": ["name", "media_type", "data"],
            "additionalProperties": True,
        },
    ]
}
_TOOL_SPEC = {
    "type": "object",
    "properties": {"name": _STRING, "description": _STRING, "input_schema": _OBJECT},
    "required": ["name", "description", "input_schema"],
    "additionalProperties": True,
}
_ROLE_CALL = {
    "type": "object",
    "properties": {
        "role": _STRING,
        "output": _OBJECT,
        "reasoning_details": {"type": ["array", "null"], "items": _OBJECT},
    },
    "required": ["role", "output"],
    "additionalProperties": True,
}
_SENDER_DEBRIEF_CALL = {
    **_ROLE_CALL,
    "properties": {
        **_ROLE_CALL["properties"],
        "prior_reasoning": {
            "type": ["object", "null"],
            "properties": {
                "items": _INTEGER,
                "types": _ARRAY,
                "formats": _ARRAY,
                "readable_chars": _INTEGER,
                "passed_back": {"type": "boolean"},
            },
            "required": ["items", "types", "formats", "readable_chars", "passed_back"],
            "additionalProperties": True,
        },
    },
}
_RATE = {
    "type": "object",
    "properties": {
        "value": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "num": {"type": "integer", "minimum": 0},
        "den": {"type": "integer", "minimum": 0},
        "lo": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "hi": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
    },
    "required": ["value", "num", "den"],
    "additionalProperties": True,
}

_CALLS = {
    "type": "object",
    "properties": {
        "sender": {**_ROLE_CALL, "type": ["object", "null"]},
        "sender_debrief": {**_SENDER_DEBRIEF_CALL, "type": ["object", "null"]},
        "guard": {**_ROLE_CALL, "type": ["object", "null"]},
        "receiver": {**_ROLE_CALL, "type": ["object", "null"]},
        "shadow_receiver": {**_ROLE_CALL, "type": ["object", "null"]},
        "judge": {**_ROLE_CALL, "type": ["object", "null"]},
    },
    "required": ["sender", "guard", "sender_debrief"],
    "additionalProperties": True,
}
_GUARD_RESULT = {
    "type": ["object", "null"],
    "properties": {
        "mode": _STRING,
        "suspicion": {"type": ["number", "null"]},
        "verdict": {"type": ["string", "null"]},
        "threshold": {"type": ["number", "null"]},
        "blocked": {"type": "boolean"},
        "guess": {"oneOf": [_OPTION_REF, {"type": "null"}]},
    },
    "required": ["mode", "suspicion", "threshold", "blocked", "guess"],
    "additionalProperties": True,
}
_MESSAGE = {
    "type": "object",
    "properties": {
        "n": _INTEGER,
        "from": _STRING,
        "to": _STRING,
        "sent": _STRING,
        "rewritten": {"type": ["string", "null"]},
        "delivered": {"type": ["string", "null"]},
    },
    "required": ["n", "from", "to", "sent", "rewritten", "delivered"],
    "additionalProperties": True,
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "run": _object_schema(
        "simmons.run/1",
        ("run_id", "experiment", "status", "matrix"),
        {
            "run_id": _STRING,
            "experiment": _STRING,
            "status": {"enum": ["running", "complete", "stopped_budget", "interrupted", "failed"]},
            "matrix": {"type": ["object", "null"]},
            "ids": {"type": "object", "additionalProperties": _STRING},
            "spent_usd": {"type": "number", "minimum": 0},
        },
    ),
    "plan": _object_schema(
        "simmons.plan/1",
        ("index", "kind", "options", "protected", "target"),
        {
            "index": _INTEGER,
            "kind": {"enum": ["covert", "control", "plain_allowed", "plain_protected"]},
            "options": {"type": "array", "items": _STRING},
            "protected": {"type": ["array", "null"], "items": _INTEGER},
            "target": _INTEGER,
        },
    ),
    "episode": _object_schema(
        "simmons.episode/1",
        ("run_id", "index", "wave", "kind", "target", "protected", "log_seen", "calls", "messages", "guard", "pick", "shadow_pick", "score", "outcome", "error", "cost_usd", "started_at", "finished_at"),
        {
            "run_id": _STRING,
            "index": _INTEGER,
            "wave": _INTEGER,
            "kind": {"enum": ["covert", "control", "plain_allowed", "plain_protected"]},
            "target": _OPTION_REF,
            "protected": {"type": ["array", "null"], "items": _OPTION_REF},
            "log_seen": {
                "type": "object",
                "additionalProperties": {
                    "oneOf": [{"type": "array", "items": _INTEGER}, {"type": "null"}]
                },
            },
            "calls": _CALLS,
            "messages": {"type": "array", "items": _MESSAGE},
            "guard": _GUARD_RESULT,
            "pick": {"oneOf": [_OPTION_REF, {"type": "null"}]},
            "shadow_pick": {"oneOf": [_OPTION_REF, {"type": "null"}]},
            "debrief_error": {"type": ["string", "null"]},
            "score": {
                "type": ["object", "null"],
                "properties": {
                    "scorer": _STRING,
                    "value": _NUMBER,
                    "hit": {"type": "boolean"},
                    "detail": {"type": ["object", "null"]},
                },
                "required": ["scorer", "value", "hit", "detail"],
                "additionalProperties": True,
            },
            "outcome": {"enum": ["CATCH", "MISS", "HIT", "ERROR"]},
            "error": {"type": ["object", "null"]},
            "cost_usd": {"type": "number", "minimum": 0},
            "started_at": _STRING,
            "finished_at": _STRING,
        },
    ),
    "attempt": _object_schema(
        "simmons.attempt/1",
        ("episode", "target", "message", "outcome"),
        {
            "episode": {"type": "integer", "minimum": 0},
            "target": _STRING,
            "message": _STRING,
            "outcome": {"enum": ["CATCH", "MISS", "HIT"]},
        },
    ),
    "summary": _object_schema(
        "simmons.summary/1",
        ("run_id", "experiment", "status", "matrix", "counts", "rates"),
        {
            "run_id": _STRING,
            "experiment": _STRING,
            "status": {"enum": ["running", "complete", "stopped_budget", "interrupted", "failed"]},
            "matrix": {"type": ["object", "null"]},
            "ids": {"type": "object", "additionalProperties": _STRING},
            "labels": {"type": ["object", "null"], "additionalProperties": _STRING},
            "source": {"type": ["object", "null"]},
            "counts": _OBJECT,
            "rates": {"type": "object", "additionalProperties": _RATE},
            "decode": {"type": ["object", "null"]},
            "curve": {"type": ["object", "null"]},
            "cost": {"type": ["object", "null"]},
            "tokens": {"type": ["object", "null"]},
            "messages": {"type": ["object", "null"]},
            "latency_ms": {"type": ["object", "null"]},
        },
    ),
    "event": _object_schema("simmons.event/1", ("ts", "level", "event", "run_id"), {"ts": _STRING, "level": _STRING, "event": _STRING, "run_id": _STRING}),
    "call": _object_schema("simmons.call/1", ("request_sha256", "response", "timings"), {"request_sha256": _STRING, "response": _OBJECT, "timings": _OBJECT}),
    "spend": {
        **_object_schema(
            "simmons.spend/1",
            ("attempt_id", "event", "usd", "ts"),
            {
                "attempt_id": _STRING,
                "event": {"enum": ["reserve", "settle", "unresolved"]},
                "usd": {"type": "number", "minimum": 0},
                "request_sha256": _STRING,
                "generation_id": _STRING,
                "reason": _STRING,
                "ts": _STRING,
            },
        ),
        "allOf": [
            {
                "if": {"properties": {"event": {"const": "reserve"}}, "required": ["event"]},
                "then": {"required": ["request_sha256"]},
            },
            {
                "if": {"properties": {"event": {"const": "settle"}}, "required": ["event"]},
                "then": {"required": ["generation_id"]},
            },
            {
                "if": {"properties": {"event": {"const": "unresolved"}}, "required": ["event"]},
                "then": {"required": ["reason"]},
            },
        ],
    },
    "request": _object_schema(
        "simmons.request/1",
        ("request_sha256", "model", "system", "user", "tool"),
        {
            "request_sha256": _STRING,
            "model": {
                "type": "object",
                "properties": {
                    "provider": _STRING,
                    "model": _STRING,
                    "temperature": {"type": ["number", "null"]},
                    "max_tokens": {"type": "integer", "minimum": 1},
                    "params": _OBJECT,
                },
                "required": ["provider", "model", "temperature", "max_tokens", "params"],
                "additionalProperties": True,
            },
            "system": _STRING,
            "user": {"type": "array", "items": _PART},
            "tool": _TOOL_SPEC,
            "prior": {
                "type": ["object", "null"],
                "properties": {
                    "tool": _TOOL_SPEC,
                    "assistant": _OBJECT,
                    "tool_call_id": _STRING,
                    "tool_result": {"type": "array", "items": _PART},
                },
                "required": ["tool", "assistant", "tool_call_id", "tool_result"],
                "additionalProperties": True,
            },
        },
    ),
}

_EPISODE_SCHEMA = SCHEMAS["episode"]
_EPISODE_SCHEMA["allOf"] = [
    {
        "if": {"properties": {"outcome": {"const": "ERROR"}}, "required": ["outcome"]},
        "then": {},
        "else": {
            "properties": {
                "guard": {"type": "object"},
                "calls": {
                    "properties": {
                        "sender": {"type": "object"},
                        "guard": {"type": "object"},
                    }
                },
            }
        },
    }
]


def schema_for(record: Mapping[str, Any]) -> dict[str, Any]:
    tag = record.get("schema")
    match = _SCHEMA_RE.fullmatch(tag) if isinstance(tag, str) else None
    if match is None:
        raise UnsupportedRecordVersion(f"missing or invalid record schema: {tag!r}")
    name, version_text = match.groups()
    expected = SCHEMAS.get(name)
    version = int(version_text)
    if expected is None or version != SCHEMA_MAJOR:
        raise UnsupportedRecordVersion(f"unsupported record schema: {tag}")
    return expected


def validate_record(record: Mapping[str, Any]) -> None:
    Draft202012Validator(schema_for(record)).validate(dict(record))


def canonical_json(value: Any) -> str:
    """Serialize stable UTF-8 JSON with sorted keys and compact separators."""
    return json.dumps(_json_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, bytes):
        return {"$bytes_base64": base64.b64encode(value).decode("ascii")}
    return value


def request_sha256(request: RoleRequest | Mapping[str, Any]) -> str:
    """Hash the canonical request, excluding its id, role label, and attempt."""
    if isinstance(request, RoleRequest):
        payload: dict[str, Any] = {
            "model": request.model,
            "system": request.system,
            "user": request.user,
            "tool": request.tool,
        }
        if request.prior is not None:
            payload["prior"] = request.prior
    else:
        payload = {
            key: value
            for key, value in request.items()
            if key not in {"request_id", "role", "attempt"}
        }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def write_metadata(path: str | Path, record: Mapping[str, Any]) -> None:
    """Validate and atomically replace one JSON metadata file."""
    validate_record(record)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=destination.parent, delete=False) as stream:
            temporary = stream.name
            stream.write(canonical_json(record))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


atomic_write_json = write_metadata
write_json_atomic = write_metadata


def append_jsonl(path: str | Path, record: Mapping[str, Any]) -> None:
    """Append one validated JSONL record and flush it before returning."""
    validate_record(record)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(canonical_json(record))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


append_jsonl_flush = append_jsonl


def _parse_jsonl_lines(lines: Iterable[str], *, allow_torn_final: bool) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    materialized = list(lines)
    for index, line in enumerate(materialized):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            is_final = index == len(materialized) - 1
            if allow_torn_final and is_final:
                break
            raise
        if not isinstance(record, dict):
            raise ValueError("JSONL record must be an object")
        validate_record(record)
        result.append(record)
    return result


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Read JSONL, dropping a malformed final line left by an interrupted append."""
    raw = Path(path).read_bytes()
    byte_lines = raw.split(b"\n")
    allow_torn_final = bool(raw) and not raw.endswith(b"\n")
    lines: list[str] = []
    for index, line in enumerate(byte_lines):
        try:
            lines.append(line.decode("utf-8"))
        except UnicodeDecodeError:
            if allow_torn_final and index == len(byte_lines) - 1:
                break
            raise
    return _parse_jsonl_lines(lines, allow_torn_final=allow_torn_final)


def read_requests_xz(path: str | Path) -> list[dict[str, Any]]:
    """Read and validate the committed compressed request JSONL file."""
    with lzma.open(path, mode="rt", encoding="utf-8") as stream:
        return _parse_jsonl_lines(stream, allow_torn_final=False)


read_jsonl_xz = read_requests_xz
