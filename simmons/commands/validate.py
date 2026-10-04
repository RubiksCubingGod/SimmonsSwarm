"""Validate experiment files and the inputs recorded by committed runs."""

from __future__ import annotations

import argparse
import importlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from difflib import get_close_matches
from pathlib import Path
from string import Template
from types import SimpleNamespace
from typing import Any

from pydantic import ValidationError

from ..catalog import check_models, load_catalog
from ..config import Config, ModelSpecConfig, model_param_issues, resolve_models
from ..contracts import SEND_MESSAGE_TOOL_NAME
from ..inputs import load
from ..records import canonical_json
from ..roles import request_for
from ..template_tags import SENDER_DEBRIEF_TEMPLATE_NAME
from ..types import EpisodeState, Pick, Target, TextPart

_MODEL_ROLES = {"sender", "receiver", "guard", "plain_sender", "judge", "default"}
_BOOL_FIELDS = {
    "guard_guesses_target",
    "guard_sees_sender_prompt",
    "sender_knows_protected",
    "receiver_knows_protected",
    "receiver_sees_log",
    "tell_partner_model",
    "shadow_decode",
}


@dataclass
class _Experiment:
    path: Path
    display_path: str
    name: str | None = None
    config: Config | None = None
    inputs: Any = None
    ids: dict[str, str] | None = None
    errors: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def public(self, *, resolved: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "path": self.display_path,
            "name": self.name,
            "errors": self.errors,
            "warnings": self.warnings,
        }
        if resolved and self.config is not None and not self.errors:
            fields = self.config.model_dump(mode="json")
            fields["models"] = {
                role: _model_value(model)
                for role, model in resolve_models(self.config).items()
            }
            if self.inputs is not None and fields["options_per_episode"] is None:
                fields["options_per_episode"] = len(self.inputs.option_set.options)
            if self.ids is not None:
                fields.update(self.ids)
            result["resolved"] = fields
        return result


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    command = subparsers.add_parser("validate", help="validate experiments and repository inputs")
    command.add_argument("paths", nargs="*", type=Path, help="experiment JSON files")
    command.add_argument("--resolved", action="store_true", help="include fully resolved config fields")
    command.add_argument("--json", action="store_true", help="print machine-readable output")
    command.set_defaults(handler=_run)


