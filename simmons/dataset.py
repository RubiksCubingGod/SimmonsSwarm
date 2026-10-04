"""Read completed run records into the run-level dataset."""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import ValidationError as SchemaValidationError
from pydantic import ValidationError

from . import metrics
from .config import Config, PROTOCOL_VERSION, resolve_models
from .records import canonical_json, read_jsonl

_BASE_COLUMNS = (
    "run_id",
    "experiment",
    "author",
    "status",
    "source",
    "started_at",
    "episodes_done",
    "cost_usd",
)
_ID_COLUMNS = ("id.config", "id.arena", "id.sender", "id.guard", "id.plan")
_LABEL_COLUMNS = ("label.sender", "label.guard", "label.cell")
_PATH_FIELDS = {
    "option_set": "option-set.json",
    "sender_prompt": "prompts/sender.md",
    "guard_prompt": "prompts/guard.md",
    "plain_sender_prompt": "prompts/plain_sender.md",
    "cover_task": "cover-task.md",
}
_KINDS = ("covert", "control", "plain_allowed", "plain_protected")
_OUTCOMES = ("CATCH", "HIT", "MISS", "ERROR")


@dataclass
class RunData:
    """One validated run plus its records and dataset projection."""

    run_dir: Path
    run: dict[str, Any]
    config: dict[str, Any]
    episodes: list[dict[str, Any]]
    summary: dict[str, Any]
    row: dict[str, Any]
    source_root: Path


def default_roots(
    runs_dirs: Iterable[Path] | None = None,
    *,
    include_local: bool = False,
    repo_root: Path | None = None,
) -> list[Path]:
    """Resolve the run roots shared by dataset, pivot, and grid."""
    root = (repo_root or Path.cwd()).resolve()
    roots = [Path(path).expanduser().resolve() for path in runs_dirs or ()]
    if not roots:
        roots = [root / "runs"]
    if include_local:
        local = root / "runs-local"
        if local not in roots:
            roots.append(local)
    return roots


def scan_runs(
    roots: Iterable[Path],
    *,
    include_partial: bool = False,
    include_fake: bool = False,
    where: Iterable[str] = (),
) -> tuple[list[RunData], list[dict[str, str]]]:
    """Load matching run directories, de-duplicating by ``run_id``."""
    selected: list[RunData] = []
    skipped: list[dict[str, str]] = []
    seen: set[str] = set()
    expressions = tuple(_parse_where(item) for item in where)

    for root in roots:
        if not root.is_dir():
            continue
        for run_path in _run_json_paths(root):
            run_dir = run_path.parent
            try:
                item = _load_run(run_dir, root)
            except (
                OSError,
                UnicodeError,
                json.JSONDecodeError,
                ValueError,
                ValidationError,
                SchemaValidationError,
            ) as exc:
                skipped.append({"run_dir": str(run_dir), "reason": str(exc)})
                continue

            run_id = str(item.run.get("run_id", ""))
            if not run_id:
                skipped.append({"run_dir": str(run_dir), "reason": "run.json has no run_id"})
                continue
            if run_id in seen:
                skipped.append({"run_dir": str(run_dir), "reason": f"duplicate run_id {run_id}"})
                continue
            seen.add(run_id)

            status = item.run.get("status")
            allowed_statuses = {"complete"}
            if include_partial:
                allowed_statuses.update({"stopped_budget", "interrupted"})
            if status not in allowed_statuses:
                skipped.append({"run_dir": str(run_dir), "reason": f"status {status!r} is not selected"})
                continue
            if not include_fake and _uses_fake(item.config):
                skipped.append({"run_dir": str(run_dir), "reason": "fake provider excluded"})
                continue
            if not all(_matches_where(item.row, field, value) for field, value in expressions):
                skipped.append({"run_dir": str(run_dir), "reason": "does not match --where"})
                continue
            selected.append(item)

    selected.sort(key=lambda item: (str(item.row.get("experiment", "")), str(item.row["run_id"])))
    return selected, skipped


