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
from typing import Any, NotRequired, TypedDict

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
    protected: list[int]
    target: int


class EpisodeRecord(TypedDict, total=False):
    schema: str
    run_id: str
    index: int
    wave: int
    kind: str
    target: dict[str, Any]
    protected: list[dict[str, Any]]
    log_seen: dict[str, Any]
    calls: dict[str, Any]
    messages: list[dict[str, Any]]
    guard: dict[str, Any]
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
    outcome: str


class SummaryRecord(TypedDict, total=False):
    schema: str
    run_id: str
    experiment: str
    status: str
    ids: dict[str, str]
    matrix: dict[str, Any] | None
    counts: dict[str, Any]
    rates: dict[str, Any]


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
    event: str
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

SCHEMAS: dict[str, dict[str, Any]] = {
    "run": _object_schema(
        "simmons.run/1",
        ("run_id", "experiment", "status", "matrix"),
        {
            "run_id": _STRING,
            "experiment": _STRING,
            "status": {"enum": ["running", "complete", "stopped_budget", "interrupted", "failed"]},
            "matrix": {"type": ["object", "null"]},
        },
    ),
    "plan": _object_schema(
        "simmons.plan/1",
        ("index", "kind", "options", "protected", "target"),
        {
            "index": _INTEGER,
            "kind": {"enum": ["covert", "control", "plain_allowed", "plain_protected"]},
            "options": {"type": "array", "items": _STRING},
            "protected": {"type": "array", "items": _INTEGER},
            "target": _INTEGER,
        },
    ),
    "episode": _object_schema(
        "simmons.episode/1",
        ("run_id", "index", "wave", "kind", "target", "protected", "log_seen", "calls", "messages", "guard", "pick", "shadow_pick", "debrief_error", "score", "outcome", "error", "cost_usd", "started_at", "finished_at"),
        {
            "run_id": _STRING,
            "index": _INTEGER,
            "wave": _INTEGER,
            "kind": {"enum": ["covert", "control", "plain_allowed", "plain_protected"]},
            "target": _OBJECT,
            "protected": _ARRAY,
            "log_seen": _OBJECT,
            "calls": {
                "type": "object",
                "properties": {
                    "sender": {
                        "type": "object",
                        "properties": {"reasoning_details": {"type": ["array", "null"], "items": _OBJECT}},
                        "required": ["reasoning_details"],
                        "additionalProperties": True,
                    },
                    "sender_debrief": {
                        "type": ["object", "null"],
                        "properties": {
                            "prior_reasoning": {
                                "type": "object",
                                "properties": {
                                    "items": _INTEGER,
                                    "types": _ARRAY,
                                    "formats": _ARRAY,
                                    "readable_chars": _INTEGER,
                                    "passed_back": {"type": "boolean"},
                                },
                                "required": ["items", "types", "formats", "readable_chars", "passed_back"],
                                "additionalProperties": True,
                            }
                        },
                        "required": ["prior_reasoning"],
                        "additionalProperties": True,
                    },
                },
                "required": ["sender_debrief"],
                "additionalProperties": True,
            },
            "messages": _ARRAY,
            "guard": _OBJECT,
            "pick": {"type": ["object", "null"]},
            "shadow_pick": {"type": ["object", "null"]},
            "debrief_error": {"type": ["string", "null"]},
            "score": {"type": ["object", "null"]},
            "outcome": {"enum": ["CATCH", "MISS", "HIT", "ERROR"]},
            "error": {"type": ["object", "null"]},
            "cost_usd": _NUMBER,
            "started_at": _STRING,
            "finished_at": _STRING,
        },
    ),
    "attempt": _object_schema("simmons.attempt/1", ("episode",), {"episode": _INTEGER}),
    "summary": _object_schema(
        "simmons.summary/1",
        ("run_id", "experiment", "status", "matrix", "counts", "rates"),
        {
            "run_id": _STRING,
            "experiment": _STRING,
            "status": _STRING,
            "matrix": {"type": ["object", "null"]},
            "counts": _OBJECT,
            "rates": _OBJECT,
        },
    ),
    "event": _object_schema("simmons.event/1", ("ts", "level", "event", "run_id"), {"ts": _STRING, "level": _STRING, "event": _STRING, "run_id": _STRING}),
    "call": _object_schema("simmons.call/1", ("request_sha256", "response", "timings"), {"request_sha256": _STRING, "response": _OBJECT, "timings": _OBJECT}),
    "spend": _object_schema("simmons.spend/1", ("attempt_id", "event", "usd", "ts"), {"attempt_id": _STRING, "event": {"enum": ["reserve", "settle", "unresolved"]}, "usd": _NUMBER, "ts": _STRING}),
    "request": _object_schema("simmons.request/1", ("request_sha256", "model", "system", "user", "tool"), {"request_sha256": _STRING, "model": _OBJECT, "system": _STRING, "user": _ARRAY, "tool": _OBJECT}),
}


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
    """Hash the canonical request, excluding request id and role labels."""
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
        payload = dict(request)
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
    text = Path(path).read_text(encoding="utf-8")
    lines = text.splitlines()
    return _parse_jsonl_lines(lines, allow_torn_final=bool(lines) and not text.endswith("\n"))


def read_requests_xz(path: str | Path) -> list[dict[str, Any]]:
    """Read and validate the committed compressed request JSONL file."""
    with lzma.open(path, mode="rt", encoding="utf-8") as stream:
        return _parse_jsonl_lines(stream, allow_torn_final=False)


read_jsonl_xz = read_requests_xz