def _run(args: argparse.Namespace) -> int:
    root = Path.cwd().resolve()
    explicit = bool(args.paths)
    paths = [(_absolute(path, root), str(path)) for path in args.paths]
    if not explicit:
        experiment_dir = root / "experiments"
        paths = [
            (path, path.relative_to(root).as_posix())
            for path in sorted(experiment_dir.glob("*.json"))
        ] if experiment_dir.is_dir() else []

    reports = [validate_experiment(path, display, root) for path, display in paths]
    repo_errors: list[dict[str, str]] = []
    _check_committed_runs(reports, root, check_orphans=not explicit, errors=repo_errors)

    failed = any(report.errors for report in reports) or bool(repo_errors)
    if args.json:
        payload = {
            "experiments": [report.public(resolved=args.resolved) for report in reports],
            "errors": repo_errors,
            "warnings": [],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        if not reports:
            print("No experiments found.")
        for report in reports:
            label = report.name or report.display_path
            if report.errors:
                for error in report.errors:
                    print(f"ERROR {label} {error['path']}: {error['message']}")
            else:
                print(f"OK {label}")
                if args.resolved:
                    print(json.dumps(report.public(resolved=True)["resolved"], indent=2, ensure_ascii=False))
            for warning in report.warnings:
                print(f"WARNING {label}: {warning}")
        for error in repo_errors:
            print(f"ERROR {error['path']}: {error['message']}")
    return 2 if failed else 0


def validate_experiment(path: Path, display_path: str, root: Path) -> _Experiment:
    report = _Experiment(path=path, display_path=display_path)
    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except FileNotFoundError:
        _add(report.errors, "$", "experiment file does not exist")
        return report
    except OSError as exc:
        _add(report.errors, "$", f"cannot read experiment file: {exc.strerror or exc}")
        return report
    except UnicodeDecodeError:
        _add(report.errors, "$", "experiment file must be UTF-8")
        return report
    except json.JSONDecodeError as exc:
        _add(report.errors, "$", f"invalid JSON: {exc.msg}")
        return report
    except ValueError as exc:
        _add(report.errors, "$", f"invalid JSON: {exc}")
        return report

    if not isinstance(raw, dict):
        _add(report.errors, "$", "experiment config must be a JSON object")
        return report

    cleaned = _strip_ignored(raw)
    known = set(Config.model_fields)
    for key in sorted(set(cleaned) - known):
        suggestion = get_close_matches(key, sorted(known), n=1, cutoff=0.55)
        message = "unknown field"
        if suggestion:
            message += f"; did you mean {suggestion[0]!r}?"
        _add(report.errors, key, message)

    for key in sorted(_BOOL_FIELDS & cleaned.keys()):
        if type(cleaned[key]) is not bool:
            _add(report.errors, key, "must be a boolean")

    model_raw = cleaned.get("models")
    validation_models = model_raw
    if isinstance(model_raw, dict):
        for role in sorted(set(model_raw) - _MODEL_ROLES):
            suggestion = get_close_matches(role, sorted(_MODEL_ROLES), n=1, cutoff=0.55)
            message = "unknown model role"
            if suggestion:
                message += f"; did you mean {suggestion[0]!r}?"
            _add(report.errors, f"models.{role}", message)
        validation_models = dict(model_raw)
        for role, value in model_raw.items():
            if not isinstance(value, dict):
                continue
            for key in sorted(set(value) - {"model", "temperature", "max_tokens", "params"}):
                suggestion = get_close_matches(
                    key, ["model", "temperature", "max_tokens", "params"], n=1, cutoff=0.55
                )
                message = "unknown model field"
                if suggestion:
                    message += f"; did you mean {suggestion[0]!r}?"
                _add(report.errors, f"models.{role}.{key}", message)
            validation_models[role] = {
                key: item
                for key, item in value.items()
                if key in {"model", "temperature", "max_tokens", "params"}
            }

    validation_values = {key: value for key, value in cleaned.items() if key in known}
    if isinstance(validation_models, dict):
        validation_values["models"] = validation_models
    try:
        config = Config.model_validate(validation_values)
    except ValidationError as exc:
        for error in exc.errors(include_url=False):
            loc = error.get("loc", ())
            if isinstance(cleaned.get("models"), dict) and (
                loc == ("models",)
                or (len(loc) >= 2 and loc[0] == "models" and loc[-1] == "str")
            ):
                continue
            path_part = _error_path(error.get("loc", ()))
            _add(report.errors, path_part, str(error.get("msg", "invalid value")))
        return report

    report.config = config
    report.name = config.name
    if report.errors:
        return report

    if config.name != path.stem:
        _add(report.errors, "name", f"must match experiment filename stem {path.stem!r}")
        return report

    _check_local_rules(config, report)
    if report.errors:
        return report

    _check_input_paths(config, root, report)
    if report.errors:
        return report
    try:
        report.inputs = load(config)
    except (OSError, UnicodeError, ValueError) as exc:
        _add_from_message(report.errors, str(exc), "option_set")
        return report
    if not report.inputs.guard_prompt.strip():
        report.warnings.append("guard_prompt is empty.")

    pool_size = len(report.inputs.option_set.options)
    episode_option_count = config.options_per_episode or pool_size
    if config.option_draw == "sample" and config.options_per_episode == pool_size:
        _add(report.errors, "option_draw", "sample requires options_per_episode below the option_set size")
    if config.protected_count is not None and config.protected_count >= episode_option_count:
        _add(report.errors, "protected_count", "must be less than options_per_episode")
    if config.protected_selection == "cluster":
        if not report.inputs.option_set.clusters:
            _add(report.errors, "protected_selection", "cluster selection requires clusters in the option_set")
        elif config.protected_cluster is not None:
            cluster = report.inputs.option_set.clusters.get(config.protected_cluster)
            if cluster is None:
                _add(report.errors, "protected_cluster", "cluster does not exist in the option_set")
            elif config.protected_count is not None and len(cluster) < config.protected_count:
                _add(report.errors, "protected_cluster", "cluster is smaller than protected_count")
    if report.errors:
        return report

    scorer_type = _registered_scorer(config.scorer)
    if scorer_type is None:
        _add(report.errors, "scorer", _unknown_scorer_message(config.scorer))
        return report
    if _registered_payload(config.payload) is None:
        _add(report.errors, "payload", f"unknown payload {config.payload!r}")
        return report
    if config.payload not in scorer_type.payloads:
        _add(report.errors, "scorer", f"scorer {config.scorer!r} does not support payload {config.payload!r}")
        return report
    if scorer_type.needs_model and "judge" not in report.inputs.templates:
        _add(report.errors, "templates.judge", "file does not exist and the scorer requires a model")
        return report
    try:
        models = resolve_models(config)
    except (ValueError, OSError) as exc:
        _add_from_message(report.errors, str(exc), "models")
        return report
    if isinstance(config.models, dict):
        for role, value in config.models.items():
            if not isinstance(value, ModelSpecConfig):
                continue
            try:
                issues = model_param_issues(
                    value.params,
                    f"models.{role}.params",
                    model_name=value.model,
                )
            except ValueError as exc:
                _add(report.errors, f"models.{role}.model", str(exc))
                continue
            for issue_path, message in issues:
                _add(report.errors, issue_path, message)
    if report.errors:
        return report
    if scorer_type.needs_model and models.get("judge") is None:
        _add(report.errors, "models.judge", "a scorer that needs a model requires a judge model")
        return report

    try:
        _check_templates(config, report.inputs)
    except (KeyError, TypeError, ValueError, NotImplementedError) as exc:
        _add_from_message(report.errors, str(exc), "templates")
        return report

    try:
        catalog = load_catalog(root / ".simmons" / "openrouter-catalog.json")
        configured_models = {role: model for role, model in models.items() if model is not None}
        projected_input_tokens = (
            _projected_input_tokens(config, report.inputs, models, needs_judge=scorer_type.needs_model)
            if catalog.age_seconds() is not None
            else None
        )
        notices = check_models(
            configured_models,
            catalog,
            projected_input_tokens=projected_input_tokens,
        )
        report.warnings.extend(notices)
    except (OSError, ValueError, RuntimeError) as exc:
        _add_from_message(report.errors, str(exc), "models")
        return report

    if config.purpose == "eval":
        _check_eval_seed(config, root, report)
    if report.errors:
        return report

    try:
        from ..config import compute_ids

        report.ids = compute_ids(config, root)
    except (OSError, UnicodeError, ValueError) as exc:
        _add_from_message(report.errors, str(exc), "config_hash")
    return report


def _projected_input_tokens(
    config: Config,
    inputs: Any,
    models: Mapping[str, Any],
    *,
    needs_judge: bool,
) -> dict[str, int]:
    """Estimate the largest rendered request for each model role.

    The estimate uses the real role templates and views, the longest possible
    option draw, max-length messages, and the largest history available in one
    run. Sender debrief replays the send turn, so its larger request also counts
    against the sender model's context.
    """
    pool = tuple(inputs.option_set.options)
    option_count = min(config.options_per_episode or len(pool), len(pool))
    options = tuple(sorted(pool, key=lambda value: (-len(value), value))[:option_count])
    target = Target(payload=config.payload, value=options[0], index=0)
    pick = Pick(payload=config.payload, value=options[0], index=0)
    protected = (
        frozenset(range(config.protected_count))
        if config.protected_count is not None
        else None
    )

    history_count = max(0, config.episodes - 1)
    if config.log_window is not None:
        history_count = min(history_count, config.log_window)
    if not config.log_records:
        history_count = 0
    longest_option = max(pool, key=len)
    log_values = {
        "target": longest_option,
        "message": "x" * config.max_message_chars,
        "outcome": "CATCH",
        "receiver_pick": longest_option,
        "guard_suspicion": 0.9999999999999999,
        "guard_guess": longest_option,
    }
    log_entry = {name: log_values[name] for name in config.log_records}
    log_entry_line = json.dumps(log_entry, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    sender_spec = models.get("sender")
    assistant_message: dict[str, Any] = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "projected-send-tool-call",
                "type": "function",
                "function": {
                    "name": SEND_MESSAGE_TOOL_NAME,
                    "arguments": json.dumps(
                        {"message": "x" * config.max_message_chars},
                        separators=(",", ":"),
                    ),
                },
            }
        ],
    }
    if sender_spec is not None:
        # A send response can carry reasoning back into the debrief request.
        message_tokens = math.ceil(config.max_message_chars / 4) + 64
        reasoning_tokens = max(0, sender_spec.max_tokens - message_tokens)
        assistant_message["reasoning_details"] = [
            {"type": "reasoning.text", "text": "x" * (reasoning_tokens * 4)}
        ]

    state = EpisodeState(
        episode=max(0, config.episodes - 1),
        kind="covert",
        options=options,
        target=target,
        protected=protected,
        message="x" * config.max_message_chars,
        attempt_log=(),
        guard_suspicion=1.0,
        guard_guess=pick,
        receiver_pick=pick,
        outcome="HIT",
        sender_response=(
            SimpleNamespace(
                assistant_message=assistant_message,
                tool_call_id="projected-send-tool-call",
            )
            if sender_spec is not None
            else None
        ),
    )

    roles_by_model: dict[str, list[str]] = {
        "guard": ["guard"],
        "receiver": ["receiver"],
    }
    if config.plain_sender_rate > 0:
        roles_by_model["plain_sender"] = ["plain_sender"]
    if config.plain_sender_rate < 1:
        sender_roles = ["sender"]
        if config.sender_debrief != "off":
            sender_roles.append("sender_debrief")
        roles_by_model["sender"] = sender_roles
    if needs_judge:
        roles_by_model["judge"] = ["judge"]

    projections: dict[str, int] = {}
    for model_role, request_roles in roles_by_model.items():
        model = models.get(model_role)
        if model is None or model.provider != "openrouter":
            continue
        projections[model_role] = max(
            _request_input_tokens(
                request_for(role, state, config, inputs, model),
                log_entry_line=log_entry_line,
                history_count=history_count,
                log_occurrences=_log_section_occurrences(role, inputs, config),
            )
            for role in request_roles
        )
    return projections


