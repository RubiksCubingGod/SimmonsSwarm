"""Tool names and field lists frozen for the role implementation in T02c."""

from __future__ import annotations

from typing import Any

SEND_MESSAGE_TOOL_NAME = "send_message"
SEND_MESSAGE_FIELDS = ("message",)
SEND_MESSAGE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"message": {"type": "string", "minLength": 1, "maxLength": 2000}},
    "required": ["message"],
    "additionalProperties": False,
}

EXPLAIN_MESSAGE_TOOL_NAME = "explain_message"
EXPLAIN_MESSAGE_FIELDS = ("scheme", "encoding", "expected_decode", "confidence")
EXPLAIN_MESSAGE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scheme": {"type": "string", "minLength": 1, "maxLength": 200},
        "encoding": {"type": "string", "minLength": 1, "maxLength": 3000},
        "expected_decode": {"type": "string", "maxLength": 1500},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": list(EXPLAIN_MESSAGE_FIELDS),
    "additionalProperties": False,
}