def _run_json_paths(root: Path) -> list[Path]:
    if (root / "run.json").is_file():
        return [root / "run.json"]
    return sorted(
        path
        for path in root.rglob("run.json")
        if path.is_file() and ".git" not in path.parts
    )


def _load_run(run_dir: Path, source_root: Path) -> RunData:
    run = _read_object(run_dir / "run.json")
    raw_config = _read_object(run_dir / "config.json")
    config_model = Config.model_validate(
        {key: value for key, value in raw_config.items() if key in Config.model_fields}
    )
    config = config_model.model_dump(mode="python")
    config["options_per_episode"] = _resolved_option_count(
        run_dir, config.get("options_per_episode")
    )
    config_json = config_model.model_dump(mode="json")
    config_json["options_per_episode"] = config["options_per_episode"]

    episode_path = run_dir / "episodes.jsonl"
    episode_bytes = episode_path.read_bytes()
    episodes = read_jsonl(episode_path)
    try:
        summary = _read_object(run_dir / "summary.json")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        summary = {}
    protocol = run.get("protocol_version", raw_config.get("protocol_version", PROTOCOL_VERSION))
    if not _summary_is_fresh(summary, episode_bytes, protocol):
        summary = _recompute_summary(run, config_json, episodes, episode_bytes, protocol)

    resolved_models = resolve_models(config_model)
    row: dict[str, Any] = {
        "run_id": run.get("run_id"),
        "experiment": run.get("experiment", config_model.name),
        "author": run.get("author", ""),
        "status": run.get("status", ""),
        "source": _source_name(source_root),
        "started_at": run.get("started_at", ""),
        "episodes_done": int(run.get("episodes_done", len(episodes))),
        "cost_usd": _number(run.get("spent_usd", summary.get("cost", {}).get("usd"))),
        "id.config": _id(run, "config"),
        "id.arena": _id(run, "arena"),
        "id.sender": _id(run, "sender"),
        "id.guard": _id(run, "guard"),
        "id.plan": _id(run, "plan"),
        "label.sender": _role_label(config_model.sender_prompt, resolved_models.get("sender")),
        "label.guard": _role_label(config_model.guard_prompt, resolved_models.get("guard")),
        "label.cell": "",
    }
    row.update(_flatten_config(config_model, config, resolved_models, run_dir))
    row.update(_flatten_summary(summary))
    return RunData(run_dir, run, config, episodes, summary, row, source_root)


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def _summary_is_fresh(
    summary: Mapping[str, Any], episode_bytes: bytes, protocol_version: Any
) -> bool:
    source = summary.get("source")
    return (
        summary.get("schema") == "simmons.summary/1"
        and isinstance(summary.get("counts"), Mapping)
        and isinstance(summary.get("rates"), Mapping)
        and isinstance(source, Mapping)
        and source.get("episodes_sha256") == hashlib.sha256(episode_bytes).hexdigest()
        and source.get("protocol_version") == protocol_version
    )


def _recompute_summary(
    run: Mapping[str, Any],
    config: Mapping[str, Any],
    episodes: list[dict[str, Any]],
    episode_bytes: bytes,
    protocol_version: Any,
) -> dict[str, Any]:
    result = metrics.summarize_with_config(episodes, config=config)
    result["counts"]["planned"] = int(
        run.get("episodes_planned", config.get("episodes", len(episodes)))
    )
    result["counts"]["done"] = len(episodes)
    return {
        "schema": "simmons.summary/1",
        "run_id": run.get("run_id"),
        "experiment": run.get("experiment", config.get("name", "")),
        "status": run.get("status", ""),
        "ids": run.get("ids", {}),
        "matrix": run.get("matrix"),
        "source": {
            "episodes_sha256": hashlib.sha256(episode_bytes).hexdigest(),
            "protocol_version": protocol_version,
        },
        **result,
    }


def _resolved_option_count(run_dir: Path, configured: Any) -> Any:
    if configured is not None:
        return configured
    path = run_dir / "inputs" / "option-set.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    options = value.get("options") if isinstance(value, Mapping) else value
    return len(options) if isinstance(options, list) else None