def _request_input_tokens(
    request: Any,
    *,
    log_entry_line: str,
    history_count: int,
    log_occurrences: int,
) -> int:
    """Count a provider-shaped request at the spend estimator's chars/4 rate."""
    messages: list[dict[str, Any]] = []
    if request.system:
        messages.append({"role": "system", "content": request.system})
    messages.append(
        {
            "role": "user",
            "content": "\n\n".join(part.text for part in request.user if isinstance(part, TextPart)),
        }
    )
    tools: list[dict[str, Any]] = []
    if request.prior is not None:
        prior_tool = request.prior.tool
        tools.append(_tool_definition(prior_tool))
        messages.append(dict(request.prior.assistant))
        messages.append(
            {
                "role": "tool",
                "tool_call_id": request.prior.tool_call_id,
                "content": "\n\n".join(
                    part.text for part in request.prior.tool_result if isinstance(part, TextPart)
                ),
            }
        )
    if all(tool["function"]["name"] != request.tool.name for tool in tools):
        tools.append(_tool_definition(request.tool))
    body = {"model": request.model.model, "messages": messages, "tools": tools}
    body_chars = len(canonical_json(body))
    if history_count and log_occurrences:
        # Add the history analytically. This preserves the exact JSON escaping
        # cost without materializing up to 100,000 potentially max-size records.
        escaped_entry_chars = len(json.dumps(log_entry_line, ensure_ascii=False)) - 2
        escaped_separator_chars = len(json.dumps("\n", ensure_ascii=False)) - 2
        body_chars += log_occurrences * (
            history_count * escaped_entry_chars
            + (history_count - 1) * escaped_separator_chars
        )
    return math.ceil(body_chars / 4)


