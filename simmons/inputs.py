"""Load experiment inputs and snapshot their exact source bytes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from .config import Config
from .records import canonical_json
from .template_tags import SENDER_DEBRIEF_TEMPLATE_NAME
from .text import normalize
from .types import Inputs, OptionSet

_PROMPTS = {
    "sender_prompt": "prompts/sender.md",
    "guard_prompt": "prompts/guard.md",
    "plain_sender_prompt": "prompts/plain_sender.md",
}
_TEMPLATES = {
    "sender": "sender.md",
    "plain_sender": "plain_sender.md",
    "guard": "guard.md",
    "receiver": "receiver.md",
}


@dataclass(frozen=True)
class _LoadedInputs(Inputs):
    """Inputs plus private byte copies needed by the frozen snapshot seam."""

    source_files: Mapping[str, bytes]


def _path(value: str, root: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _option_set_path(value: str, root: Path) -> Path:
    if "/" in value:
        return _path(value, root)
    return root / "option-sets" / f"{value}.json"


def _read_utf8(path: Path, label: str) -> tuple[bytes, str]:
    raw = path.read_bytes()
    try:
        return raw, raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label}: must be UTF-8") from exc


def _load_prompt(path: Path, label: str, *, required: bool) -> tuple[bytes, str]:
    raw, content = _read_utf8(path, label)
    if len(raw) > 64 * 1024:
        raise ValueError(f"{label}: must be at most 64 KB")
    if required and not content.strip():
        raise ValueError(f"{label}: must be non-empty")
    return raw, content


def _validate_options(value: object, options_per_episode: int | None) -> OptionSet:
    if isinstance(value, dict):
        raise ValueError("option_set: object-form support lands in v1.1")
    if not isinstance(value, list):
        raise ValueError("option_set: must be an array of strings")

    options: list[str] = []
    normalized: dict[str, int] = {}
    newline_chars = "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029"
    for index, option in enumerate(value):
        if not isinstance(option, str) or not option or len(option) > 200:
            raise ValueError(f"option_set[{index}]: must be a non-empty string of at most 200 characters")
        if any(char in option for char in newline_chars):
            raise ValueError(f"option_set[{index}]: must not contain a newline")
        key = normalize(option)
        if not key:
            raise ValueError(f"option_set[{index}]: must not be empty after normalization")
        if key in normalized:
            previous = normalized[key]
            raise ValueError(
                f"option_set[{index}]: collides after normalization with option_set[{previous}]"
            )
        normalized[key] = index
        options.append(option)

    resolved_count = len(options) if options_per_episode is None else options_per_episode
    if resolved_count < 2:
        raise ValueError("options_per_episode: must be at least 2")
    if resolved_count > len(options):
        raise ValueError("options_per_episode: exceeds the option_set size")
    return OptionSet(options=tuple(options), clusters=MappingProxyType({}))


def load(config: Config) -> Inputs:
    """Load v1 array option sets and the configured prompt/task/templates."""
    root = Path.cwd()
    option_path = _option_set_path(config.option_set, root)
    option_bytes, option_text = _read_utf8(option_path, "option_set")
    try:
        option_value = json.loads(option_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"option_set: invalid JSON: {exc.msg}") from exc
    option_set = _validate_options(option_value, config.options_per_episode)

    source_files: dict[str, bytes] = {"option-set.json": option_bytes}
    prompt_values: dict[str, str] = {}
    for field, snapshot_name in _PROMPTS.items():
        raw, content = _load_prompt(
            _path(getattr(config, field), root),
            field,
            required=field == "plain_sender_prompt",
        )
        source_files[snapshot_name] = raw
        prompt_values[field] = content

    cover_bytes, cover_task = _read_utf8(_path(config.cover_task, root), "cover_task")
    if not cover_task.strip():
        raise ValueError("cover_task: must be non-empty")
    source_files["cover-task.md"] = cover_bytes

    templates_dir = _path(config.templates, root)
    if not templates_dir.is_dir():
        raise FileNotFoundError(f"templates directory does not exist: {templates_dir}")
    templates: dict[str, str] = {}
    for role, filename in _TEMPLATES.items():
        path = templates_dir / filename
        raw, content = _read_utf8(path, f"templates.{role}")
        templates[role] = content
        source_files[f"templates/{filename}"] = raw

    judge_path = templates_dir / "judge.md"
    if judge_path.is_file():
        raw, content = _read_utf8(judge_path, "templates.judge")
        templates["judge"] = content
        source_files["templates/judge.md"] = raw

    if config.sender_debrief != "off":
        debrief_path = templates_dir / SENDER_DEBRIEF_TEMPLATE_NAME
        raw, content = _read_utf8(debrief_path, "templates.sender_debrief")
        templates[SENDER_DEBRIEF_TEMPLATE_NAME] = content
        source_files[f"templates/{SENDER_DEBRIEF_TEMPLATE_NAME}"] = raw

    for path in sorted(path for path in templates_dir.rglob("*") if path.is_file()):
        relative_path = path.relative_to(templates_dir)
        if any(
            part.startswith(".") or part.endswith("~")
            for part in relative_path.parts
        ):
            continue
        relative = relative_path.as_posix()
        source_files[f"templates/{relative}"] = path.read_bytes()

    return _LoadedInputs(
        option_set=option_set,
        sender_prompt=prompt_values["sender_prompt"],
        guard_prompt=prompt_values["guard_prompt"],
        plain_sender_prompt=prompt_values["plain_sender_prompt"],
        cover_task=cover_task,
        templates=MappingProxyType(templates),
        source_files=MappingProxyType(source_files),
    )


def snapshot(inputs: Inputs, destination: str | Path) -> Path:
    """Write loaded input files byte-for-byte into a run's ``inputs/``."""
    if isinstance(inputs, _LoadedInputs):
        files = inputs.source_files
    else:
        # Preserve support for callers that construct the frozen Inputs type
        # directly; loaded inputs use the exact source bytes above.
        files = {
            "option-set.json": (canonical_json(list(inputs.option_set.options)) + "\n").encode("utf-8"),
            "prompts/sender.md": inputs.sender_prompt.encode("utf-8"),
            "prompts/guard.md": inputs.guard_prompt.encode("utf-8"),
            "prompts/plain_sender.md": inputs.plain_sender_prompt.encode("utf-8"),
            "cover-task.md": inputs.cover_task.encode("utf-8"),
            **{
                f"templates/{role}.md": content.encode("utf-8")
                for role, content in inputs.templates.items()
            },
        }

    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


__all__ = ["load", "snapshot"]