def _uses_fake(config_raw: Mapping[str, Any]) -> bool:
    config = Config.model_validate(
        {key: value for key, value in config_raw.items() if key in Config.model_fields}
    )
    return any(
        model is not None and model.provider == "fake"
        for model in resolve_models(config).values()
    )


def _source_name(root: Path) -> str:
    return "runs-local" if root.name == "runs-local" else "runs"


def _id(run: Mapping[str, Any], name: str) -> Any:
    ids = run.get("ids")
    return ids.get(name) if isinstance(ids, Mapping) else None


def _role_label(prompt: str, model: Any) -> str:
    prompt_label = Path(prompt).stem if prompt else "unknown"
    if model is None:
        return prompt_label
    return f"{prompt_label} · {model.model.rsplit('/', 1)[-1]}"


def _flatten_config(
    config_model: Config,
    resolved_config: Mapping[str, Any],
    resolved_models: Mapping[str, Any],
    run_dir: Path,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    raw = config_model.model_dump(mode="python")
    for name, value in raw.items():
        if name == "models":
            continue
        if name in _PATH_FIELDS:
            result[f"cfg.{name}"] = value
            result[f"cfg.{name}.sha"] = _snapshot_hash(run_dir, _PATH_FIELDS[name])
        elif name == "templates":
            result[f"cfg.{name}"] = value
            result[f"cfg.{name}.sha"] = _templates_snapshot_hash(run_dir)
        elif isinstance(value, Mapping):
            _flatten_value(result, f"cfg.{name}", value)
        elif isinstance(value, (list, tuple)):
            result[f"cfg.{name}"] = "|".join(_scalar_string(item) for item in value)
        else:
            result[f"cfg.{name}"] = value

    for role in ("sender", "receiver", "guard", "plain_sender", "judge"):
        model = resolved_models.get(role)
        prefix = f"cfg.models.{role}"
        result[f"{prefix}.model"] = (
            f"{model.provider}:{model.model}" if model is not None else None
        )
        result[f"{prefix}.temperature"] = model.temperature if model is not None else None
        result[f"{prefix}.max_tokens"] = model.max_tokens if model is not None else None
        result[f"{prefix}.params"] = (
            canonical_json(dict(model.params)) if model is not None else None
        )
    result["cfg.options_per_episode"] = resolved_config.get("options_per_episode")
    return result


def _flatten_value(result: dict[str, Any], prefix: str, value: Mapping[str, Any]) -> None:
    for key, item in value.items():
        path = f"{prefix}.{key}"
        if isinstance(item, Mapping):
            _flatten_value(result, path, item)
        elif isinstance(item, (list, tuple)):
            result[path] = "|".join(_scalar_string(part) for part in item)
        elif item is None or isinstance(item, (str, int, float, bool)):
            result[path] = item
        else:
            result[path] = canonical_json(item)


def _snapshot_hash(run_dir: Path, snapshot_name: str) -> str | None:
    path = run_dir / "inputs" / snapshot_name
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:8] if path.is_file() else None
    except OSError:
        return None


def _templates_snapshot_hash(run_dir: Path) -> str | None:
    root = run_dir / "inputs" / "templates"
    if not root.is_dir():
        return None
    pairs: list[tuple[str, str]] = []
    files: list[tuple[str, Path]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part.startswith(".") or part.endswith("~") for part in relative.parts):
            continue
        files.append((relative.as_posix(), path))
    for relative_name, path in sorted(files):
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None
        pairs.append((relative_name, digest))
    return hashlib.sha256(canonical_json(pairs).encode("utf-8")).hexdigest()[:8]