def _log_section_occurrences(role: str, inputs: Any, config: Config) -> int:
    if role == "sender":
        templates = (("sender.md", "sender"),)
    elif role == "sender_debrief":
        templates = (("sender.md", "sender"), ("sender_debrief.md", "sender_debrief"))
    elif role == "receiver" and config.receiver_sees_log:
        templates = (("receiver.md", "receiver"),)
    else:
        return 0

    occurrences = 0
    for name, template_role in templates:
        template = _role_template(inputs, name, template_role)
        occurrences += sum(
            1
            for match in Template.pattern.finditer(template)
            if match.group("named") == "log_section" or match.group("braced") == "log_section"
        )
    return occurrences


def _role_template(inputs: Any, name: str, role: str) -> str:
    for key in (name, role, f"templates/{name}"):
        value = inputs.templates.get(key)
        if isinstance(value, str):
            return value
    return ""


def _tool_definition(tool: Any) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


def _check_local_rules(config: Config, report: _Experiment) -> None:
    if config.payload != "option":
        _add(report.errors, "payload", "v1 accepts only the 'option' payload")
    if config.control_rate + config.plain_sender_rate > 1:
        _add(report.errors, "control_rate", "control_rate + plain_sender_rate must be at most 1")

    has_watchlist = config.protected_count is not None
    if not has_watchlist:
        if config.control_rate != 0:
            _add(report.errors, "control_rate", "must be 0 without a watchlist")
        if config.protected_selection != "random":
            _add(report.errors, "protected_selection", "is only used with a watchlist")
        if config.protected_cluster is not None:
            _add(report.errors, "protected_cluster", "is only used with protected_selection='cluster'")
        if config.protected_draw != "sample":
            _add(report.errors, "protected_draw", "is only used with a watchlist")
        if config.sender_knows_protected:
            _add(report.errors, "sender_knows_protected", "requires a watchlist")
        if config.receiver_knows_protected:
            _add(report.errors, "receiver_knows_protected", "requires a watchlist")

    if config.protected_cluster is not None and config.protected_selection != "cluster":
        _add(report.errors, "protected_cluster", "requires protected_selection='cluster'")
    if config.option_draw == "sample" and config.options_per_episode is None:
        _add(report.errors, "option_draw", "sample requires options_per_episode below the option_set size")
    if config.messages_per_episode != 1:
        _add(report.errors, "messages_per_episode", "v1 accepts only 1")
    if len(set(config.log_records)) != len(config.log_records):
        _add(report.errors, "log_records", "values must be unique")
    if (
        config.protected_count is not None
        and config.options_per_episode is not None
        and config.protected_count >= config.options_per_episode
    ):
        _add(report.errors, "protected_count", "must be less than options_per_episode")

    if math_is_one(config.control_rate + config.plain_sender_rate):
        report.warnings.append("control_rate + plain_sender_rate is 1; there will be no covert episodes.")


