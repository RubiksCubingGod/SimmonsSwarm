"""Run-level pivots and frame checks derived from the dataset."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from . import metrics
from .dataset import RunData
from .records import canonical_json

DEFAULT_METRICS = ("hit_rate", "false_alarm_rate")
_NON_IDENTITY = {"cfg.name", "cfg.description", "cfg.seed", "cfg.max_cost_usd"}
_PATH_COLUMNS = {
    "cfg.option_set",
    "cfg.sender_prompt",
    "cfg.guard_prompt",
    "cfg.plain_sender_prompt",
    "cfg.cover_task",
    "cfg.templates",
}


def normalize_column(column: str | None) -> str | None:
    if column is None:
        return None
    if column in {"sender", "guard", "arena"}:
        return {"sender": "id.sender", "guard": "id.guard", "arena": "id.arena"}[column]
    return column if column.startswith(("cfg.", "id.", "label.", "axis.")) or column == "author" else f"cfg.{column}"


def build_pivot(
    runs: Iterable[RunData],
    *,
    rows: str,
    cols: str | None = None,
    split: str | None = None,
    selected_metrics: Iterable[str] = DEFAULT_METRICS,
    allow_mixed: bool = False,
) -> dict[str, Any]:
    """Build one or more pivot tables from selected complete run records."""
    selected = list(runs)
    row_field = normalize_column(rows)
    col_field = normalize_column(cols)
    split_field = normalize_column(split)
    assert row_field is not None
    metric_names = tuple(dict.fromkeys(selected_metrics))
    if not metric_names:
        raise ValueError("at least one --metric is required")

    configs = _config_representatives(selected)
    identity = {key: _identity_values(run) for key, run in configs.items()}
    dimensions = _dimension_fields((rows, cols, split), identity)
    held_excluded = _dimension_fields((rows, cols, None), identity)
    varying = _varying_fields(identity, dimensions)

    bucket_configs: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    bucket_runs: dict[tuple[str, str, str], list[RunData]] = defaultdict(list)
    axes: dict[str, dict[str, Any]] = {"rows": {}, "cols": {}}
    split_values: dict[str, Any] = {}
    split_runs: dict[str, RunData] = {}
    for run in selected:
        run_config = str(run.row.get("id.config") or run.run.get("run_id"))
        row_value = run.row.get(row_field)
        col_value = run.row.get(col_field) if col_field is not None else ""
        split_value = run.row.get(split_field) if split_field is not None else ""
        row_key, col_key, split_key = map(_key, (row_value, col_value, split_value))
        bucket = (split_key, row_key, col_key)
        bucket_configs[bucket].add(run_config)
        bucket_runs[bucket].append(run)
        axes["rows"].setdefault(row_key, _axis_value(run, row_value, rows, row_field))
        if col_field is not None:
            axes["cols"].setdefault(col_key, _axis_value(run, col_value, cols, col_field))
        split_values.setdefault(split_key, split_value)
        split_runs.setdefault(split_key, run)

    mixed, confounded, held = _frame(
        varying,
        identity,
        bucket_configs,
        configs,
        held_excluded,
    )
    frame = {"held": held, "confounded": confounded, "mixed": mixed}
    paired = _pairing(selected)

    slices: list[dict[str, Any]] = []
    split_keys = sorted(split_values, key=lambda key: _sort_value(split_values[key]))

    for split_key in split_keys:
        split_buckets = {
            (row_key, col_key): runs_for_bucket
            for (candidate_split, row_key, col_key), runs_for_bucket in bucket_runs.items()
            if candidate_split == split_key
        }
        row_keys = {
            row_key
            for (candidate_split, row_key, _), _bucket in bucket_runs.items()
            if candidate_split == split_key
        }
        col_keys = {
            col_key
            for (candidate_split, _row_key, col_key), _bucket in bucket_runs.items()
            if candidate_split == split_key
        }
        row_axes = _ordered_axes([axes["rows"][key] for key in row_keys], rows)
        col_axes = (
            _ordered_axes([axes["cols"][key] for key in col_keys], cols)
            if col_field is not None
            else [{"value": "", "label": "all"}]
        )
        cell_reports: list[dict[str, Any]] = []
        for row_axis in row_axes:
            for col_axis in col_axes:
                cell_runs = split_buckets.get((_key(row_axis["value"]), _key(col_axis["value"])), [])
                if not cell_runs:
                    continue
                report = _pool_cell(cell_runs, metric_names)
                report.update({"row": row_axis["value"], "col": col_axis["value"]})
                report["mixed"] = allow_mixed and any(
                    entry["bucket"] == [split_key, _key(row_axis["value"]), _key(col_axis["value"])]
                    for item in mixed
                    for entry in item["buckets"]
                )
                cell_reports.append(report)

        slices.append(
            {
                "split": {
                    "field": split_field,
                    "value": split_values[split_key],
                    "label": (
                        _split_label(split_runs[split_key], split_field, split_values[split_key])
                        if split_key in split_runs
                        else "no runs"
                    ),
                }
                if split_field is not None
                else None,
                "rows": row_axes,
                "cols": col_axes,
                "frame": frame,
                "paired": (
                    _pairing([run for run in selected if _key(run.row.get(split_field)) == split_key])
                    if split_field is not None
                    else paired
                ),
                "cells": cell_reports,
            }
        )

    refusal = mixed if mixed and not allow_mixed else []
    return {
        "metric": metric_names[0] if len(metric_names) == 1 else list(metric_names),
        "metrics": list(metric_names),
        "rows_field": row_field,
        "cols_field": col_field,
        "split_field": split_field,
        "n_buckets": len(bucket_configs),
        "frame": frame,
        "paired": paired,
        "slices": slices,
        "refused": refusal,
    }


def render_pivot(report: Mapping[str, Any], *, markdown: bool = False) -> str:
    """Render the shared pivot representation for terminal and Markdown."""
    refusal = report.get("refused", [])
    if refusal:
        mixed_buckets = {
            tuple(entry["bucket"])
            for item in refusal
            for entry in item.get("buckets", [])
        }
        lines = [
            f"pivot refused: {len(mixed_buckets)} of {report.get('n_buckets', 0)} buckets mix cells that differ in a field you did not pivot on"
        ]
        for item in refusal:
            values = ", ".join(_display(value) for value in item.get("values", []))
            lines.append(f"  {item['field']}: {values} ({len(item.get('buckets', []))} buckets)")
        lines.append(
            f"add --where {refusal[0]['field']}=<value>, add --split {refusal[0]['field']}, or pass --allow-mixed"
        )
        return "\n".join(lines) + "\n"

    lines: list[str] = []
    metric_names = report.get("metrics", [report.get("metric", "hit_rate")])
    if not isinstance(metric_names, list):
        metric_names = [metric_names]
    row_field = _short_field(str(report.get("rows_field", "rows")))
    col_raw = report.get("cols_field")
    col_field = _short_field(str(col_raw)) if col_raw else None
    split_field = report.get("split_field")
    title = f"pivot {', '.join(str(name) for name in metric_names)} · rows {row_field}"
    if col_field:
        title += f" · cols {col_field}"
    if split_field:
        title += f" · split {_short_field(str(split_field))}"
    lines.append(title)

    frame = report.get("frame", {})
    held = frame.get("held", []) if isinstance(frame, Mapping) else []
    confounded = frame.get("confounded", []) if isinstance(frame, Mapping) else []
    mixed = frame.get("mixed", []) if isinstance(frame, Mapping) else []
    also = [
        f"{item.get('field')}={', '.join(_display(value) for value in item.get('values', []))}"
        for item in confounded
    ]
    also.extend(
        f"{item.get('field')}={', '.join(_display(value) for value in item.get('values', []))}"
        for item in mixed
        if not any(str(item.get("field")) == value.split("=", 1)[0] for value in also)
    )
    also_text = ", ".join(also) if also else "none"
    split_tables = bool(split_field)
    frame_line = f"held: {len(held)} fields · also varies: {also_text}"
    if not split_tables:
        frame_line += " · " + _pairing_text(report.get("paired", {}))
    lines.append(frame_line)

    if not report.get("slices"):
        lines.append("No runs selected.")

    for table in report.get("slices", []):
        split = table.get("split")
        if isinstance(split, Mapping):
            lines.append("")
            lines.append(f"{split.get('field')}: {split.get('label')}")
            lines.append(_pairing_text(table.get("paired", {})))
        row_axes = table.get("rows", [])
        col_axes = table.get("cols", [])
        cells = {
            (_key(cell.get("row")), _key(cell.get("col"))): cell
            for cell in table.get("cells", [])
        }
        if markdown:
            header = [f"{row_field} ↓"] + [str(axis.get("label", "")) for axis in col_axes]
            lines.append("| " + " | ".join(header) + " |")
            lines.append("| " + " | ".join(["---"] * len(header)) + " |")
            for row_axis in row_axes:
                values = [str(row_axis.get("label", ""))]
                for col_axis in col_axes:
                    cell = cells.get((_key(row_axis.get("value")), _key(col_axis.get("value"))))
                    values.append(_format_cell(cell, metric_names) if cell else "·")
                lines.append("| " + " | ".join(values) + " |")
        else:
            width = max([len(row_field + " ↓"), *(len(str(axis.get("label", ""))) for axis in row_axes)])
            col_widths = [
                max(8, len(str(axis.get("label", ""))), *(
                    len(_format_cell(cells.get((_key(row.get("value")), _key(axis.get("value")))), metric_names))
                    for row in row_axes
                ))
                for axis in col_axes
            ]
            lines.append((row_field + " ↓").ljust(width) + "  " + "  ".join(
                str(axis.get("label", "")).ljust(col_widths[index])
                for index, axis in enumerate(col_axes)
            ))
            for row_axis in row_axes:
                values = []
                for index, col_axis in enumerate(col_axes):
                    cell = cells.get((_key(row_axis.get("value")), _key(col_axis.get("value"))))
                    values.append((_format_cell(cell, metric_names) if cell else "·").ljust(col_widths[index]))
                lines.append(str(row_axis.get("label", "")).ljust(width) + "  " + "  ".join(values))
        lines.append("· slot not filled · n covert episodes · r runs")

    all_cells = [cell for table in report.get("slices", []) for cell in table.get("cells", [])]
    if any(cell.get("multiple_hosts") for cell in all_cells):
        lines.append("† multiple serving hosts")
    if any(cell.get("mixed") for cell in all_cells):
        lines.append("* pooled despite mixed identity fields (--allow-mixed)")

    return "\n".join(lines) + "\n"


def _pairing_text(paired: Any) -> str:
    if not isinstance(paired, Mapping):
        return "plans: no"
    status = paired.get("status", "no")
    seeds = paired.get("seeds", [])
    seed_text = f" (seeds {', '.join(str(seed) for seed in seeds)})" if seeds else ""
    columns = paired.get("columns", [])
    if status == "partial" and columns:
        seed_text += f"; breaks {', '.join(str(column)[:8] for column in columns)}"
    return f"plans: {status}{seed_text}"


def json_payload(report: Mapping[str, Any]) -> dict[str, Any]:
    """Return a stable pivot JSON object, or a split collection."""
    if report.get("refused"):
        return {"error": "mixed", "mixed": report["refused"]}
    if len(report.get("slices", [])) == 1:
        table = report["slices"][0]
        return {
            "metric": report.get("metric"),
            "rows": table.get("rows", []),
            "cols": table.get("cols", []),
            "frame": table.get("frame", {}),
            "paired": table.get("paired", {}),
            "cells": table.get("cells", []),
            "split": table.get("split"),
        }
    return {
        "metric": report.get("metric"),
        "split": report.get("split_field"),
        "pivots": [
            {
                "split": table.get("split"),
                "rows": table.get("rows", []),
                "cols": table.get("cols", []),
                "frame": table.get("frame", {}),
                "paired": table.get("paired", {}),
                "cells": table.get("cells", []),
            }
            for table in report.get("slices", [])
        ],
    }


def _config_representatives(runs: list[RunData]) -> dict[str, RunData]:
    result: dict[str, RunData] = {}
    for run in runs:
        key = str(run.row.get("id.config") or run.run.get("run_id"))
        result.setdefault(key, run)
    return result


def _identity_values(run: RunData) -> dict[str, Any]:
    row, config = run.row, run.config
    values: dict[str, Any] = {}
    has_history = config.get("log_scope") == "run" and bool(
        config.get("log_records") or config.get("receiver_sees_log")
    )
    for key, value in row.items():
        if not key.startswith("cfg."):
            continue
        base = key[:-4] if key.endswith(".sha") else key
        if base in _NON_IDENTITY:
            continue
        field = base.removeprefix("cfg.")
        if field == "episodes" and not has_history:
            continue
        if field == "block_threshold" and config.get("guard_output") != "suspicion":
            continue
        if base in _PATH_COLUMNS:
            if key.endswith(".sha"):
                values[base] = value
            continue
        values[base] = value
    values["cfg.options_per_episode"] = row.get("cfg.options_per_episode")
    return values


def _dimension_fields(
    raw_columns: tuple[str | None, ...],
    identity: Mapping[str, Mapping[str, Any]],
) -> set[str]:
    fields = {normalize_column(column) for column in raw_columns if column}
    fields = {field for field in fields if field is not None}
    all_identity = set().union(*(set(values) for values in identity.values())) if identity else set()
    sender_fields = {
        key for key in all_identity
        if key.startswith("cfg.sender_prompt")
        or key.startswith("cfg.models.sender.")
        or key.startswith("cfg.models.receiver.")
    }
    guard_fields = {
        key for key in all_identity
        if key.startswith("cfg.guard_prompt")
        or key.startswith("cfg.models.guard.")
        or key in {"cfg.guard_output", "cfg.block_threshold", "cfg.guard_guesses_target"}
    }
    for original in raw_columns:
        if original in {"sender", "id.sender"}:
            fields.update(sender_fields)
        elif original in {"guard", "id.guard"}:
            fields.update(guard_fields)
        elif original in {"arena", "id.arena"}:
            fields.update(all_identity - sender_fields - guard_fields)
    for raw in raw_columns:
        field = normalize_column(raw)
        if raw and field and field.startswith("cfg."):
            fields.add(field[:-4] if field.endswith(".sha") else field)
            if field in _PATH_COLUMNS:
                fields.add(field)
    return fields


def _varying_fields(
    identity: Mapping[str, Mapping[str, Any]], designated: set[str]
) -> list[str]:
    keys = set().union(*(set(values) for values in identity.values())) if identity else set()
    return sorted(
        field for field in keys - designated
        if len({_key(values.get(field)) for values in identity.values()}) > 1
    )


def _frame(
    varying: list[str],
    identity: Mapping[str, Mapping[str, Any]],
    bucket_configs: Mapping[tuple[str, str, str], set[str]],
    configs: Mapping[str, RunData],
    held_excluded: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    mixed: list[dict[str, Any]] = []
    confounded: list[dict[str, Any]] = []
    for field in varying:
        bad_buckets: list[dict[str, Any]] = []
        values_all: dict[str, Any] = {}
        for bucket, config_ids in bucket_configs.items():
            values: dict[str, Any] = {}
            for config_id in config_ids:
                value = identity.get(config_id, {}).get(field)
                display_value = (
                    configs[config_id].row.get(field, value)
                    if field in _PATH_COLUMNS and config_id in configs
                    else value
                )
                values[_key(value)] = display_value
            for value in values.values():
                values_all[_key(value)] = value
            if len(values) > 1:
                bad_buckets.append({"bucket": list(bucket), "configs": sorted(config_ids)})
        public_values = [_public_value(value) for _, value in sorted(values_all.items())]
        if bad_buckets:
            mixed.append({"field": field, "values": public_values, "buckets": bad_buckets})
        else:
            confounded.append({"field": field, "values": public_values})

    all_keys = set().union(*(set(values) for values in identity.values())) if identity else set()
    configs_by_split: dict[str, set[str]] = defaultdict(set)
    for (split_key, _row_key, _col_key), config_ids in bucket_configs.items():
        configs_by_split[split_key].update(config_ids)
    held: list[str] = []
    for field in sorted(all_keys - held_excluded):
        if all(
            len({_key(identity.get(config_id, {}).get(field)) for config_id in config_ids}) <= 1
            for config_ids in configs_by_split.values()
        ):
            held.append(field)
    return mixed, confounded, held


def _pairing(runs: list[RunData]) -> dict[str, Any]:
    by_seed: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    missing_plans: dict[str, set[str]] = defaultdict(set)
    config_ids: set[str] = set()
    seed_values: dict[str, Any] = {}
    for run in runs:
        config_id = str(run.row.get("id.config") or run.run.get("run_id"))
        config_ids.add(config_id)
        seed = run.config.get("seed")
        seed_key = _key(seed)
        seed_values.setdefault(seed_key, seed)
        plan = run.row.get("id.plan")
        plans = by_seed[seed_key][config_id]
        if plan is not None:
            plans.add(str(plan))
        else:
            missing_plans[seed_key].add(config_id)
    shared: list[Any] = []
    broken: list[Any] = []
    broken_columns: set[str] = set()
    for seed_key, by_config in by_seed.items():
        hashes = set().union(*by_config.values()) if by_config else set()
        if (
            len(by_config) >= 2
            and len(hashes) == 1
            and all(len(items) == 1 for items in by_config.values())
            and not missing_plans.get(seed_key)
        ):
            shared.append(seed_values[seed_key])
        else:
            broken.append(seed_values[seed_key])
            expected = (
                max(
                    hashes,
                    key=lambda digest: sum(digest in items for items in by_config.values()),
                )
                if hashes
                else None
            )
            broken_columns.update(
                config_id
                for config_id, items in by_config.items()
                if len(items) != 1 or expected not in items
            )
            broken_columns.update(missing_plans.get(seed_key, set()))
            if len(by_config) < len(config_ids):
                broken_columns.update(config_ids - set(by_config))
    status = "paired" if shared and not broken else "partial" if shared else "no"
    return {
        "status": status,
        "seeds": sorted(shared, key=_sort_value),
        "unpaired_seeds": sorted(broken, key=_sort_value),
        "columns": sorted(broken_columns),
        "configs": len(config_ids),
    }


def _pool_cell(runs: list[RunData], metric_names: tuple[str, ...]) -> dict[str, Any]:
    episodes: list[dict[str, Any]] = []
    offset = 0
    for run in sorted(runs, key=lambda item: str(item.run.get("run_id", ""))):
        for episode in run.episodes:
            copied = dict(episode)
            index = copied.get("index")
            if isinstance(index, int) and not isinstance(index, bool):
                copied["index"] = offset + index
            episodes.append(copied)
        offset += max(
            (episode.get("index", -1) + 1 for episode in run.episodes if isinstance(episode.get("index"), int)),
            default=0,
        )
    summary = metrics.summarize_with_config(episodes, config=runs[0].config)
    decode = summary.get("decode", {})
    guess_modes = {_guard_guesses_target(run) for run in runs}
    if len(guess_modes) > 1:
        # A gap is only meaningful when every run applies the same guard
        # decode policy. Do not inherit that policy from whichever run is first.
        decode["guard"] = _empty_rate()
        decode["guard_all_covert"] = _empty_rate()
        decode["gap"] = None
        decode["gap_all_covert"] = None
        decode["named_catches"] = None
    if len({_shadow_decode_enabled(run) for run in runs}) > 1:
        decode.pop("receiver_all_covert", None)
        decode["gap_all_covert"] = None
    if len({_chance_signature(run, "receiver") for run in runs}) > 1:
        decode.setdefault("chance", {})["receiver"] = None
    if len({_chance_signature(run, "guard") for run in runs}) > 1:
        decode.setdefault("chance", {})["guard"] = None
    result: dict[str, Any] = {
        "configs": sorted({str(run.row.get("id.config")) for run in runs}),
        "n_runs": len(runs),
        "n_covert": summary.get("counts", {}).get("by_kind", {}).get("covert", {}).get("n", 0),
        "seeds": sorted({run.config.get("seed") for run in runs}, key=_sort_value),
        "authors": sorted({str(run.row.get("author", "")) for run in runs if run.row.get("author")}),
        "run_ids": sorted({str(run.row.get("run_id", "")) for run in runs}),
        "hosts": sorted(_serving_hosts(runs)),
        "multiple_hosts": _uses_multiple_hosts(runs),
    }
    rates = summary.get("rates", {})
    for name in metric_names:
        if name in rates:
            result[name] = rates[name]
        elif name == "receiver_decode":
            result[name] = decode.get("receiver", _empty_rate())
        elif name == "guard_decode":
            result[name] = decode.get("guard", _empty_rate())
        elif name in {"decode_gap", "decode_gap_all"}:
            rate_name = "receiver" if name == "decode_gap" else "receiver_all_covert"
            receiver = decode.get(rate_name, {})
            result[name] = {
                "value": decode.get("gap" if name == "decode_gap" else "gap_all_covert"),
                "n": receiver.get("den") if isinstance(receiver, Mapping) else None,
            }
        elif name in {"receiver_chance", "guard_chance"}:
            role = "receiver" if name == "receiver_chance" else "guard"
            chance = decode.get("chance", {})
            result[name] = {"value": chance.get(role)} if isinstance(chance, Mapping) else {"value": None}
        elif name == "auc":
            curve = summary.get("curve", {})
            result[name] = {"value": curve.get("auc"), "n": result["n_covert"]}
        else:
            raise ValueError(f"unknown metric {name!r}")
    return result


def _serving_hosts(runs: list[RunData]) -> set[str]:
    hosts: set[str] = set()
    for run in runs:
        for episode in run.episodes:
            calls = episode.get("calls")
            if not isinstance(calls, Mapping):
                continue
            for call in calls.values():
                if isinstance(call, Mapping) and isinstance(call.get("served_by"), str):
                    hosts.add(call["served_by"])
    return hosts


def _uses_multiple_hosts(runs: list[RunData]) -> bool:
    hosts_by_model: dict[tuple[str, str], set[str]] = defaultdict(set)
    for run in runs:
        for episode in run.episodes:
            calls = episode.get("calls")
            if not isinstance(calls, Mapping):
                continue
            for call_name, call in calls.items():
                if not isinstance(call, Mapping) or not isinstance(call.get("served_by"), str):
                    continue
                role = _model_role(str(call_name))
                model = run.row.get(f"cfg.models.{role}.model")
                model = model if isinstance(model, str) else call.get("model")
                if not isinstance(model, str) or _role_is_host_pinned(run, role):
                    continue
                hosts_by_model[(role, model)].add(call["served_by"])
    return any(len(hosts) > 1 for hosts in hosts_by_model.values())


def _model_role(call_name: str) -> str:
    return {"sender_debrief": "sender", "shadow_receiver": "receiver"}.get(call_name, call_name)


def _role_is_host_pinned(run: RunData, role: str) -> bool:
    raw = run.row.get(f"cfg.models.{role}.params")
    try:
        params = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return False
    if not isinstance(params, Mapping):
        return False
    provider = params.get("provider")
    if not isinstance(provider, Mapping):
        return False
    allowed = provider.get("only")
    if allowed is None and provider.get("allow_fallbacks") is False:
        allowed = provider.get("order")
    return isinstance(allowed, list) and len(allowed) == 1


def _guard_guesses_target(run: RunData) -> bool:
    return bool(run.config.get("guard_guesses_target", True))


def _shadow_decode_enabled(run: RunData) -> bool:
    if "shadow_decode" in run.config:
        return bool(run.config["shadow_decode"])
    return any(
        episode.get("kind") == "covert"
        and (isinstance(episode.get("shadow_pick"), Mapping) or episode.get("shadow_error") is not None)
        for episode in run.episodes
    )


def _chance_signature(run: RunData, role: str) -> tuple[Any, ...]:
    config = run.config
    common = (config.get("options_per_episode"), config.get("protected_count"))
    return (*common, config.get("receiver_knows_protected")) if role == "receiver" else common


def _empty_rate() -> dict[str, Any]:
    return {"value": None, "num": 0, "den": 0, "lo": None, "hi": None}


def _axis_value(run: RunData, value: Any, raw_column: str | None, field: str) -> dict[str, Any]:
    label_field = {"id.sender": "label.sender", "id.guard": "label.guard"}.get(field)
    label = run.row.get(label_field) if label_field else None
    return {"value": value, "label": str(label) if label else _display(value)}


def _split_label(run: RunData, field: str | None, value: Any) -> str:
    if field != "id.arena":
        return _display(value)
    row = run.row
    option_set = str(row.get("cfg.option_set") or "unknown")
    cover_task = str(row.get("cfg.cover_task") or "unknown")
    option_set = option_set.rsplit("/", 1)[-1].removesuffix(".json")
    cover_task = cover_task.rsplit("/", 1)[-1].removesuffix(".md")
    count = row.get("cfg.options_per_episode")
    protected = row.get("cfg.protected_count")
    watchlist = "no watchlist" if protected is None else f"watchlist {protected}"
    options = f" · {count} options" if count is not None else ""
    return f"{value} · {option_set} · {cover_task}{options}, {watchlist}"


def _ordered_axes(axes: list[dict[str, Any]], raw_column: str | None) -> list[dict[str, Any]]:
    labels: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for axis in axes:
        labels[str(axis["label"])].append(axis)
    for label, duplicates in labels.items():
        if len(duplicates) > 1:
            for axis in duplicates:
                suffix = str(axis["value"])
                axis["label"] = f"{label} @{suffix[:6]}"
    return sorted(
        axes,
        key=lambda axis: (
            0 if str(axis.get("label", "")).lower().startswith("reference") else 1,
            str(axis.get("label", "")).lower(),
            _sort_value(axis.get("value")),
        ),
    )


def _format_cell(cell: Mapping[str, Any] | None, metric_names: list[str]) -> str:
    if cell is None:
        return "·"
    if metric_names == ["hit_rate", "false_alarm_rate"]:
        hit = _format_metric(cell.get("hit_rate"))
        false_alarm = _format_metric(cell.get("false_alarm_rate"), short=True)
        suffix = "†" if cell.get("multiple_hosts") else ""
        mixed = "*" if cell.get("mixed") else ""
        return f"{hit}  FA {false_alarm}  (n={cell.get('n_covert', 0)}, r={cell.get('n_runs', 0)}){suffix}{mixed}"
    return "; ".join(f"{name}={_format_metric(cell.get(name))}" for name in metric_names)


def _format_metric(value: Any, *, short: bool = False) -> str:
    if not isinstance(value, Mapping):
        return "—"
    number = value.get("value")
    if number is None:
        return "—"
    if "lo" in value and "hi" in value and value.get("lo") is not None:
        rendered = f"{round(float(number) * 100)}%"
        if not short:
            rendered += f" [{round(float(value['lo']) * 100)}–{round(float(value['hi']) * 100)}]"
        return rendered
    return f"{float(number):.3f}"


def _short_field(field: str) -> str:
    if field == "id.sender":
        return "sender"
    if field == "id.guard":
        return "guard"
    return field.removeprefix("id.")


def _key(value: Any) -> str:
    return canonical_json(value)


def _public_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return canonical_json(value)


def _display(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _sort_value(value: Any) -> tuple[int, Any]:
    if value is None:
        return (0, "")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (1, float(value))
    return (2, str(value).lower())


__all__ = ["DEFAULT_METRICS", "build_pivot", "json_payload", "normalize_column", "render_pivot"]