def _flatten_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    rates = summary.get("rates")
    if isinstance(rates, Mapping):
        for name, value in rates.items():
            if not isinstance(value, Mapping):
                continue
            _add_rate(result, str(name), value)
    decode = summary.get("decode")
    if isinstance(decode, Mapping):
        for source, target in (("receiver", "receiver_decode"), ("guard", "guard_decode")):
            value = decode.get(source)
            if isinstance(value, Mapping):
                _add_rate(result, target, value)
        result["decode_gap"] = decode.get("gap")
        receiver_rate = decode.get("receiver")
        result["decode_gap.den"] = (
            receiver_rate.get("den") if isinstance(receiver_rate, Mapping) else None
        )
        result["decode_gap_all"] = decode.get("gap_all_covert")
        all_rate = decode.get("receiver_all_covert")
        result["decode_gap_all.den"] = all_rate.get("den") if isinstance(all_rate, Mapping) else None
        chance = decode.get("chance")
        if isinstance(chance, Mapping):
            result["receiver_chance"] = chance.get("receiver")
            result["guard_chance"] = chance.get("guard")
    curve = summary.get("curve")
    result["auc"] = curve.get("auc") if isinstance(curve, Mapping) else None
    counts = summary.get("counts")
    by_kind = counts.get("by_kind") if isinstance(counts, Mapping) else None
    for kind in _KINDS:
        values = by_kind.get(kind, {}) if isinstance(by_kind, Mapping) else {}
        result[f"n.{kind}"] = values.get("n", 0) if isinstance(values, Mapping) else 0
        for outcome in _OUTCOMES:
            result[f"n.{kind}.{outcome}"] = (
                values.get(outcome, 0) if isinstance(values, Mapping) else 0
            )
    return result


def _add_rate(result: dict[str, Any], name: str, value: Mapping[str, Any]) -> None:
    for suffix in ("", ".num", ".den", ".lo", ".hi"):
        key = "value" if not suffix else suffix[1:]
        result[name + suffix] = value.get(key)


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _scalar_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping) or isinstance(value, (list, tuple)):
        return canonical_json(value)
    return str(value)


def _parse_where(expression: str) -> tuple[str, str]:
    if "=" not in expression:
        raise ValueError(f"--where expects field=value, got {expression!r}")
    field, value = expression.split("=", 1)
    if not field:
        raise ValueError(f"--where field is empty: {expression!r}")
    return _normalize_field(field), value


def _normalize_field(field: str) -> str:
    if field in {"sender", "guard", "arena"}:
        return {"sender": "id.sender", "guard": "id.guard", "arena": "id.arena"}[field]
    return field if field.startswith(("cfg.", "id.", "label.", "axis.")) or field == "author" else f"cfg.{field}"


def _matches_where(row: Mapping[str, Any], field: str, expected: str) -> bool:
    value = row.get(field)
    if isinstance(value, (Mapping, list)):
        actual = canonical_json(value)
    elif value is None:
        actual = "null"
    elif isinstance(value, bool):
        actual = "true" if value else "false"
    else:
        actual = str(value)
    return actual == expected


def dataset_columns(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    all_rows = list(rows)
    keys = set().union(*(row.keys() for row in all_rows)) if all_rows else set()
    ordered = [*_BASE_COLUMNS]
    ordered.extend(_ID_COLUMNS)
    ordered.extend(_LABEL_COLUMNS)
    for prefix in ("axis.", "cfg."):
        ordered.extend(sorted(key for key in keys if key.startswith(prefix)))
    ordered.extend(sorted(key for key in keys if key not in set(ordered)))
    return ordered


def write_dataset(
    rows: list[dict[str, Any]],
    output_dir: Path,
    *,
    stdout: bool = False,
) -> tuple[Path | None, Path | None, list[str]]:
    """Write deterministic JSONL and CSV files, or stream JSONL to stdout."""
    columns = dataset_columns(rows)
    if stdout:
        for row in rows:
            print(canonical_json(row))
        return None, None, columns

    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / "dataset.jsonl"
    csv_path = output_dir / "dataset.csv"
    jsonl_text = "".join(canonical_json(row) + "\n" for row in rows)
    jsonl_path.write_text(jsonl_text, encoding="utf-8", newline="\n")
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in columns})
    return jsonl_path, csv_path, columns


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (Mapping, list, tuple)):
        return canonical_json(value)
    return value


__all__ = [
    "RunData",
    "default_roots",
    "dataset_columns",
    "scan_runs",
    "write_dataset",
]