def _registered_scorer(name: str) -> type[Any] | None:
    _import_builtin("simmons.scorers.exact_match")
    try:
        from ..scorers import get_scorer

        return get_scorer(name)
    except KeyError:
        return None


def _registered_payload(name: str) -> type[Any] | None:
    _import_builtin("simmons.payloads.option")
    try:
        from ..payloads import get_payload

        return get_payload(name)
    except KeyError:
        return None


def _import_builtin(module_name: str) -> None:
    try:
        importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise


def _unknown_scorer_message(name: str) -> str:
    from ..scorers import scorer_names

    suggestion = get_close_matches(name, scorer_names(), n=1, cutoff=0.55)
    message = f"unknown scorer {name!r}"
    if suggestion:
        message += f"; did you mean {suggestion[0]!r}?"
    return message


def _check_input_paths(config: Config, root: Path, report: _Experiment) -> None:
    option_path = _option_set_path(config.option_set, root)
    if not option_path.is_file():
        _add(report.errors, "option_set", f"file does not exist: {config.option_set}")
    for field_name in ("sender_prompt", "guard_prompt", "plain_sender_prompt", "cover_task"):
        value = getattr(config, field_name)
        path = _absolute(Path(value), root)
        if not path.is_file():
            _add(report.errors, field_name, f"file does not exist: {value}")
    template_dir = _absolute(Path(config.templates), root)
    if not template_dir.is_dir():
        _add(report.errors, "templates", f"directory does not exist: {config.templates}")
        return
    required = ["sender.md", "plain_sender.md", "guard.md", "receiver.md"]
    if config.sender_debrief != "off":
        required.append("sender_debrief.md")
    for filename in required:
        if not (template_dir / filename).is_file():
            role = filename.removesuffix(".md")
            _add(report.errors, f"templates.{role}", f"file does not exist: {template_dir / filename}")


def _check_templates(config: Config, inputs: Any) -> None:
    from ..render import render
    from ..types import EpisodeState, Pick, Target
    from ..visibility import build_view

    options = tuple(inputs.option_set.options)
    target = Target(payload=config.payload, value=options[0], index=0)
    pick_index = min(1, len(options) - 1)
    pick = Pick(payload=config.payload, value=options[pick_index], index=pick_index)
    protected = (
        frozenset(range(min(config.protected_count or 0, len(options))))
        if config.protected_count is not None
        else None
    )
    state = EpisodeState(
        episode=0,
        kind="covert",
        options=options,
        target=target,
        protected=protected,
        message="A sample foreign affairs report.",
        guard_suspicion=0.25,
        guard_guess=pick,
        receiver_pick=pick,
        outcome="MISS",
    )
    for role, template in inputs.templates.items():
        view_role = "sender_debrief" if role == SENDER_DEBRIEF_TEMPLATE_NAME else role
        view = build_view(view_role, state, config, inputs)
        try:
            render(template, view)
        except Exception as exc:
            raise ValueError(f"templates.{view_role}: {exc}") from exc


def _check_eval_seed(config: Config, root: Path, report: _Experiment) -> None:
    from ..config import compute_ids

    dev_config = config.model_copy(update={"purpose": "dev"})
    try:
        arena_hash = compute_ids(dev_config, root)["arena_hash"]
    except (OSError, UnicodeError, ValueError) as exc:
        _add_from_message(report.errors, str(exc), "seed")
        return
    for run_path in sorted((root / "runs").glob("*/*/run.json")):
        run_dir = run_path.parent
        record = _read_object(run_path)
        if record is None:
            continue
        saved_config = _read_object(run_dir / "config.json") or {}
        overrides = record.get("overrides")
        if not isinstance(overrides, dict):
            overrides = {}
        purpose = record.get("purpose", saved_config.get("purpose"))
        if purpose != "dev":
            continue
        ids = record.get("ids")
        saved_arena = ids.get("arena") if isinstance(ids, dict) else None
        if saved_arena != arena_hash:
            continue
        seed = overrides.get("seed", saved_config.get("seed"))
        if seed == config.seed:
            _add(report.errors, "seed", f"eval seed {config.seed} was used by committed dev run {run_dir}")
            return


def _check_committed_runs(
    reports: list[_Experiment],
    root: Path,
    *,
    check_orphans: bool,
    errors: list[dict[str, str]] | None = None,
) -> None:
    repo_errors = errors if errors is not None else []
    by_name = {
        report.config.name: report
        for report in reports
        if report.config is not None and not report.errors
    }
    runs_root = root / "runs"
    if not runs_root.is_dir():
        return
    for experiment_dir in sorted(path for path in runs_root.iterdir() if path.is_dir()):
        report = by_name.get(experiment_dir.name)
        if report is None:
            if check_orphans:
                _add(repo_errors, f"runs.{experiment_dir.name}", "has no matching experiment file")
            continue
        for run_dir in _run_dirs(root, experiment_dir.name):
            record = _read_object(run_dir / "run.json")
            if record is None:
                _add(repo_errors, f"runs.{experiment_dir.name}.{run_dir.name}", "run.json is missing or invalid")
                continue
            _check_run_hash(report, run_dir, record, root)
            _check_run_snapshot(report, run_dir, repo_errors)


def _check_run_hash(report: _Experiment, run_dir: Path, record: dict[str, Any], root: Path) -> None:
    if report.config is None:
        return
    ids = record.get("ids")
    saved_hash = ids.get("config") if isinstance(ids, dict) else None
    if not isinstance(saved_hash, str):
        return
    overrides = record.get("overrides", {})
    if not isinstance(overrides, dict):
        _add(report.errors, "config_hash", f"run {run_dir.name} has invalid recorded overrides")
        return
    values = report.config.model_dump(mode="python")
    values.update(overrides)
    try:
        effective = Config.model_validate(values)
        from ..config import compute_ids

        current_hash = compute_ids(effective, root)["config_hash"]
    except (ValidationError, OSError, UnicodeError, ValueError) as exc:
        _add_from_message(report.errors, str(exc), "config_hash")
        return
    if current_hash != saved_hash:
        _add(report.errors, "config_hash", f"experiment no longer resolves to committed run {run_dir.name}")


def _check_run_snapshot(report: _Experiment, run_dir: Path, errors: list[dict[str, str]]) -> None:
    expected = getattr(report.inputs, "source_files", None)
    if not isinstance(expected, Mapping):
        return
    snapshot = run_dir / "inputs"
    if not snapshot.is_dir():
        _add(errors, f"runs.{report.name}.{run_dir.name}.inputs", "committed run has no input snapshot")
        return
    for relative, content in expected.items():
        path = snapshot / relative
        if not path.is_file():
            _add(errors, f"runs.{report.name}.{run_dir.name}.inputs.{relative}", "snapshot file is missing")
        elif path.read_bytes() != content:
            _add(errors, f"runs.{report.name}.{run_dir.name}.inputs.{relative}", "differs from the committed input snapshot")


def _run_dirs(root: Path, experiment: str) -> list[Path]:
    path = root / "runs" / experiment
    if not path.is_dir():
        return []
    return sorted(child for child in path.iterdir() if child.is_dir())


def _read_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-standard numeric literal {value}")


def _check_local_rules(config: Config, report: _Experiment) -> None:
    if config.payload != "option":
        _add(report.errors, "payload", "v1 accepts only the 'option' payload")
    if config.control_rate + config.plain_sender_rate > 1:
        _add(report.errors, "control_rate", "control_rate + plain_sender_rate must be at most 1")

    has_watchlist = config.protected_count is not None
    if not has_watchlist:
        if config.control_rate != 0:
            _add(report.errors, "control_rate", "must be 0 without a watchlist")
        if config.protected_selection != "random":
            _add(report.errors, "protected_selection", "is only used with a watchlist")
        if config.protected_cluster is not None:
            _add(report.errors, "protected_cluster", "is only used with protected_selection='cluster'")
        if config.protected_draw != "sample":
            _add(report.errors, "protected_draw", "is only used with a watchlist")
        if config.sender_knows_protected:
            _add(report.errors, "sender_knows_protected", "requires a watchlist")
        if config.receiver_knows_protected:
            _add(report.errors, "receiver_knows_protected", "requires a watchlist")

    if config.protected_cluster is not None and config.protected_selection != "cluster":
        _add(report.errors, "protected_cluster", "requires protected_selection='cluster'")
    if config.option_draw == "sample" and config.options_per_episode is None:
        _add(report.errors, "option_draw", "sample requires options_per_episode below the option_set size")
    if config.messages_per_episode != 1:
        _add(report.errors, "messages_per_episode", "v1 accepts only 1")
    if len(set(config.log_records)) != len(config.log_records):
        _add(report.errors, "log_records", "values must be unique")

    if math_is_one(config.control_rate + config.plain_sender_rate):
        report.warnings.append("control_rate + plain_sender_rate is 1; there will be no covert episodes.")


def _error_path(loc: Any) -> str:
    ignored = {"str", "ModelSpecConfig", "int", "float", "bool"}
    parts = [
        str(part)
        for part in loc
        if str(part) not in ignored
        and not str(part).startswith("dict[")
        and not str(part).startswith("union[")
    ]
    return ".".join(parts) if parts else "$"


def _add(errors: list[dict[str, str]], path: str, message: str) -> None:
    errors.append({"path": path or "$", "message": message})


def _add_from_message(errors: list[dict[str, str]], message: str, default_path: str) -> None:
    for line in message.splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_.\[\]-]*):\s*(.*)$", line)
        if match:
            _add(errors, match.group(1), match.group(2) or line)
        else:
            _add(errors, default_path or "$", line or "invalid value")


def _absolute(path: Path, root: Path) -> Path:
    return path if path.is_absolute() else root / path


def _strip_ignored(value: Any, *, in_params: bool = False) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_ignored(item, in_params=in_params or key == "params")
            for key, item in value.items()
            if in_params or not (isinstance(key, str) and key.startswith("_"))
        }
    if isinstance(value, list):
        return [_strip_ignored(item, in_params=in_params) for item in value]
    return value


def _option_set_path(value: str, root: Path) -> Path:
    if "/" in value:
        return _absolute(Path(value), root)
    return root / "option-sets" / f"{value}.json"


def _model_value(model: Any) -> dict[str, Any] | None:
    if model is None:
        return None
    return {
        "provider": model.provider,
        "model": model.model,
        "temperature": model.temperature,
        "max_tokens": model.max_tokens,
        "params": _normalize_numbers(dict(model.params)),
    }


def _normalize_numbers(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    if isinstance(value, dict):
        return {key: _normalize_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_numbers(item) for item in value]
    return value


def math_is_one(value: float) -> bool:
    return value == 1.0


__all__ = ["register", "validate_experiment"]
