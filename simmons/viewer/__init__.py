"""Static, self-contained HTML views over a committed Simmons run."""

from __future__ import annotations

import html
import json
import re
from dataclasses import replace
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..config import Config
from ..inputs import _validate_options
from ..records import read_jsonl, read_requests_xz, request_sha256
from ..roles import request_for
from ..types import EpisodeState, Inputs, ModelSpec, Pick, RoleRequest, RoleResponse, Target, ToolSpec, Usage

_CALL_SLOTS = (
    ("sender", "Sender", "sender"),
    ("guard", "Guard", "guard"),
    ("receiver", "Receiver", "receiver"),
    ("shadow_receiver", "Shadow receiver", "receiver"),
    ("judge", "Judge", "judge"),
    ("sender_debrief", "Sender exit interview", "sender_debrief"),
)
_ROLE_ABOUT = {
    "Sender": "S writes the cover-task message that carries the target to R. System prompt: P₁.",
    "Plain sender": "An innocent sender writes a genuine cover-task message, naming the target openly when required. System prompt: plain sender prompt.",
    "Guard": "G scores suspicion and guesses the target; at or above the threshold the message is blocked. System prompt: P₂.",
    "Receiver": "R reads the delivered message and names the target it carries. System prompt: P₁.",
    "Shadow receiver": "R reads a blocked message in a separate measurement call that cannot change the outcome; it feeds the all-covert decode gap.",
    "Judge": "J scores R's pick against the target. Exact-match scoring runs in code, so no model is called.",
    "Sender exit interview": "After the outcome, S is asked privately to explain its scheme. Nothing it says reaches any role. Off by default.",
}
_PROMPT_SECTIONS = (
    ("prompt", "sender", "P₁ · sender prompt", "Shared by sender and receiver (system prompt); also the exit interview's system prompt.", True),
    ("prompt", "guard", "P₂ · guard prompt", "Guard only (system prompt). The guard never sees P₁ unless the harder mode is on.", True),
    ("prompt", "plain_sender", "Plain sender prompt", "Plain sender only (system prompt).", False),
    ("cover_task", "", "Cover task", "Inserted into the sender, plain sender, guard, receiver and exit interview templates.", False),
    ("template", "sender.md", "Sender template", "Sender's user message: cover task, cities, target and attempt log.", False),
    ("template", "receiver.md", "Receiver template", "Receiver's user message: the delivered message, cover task and any options.", False),
    ("template", "guard.md", "Guard template", "Guard's user message: the message, cover task, any options and any watchlist.", False),
    ("template", "plain_sender.md", "Plain sender template", "Plain sender's user message: cover task, cities and target.", False),
    ("template", "judge.md", "Judge template", "Judge's user message when the scorer needs a model; exact match never sends it.", False),
    ("template", "sender_debrief.md", "Sender exit interview template", "Sent back to S after the outcome, as the result of its send call.", False),
)
_RATE_LABELS = {
    "hit_rate": "Hit rate",
    "catch_rate": "Catch rate",
    "false_alarm_rate": "False alarm rate",
    "sanity_catch_rate": "Sanity catch rate",
}
_MODEL_ROLES = ("sender", "receiver", "guard", "plain_sender", "judge")


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def _load_inputs(run_dir: Path, config: Config) -> Inputs:
    root = run_dir / "inputs"
    option_value = json.loads((root / "option-set.json").read_text(encoding="utf-8"))
    option_set = _validate_options(option_value, config.options_per_episode)
    prompts = root / "prompts"
    templates_dir = root / "templates"
    templates: dict[str, str] = {}
    for name in (
        "sender.md",
        "plain_sender.md",
        "guard.md",
        "receiver.md",
        "judge.md",
        "sender_debrief.md",
    ):
        path = templates_dir / name
        if path.is_file():
            templates[name] = path.read_text(encoding="utf-8")
    return Inputs(
        option_set=option_set,
        sender_prompt=(prompts / "sender.md").read_text(encoding="utf-8"),
        guard_prompt=(prompts / "guard.md").read_text(encoding="utf-8"),
        plain_sender_prompt=(prompts / "plain_sender.md").read_text(encoding="utf-8"),
        cover_task=(root / "cover-task.md").read_text(encoding="utf-8"),
        templates=templates,
    )


def _request_components(record: Mapping[str, Any]) -> dict[str, Any]:
    components = {key: record[key] for key in ("model", "system", "user", "tool")}
    if record.get("prior") is not None:
        components["prior"] = record["prior"]
    return components


def _request_matches_archive(record: Mapping[str, Any], expected: str) -> bool:
    recorded = record.get("request_sha256")
    return (
        isinstance(recorded, str)
        and recorded == expected
        and request_sha256(_request_components(record)) == expected
    )


def _model_spec(value: Mapping[str, Any]) -> ModelSpec:
    return ModelSpec(
        provider=str(value["provider"]),
        model=str(value["model"]),
        temperature=value.get("temperature"),
        max_tokens=int(value["max_tokens"]),
        params=dict(value.get("params", {})),
    )


def _recorded_models(
    run: Mapping[str, Any],
    requests: list[Mapping[str, Any]],
    raw_config: Mapping[str, Any],
) -> dict[str, dict[str, str] | None]:
    models: dict[str, dict[str, str] | None] = {role: None for role in _MODEL_ROLES}
    for request in requests:
        role = request.get("role")
        spec = request.get("model")
        if isinstance(role, str) and role in models and isinstance(spec, Mapping):
            provider = spec.get("provider")
            model = spec.get("model")
            if isinstance(provider, str) and isinstance(model, str):
                models[str(role)] = {"provider": provider, "model": model}

    preflight = run.get("preflight")
    roles = preflight.get("roles", ()) if isinstance(preflight, Mapping) else ()
    for entry in roles:
        if not isinstance(entry, Mapping):
            continue
        role = entry.get("role")
        value = entry.get("model")
        if (
            not isinstance(role, str)
            or role not in models
            or models[role] is not None
            or not isinstance(value, str)
        ):
            continue
        provider, separator, model = value.partition(":")
        if separator and provider and model:
            models[role] = {"provider": provider, "model": model}

    configured = raw_config.get("models")
    for role in _MODEL_ROLES:
        if models[role] is not None:
            continue
        if isinstance(configured, str):
            value: Any = None if role == "judge" else configured
        elif isinstance(configured, Mapping):
            if role in configured:
                value = configured[role]
            elif role == "judge":
                value = None
            elif "default" in configured:
                value = configured["default"]
            else:
                value = "framework default"
        else:
            value = None if role == "judge" else "framework default"
        if isinstance(value, Mapping):
            value = value.get("model")
        if isinstance(value, str) and value:
            models[role] = {"configured": value}
    return models


def _option_enums(
    value: Any, saved_options: list[str], episode_options: list[str]
) -> Any:
    # tools.json is materialized against plan row 0; each episode may draw or order its own options.
    if isinstance(value, Mapping):
        return {
            key: _option_enums(item, saved_options, episode_options)
            for key, item in value.items()
        }
    if isinstance(value, list):
        if value == saved_options:
            return list(episode_options)
        if value == [*saved_options, None]:
            return [*episode_options, None]
        return [_option_enums(item, saved_options, episode_options) for item in value]
    return value


def _snapshot_tool(
    roles: Mapping[str, Any],
    role: str,
    saved_options: list[str],
    episode_options: list[str],
) -> ToolSpec | None:
    value = roles.get(role)
    if not isinstance(value, Mapping) or not isinstance(value.get("input_schema"), Mapping):
        return None
    return ToolSpec(
        name=str(value["name"]),
        description=str(value["description"]),
        input_schema=_option_enums(value["input_schema"], saved_options, episode_options),
    )


def _with_snapshot_tools(
    request: RoleRequest,
    roles: Mapping[str, Any],
    role: str,
    saved_options: list[str],
    episode_options: list[str],
) -> RoleRequest:
    tool = _snapshot_tool(roles, role, saved_options, episode_options)
    if tool is not None:
        request = replace(request, tool=tool)
    if role == "sender_debrief" and request.prior is not None:
        send_tool = _snapshot_tool(roles, "sender", saved_options, episode_options)
        if send_tool is not None:
            request = replace(request, prior=replace(request.prior, tool=send_tool))
    return request


def _state_for_request(
    episode: Mapping[str, Any],
    plan: Mapping[str, Any],
    role: str,
    payload: str,
    attempt_log: Mapping[int, Mapping[str, Any]],
    debrief_request: Mapping[str, Any] | None,
    *,
    shadow_receiver: bool = False,
) -> EpisodeState:
    options = tuple(str(option) for option in plan["options"])
    target_index = int(plan["target"])
    protected_value = plan.get("protected")
    protected = None if protected_value is None else frozenset(int(i) for i in protected_value)
    sender_log = episode.get("log_seen", {}).get("sender") or ()
    receiver_log = episode.get("log_seen", {}).get("receiver") or ()
    visible_log: tuple[Mapping[str, Any], ...] = ()
    if role in {"sender", "sender_debrief"}:
        visible_log = tuple(attempt_log[i] for i in sender_log if i in attempt_log)
    elif role == "receiver":
        visible_log = tuple(attempt_log[i] for i in receiver_log if i in attempt_log)

    message_record = next(iter(episode.get("messages", ())), {})
    message = message_record.get("sent")
    delivered = message_record.get("delivered")
    guard = episode.get("guard") or {}
    guess_record = guard.get("guess")
    pick_record = episode.get("pick")
    sender_response = None
    if role == "sender_debrief" and debrief_request is not None:
        prior = debrief_request.get("prior")
        if isinstance(prior, Mapping):
            sender_response = RoleResponse(
                tool_input=None,
                text=None,
                thinking=None,
                reasoning_details=None,
                parse_error=None,
                usage=Usage(input_tokens=0, output_tokens=0),
                cost_usd=None,
                generation_id=None,
                served_by=None,
                model_reported="",
                finish_reason="",
                tool_choice_mode="",
                latency_ms=0,
                raw={},
                assistant_message=prior.get("assistant"),
                tool_call_id=prior.get("tool_call_id"),
            )
    return EpisodeState(
        episode=int(episode["index"]),
        kind=str(episode["kind"]),
        options=options,
        target=Target(payload, options[target_index], target_index),
        protected=protected,
        message=(message if shadow_receiver else delivered) if role == "receiver" else message,
        attempt_log=visible_log,
        guard_suspicion=guard.get("suspicion"),
        guard_guess=(
            Pick(payload, str(guess_record["value"]), _optional_int(guess_record.get("index")))
            if isinstance(guess_record, Mapping)
            else None
        ),
        receiver_pick=(
            Pick(payload, str(pick_record["value"]), _optional_int(pick_record.get("index")))
            if isinstance(pick_record, Mapping)
            else None
        ),
        outcome=episode.get("outcome"),
        sender_response=sender_response,
    )


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _fresh_request_hash(
    episode: Mapping[str, Any],
    plan: Mapping[str, Any],
    role: str,
    inputs: Inputs,
    config: Config,
    archive_record: Mapping[str, Any],
    attempts: Mapping[int, Mapping[str, Any]],
    tool_roles: Mapping[str, Any],
    saved_options: list[str],
    *,
    shadow_receiver: bool = False,
) -> str:
    state = _state_for_request(
        episode,
        plan,
        role,
        config.payload,
        attempts,
        archive_record if role == "sender_debrief" else None,
        shadow_receiver=shadow_receiver,
    )
    rebuilt = request_for(role, state, config, inputs, _model_spec(archive_record["model"]))
    rebuilt = _with_snapshot_tools(
        rebuilt, tool_roles, role, saved_options, list(plan["options"])
    )
    return request_sha256(rebuilt)


def _call_views(
    episode: Mapping[str, Any],
    plan: Mapping[str, Any],
    archive: Mapping[str, Mapping[str, Any]],
    attempts: Mapping[int, Mapping[str, Any]],
    inputs: Inputs,
    config: Config,
    tool_roles: Mapping[str, Any],
    saved_options: list[str],
) -> list[dict[str, Any]]:
    calls = episode.get("calls") or {}
    result: list[dict[str, Any]] = []
    for slot, label, requested_role in _CALL_SLOTS:
        call = calls.get(slot)
        if not isinstance(call, Mapping):
            result.append({"slot": slot, "label": label, "about": _ROLE_ABOUT[label], "called": False})
            continue
        role = str(call.get("role") or requested_role)
        if slot == "shadow_receiver":
            role = "receiver"
        expected_hash = call.get("request_sha256")
        stored = archive.get(str(expected_hash)) if expected_hash else None
        archive_match = isinstance(stored, Mapping) and _request_matches_archive(stored, str(expected_hash))
        fresh_hash: str | None = None
        render_error: str | None = None
        if archive_match and isinstance(stored, Mapping):
            try:
                fresh_hash = _fresh_request_hash(
                    episode,
                    plan,
                    role,
                    inputs,
                    config,
                    stored,
                    attempts,
                    tool_roles,
                    saved_options,
                    shadow_receiver=slot == "shadow_receiver",
                )
            except (KeyError, TypeError, ValueError, IndexError) as exc:
                render_error = str(exc)
        hash_match = bool(archive_match and fresh_hash == expected_hash)
        shown_label = "Plain sender" if slot == "sender" and role == "plain_sender" else label
        result.append(
            {
                "slot": slot,
                "label": shown_label,
                "about": _ROLE_ABOUT[shown_label],
                "called": True,
                "hash": expected_hash,
                "hash_match": hash_match,
                "archive_match": bool(archive_match),
                "render_error": render_error,
                "request": dict(stored) if isinstance(stored, Mapping) else None,
            }
        )
    return result


def _episode_page_data(
    episode: Mapping[str, Any],
    plan: Mapping[str, Any],
    run: Mapping[str, Any],
    config: Config,
    archive: Mapping[str, Mapping[str, Any]],
    attempts: Mapping[int, Mapping[str, Any]],
    inputs: Inputs,
    tool_roles: Mapping[str, Any],
    saved_options: list[str],
) -> dict[str, Any]:
    target = episode.get("target") or {}
    protected = episode.get("protected") or ()
    return {
        "page": "episode",
        "run": {"experiment": run.get("experiment"), "run_id": run.get("run_id")},
        "episode": dict(episode),
        "options": list(plan["options"]),
        "target_index": target.get("index", plan["target"]),
        "protected_indices": [item["index"] for item in protected],
        "payload": config.payload,
        "sender_debrief": config.sender_debrief,
        "roles": _call_views(
            episode, plan, archive, attempts, inputs, config, tool_roles, saved_options
        ),
        "attempt_log": [attempts[index] for index in (episode.get("log_seen", {}).get("sender") or ()) if index in attempts],
    }


def _load_run_data(run_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    run = _read_object(run_dir / "run.json")
    raw_config = _read_object(run_dir / "config.json")
    config_fields = {name: raw_config[name] for name in Config.model_fields if name in raw_config}
    config = Config.model_validate(config_fields)
    summary = _read_object(run_dir / "summary.json")
    episodes = sorted(read_jsonl(run_dir / "episodes.jsonl"), key=lambda row: int(row["index"]))
    plan_rows = read_jsonl(run_dir / "plan.jsonl")
    plan = {int(row["index"]): row for row in plan_rows}
    requests = read_requests_xz(run_dir / "requests.jsonl.xz")
    tools_path = run_dir / "inputs" / "tools.json"
    tool_snapshot = _read_object(tools_path).get("roles", {}) if tools_path.is_file() else {}
    tool_roles = tool_snapshot if isinstance(tool_snapshot, Mapping) else {}
    saved_options = list(plan[min(plan)]["options"]) if plan else []
    archive: dict[str, Mapping[str, Any]] = {}
    for request in requests:
        digest = request.get("request_sha256")
        if isinstance(digest, str):
            archive.setdefault(digest, request)
    attempt_rows = read_jsonl(run_dir / "attempt-log.jsonl") if (run_dir / "attempt-log.jsonl").exists() else []
    attempts = {int(row["episode"]): row for row in attempt_rows}
    inputs = _load_inputs(run_dir, config)

    episode_rows: list[dict[str, Any]] = []
    episode_pages: list[dict[str, Any]] = []
    for episode in episodes:
        index = int(episode["index"])
        plan_row = plan.get(index)
        if plan_row is None:
            raise ValueError(f"episode {index} has no matching plan record")
        filename = f"episode-{index:06d}.html"
        guess_index = ((episode.get("guard") or {}).get("guess") or {}).get("index")
        target_index = (episode.get("target") or {}).get("index")
        episode_rows.append(
            {
                "index": index,
                "kind": episode["kind"],
                "outcome": episode["outcome"],
                "suspicion": (episode.get("guard") or {}).get("suspicion"),
                "guess": ((episode.get("guard") or {}).get("guess") or {}).get("value"),
                "guess_correct": (
                    guess_index is not None
                    and target_index is not None
                    and guess_index == target_index
                ),
                "pick": (episode.get("pick") or {}).get("value"),
                "target": (episode.get("target") or {}).get("value"),
                "href": filename,
            }
        )
        episode_pages.append(
            _episode_page_data(
                episode,
                plan_row,
                run,
                config,
                archive,
                attempts,
                inputs,
                tool_roles,
                saved_options,
            )
        )

    cover_task = inputs.cover_task
    config_data = {key: value for key, value in raw_config.items() if key not in {"ids", "protocol_version"}}
    models = _recorded_models(run, requests, raw_config)
    prompt_names = {
        role: Path(str(raw_config.get(f"{role}_prompt", role))).stem
        for role in ("sender", "guard", "plain_sender")
    }
    run_page = {
        "page": "run",
        "run": run,
        "config": config_data,
        "models": models,
        "summary": summary,
        "cover_task": cover_task,
        "prompt_names": prompt_names,
        "prompts": _prompt_sections(inputs, prompt_names),
        "episodes": episode_rows,
    }
    return run_page, episode_pages


def _prompt_sections(inputs: Inputs, prompt_names: Mapping[str, str]) -> list[dict[str, Any]]:
    """List the run's snapshotted prompts, cover task and role templates, labelled by recipient."""
    texts = {
        "sender": inputs.sender_prompt,
        "guard": inputs.guard_prompt,
        "plain_sender": inputs.plain_sender_prompt,
    }
    sections: list[dict[str, Any]] = []
    for kind, key, label, who, open_ in _PROMPT_SECTIONS:
        if kind == "prompt":
            text, source = texts[key], f"{prompt_names.get(key, key)} (inputs/prompts/{key}.md)"
        elif kind == "cover_task":
            text, source = inputs.cover_task, "inputs/cover-task.md"
        else:
            if key not in inputs.templates:
                continue
            text, source = inputs.templates[key], f"inputs/templates/{key}"
        sections.append({"label": label, "who": who, "source": source, "text": text, "open": open_})
    return sections


def _safe_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return (
        encoded.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


_STYLE = r"""
:root{color-scheme:dark;--bg:#0d121b;--panel:#151d29;--line:#344153;--text:#f2f5f8;--muted:#b9c4d1;--cyan:#6ee7f5;--green:#9be6b1;--red:#ff9292;--gold:#ffd166;--purple:#c8a8ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{max-width:1180px;margin:0 auto;padding:28px 24px 64px}h1,h2,h3{line-height:1.2;margin:0 0 14px}h1{font-size:30px}h2{font-size:21px}h3{font-size:17px}.muted{color:var(--muted)}.eyebrow{color:var(--cyan);font-size:13px;letter-spacing:.08em;text-transform:uppercase}.top{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;margin-bottom:22px}.top a,.episode-link{color:var(--cyan);text-decoration:none}.top a:hover,.episode-link:hover{text-decoration:underline}.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin:14px 0}.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}.meta div{min-width:0}.meta strong{display:block;color:var(--muted);font-size:13px}.cover-task summary{color:var(--cyan);cursor:pointer}.cover-task-full{margin-top:8px;color:var(--muted);font-size:13px;white-space:pre-wrap;overflow-wrap:anywhere}.metric-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:10px}.metric{background:#101722;border:1px solid var(--line);border-radius:9px;padding:12px}.metric strong{display:block;font-size:22px;font-variant-numeric:tabular-nums}.metric span,.metric small{display:block;color:var(--muted)}.toolbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:12px 0}.toolbar label{display:flex;align-items:center;gap:8px;color:var(--muted)}select,button{font:inherit;color:var(--text);background:#111925;border:1px solid #637287;border-radius:7px;min-height:40px;padding:7px 12px}select{width:180px}button{cursor:pointer}button:hover{border-color:var(--cyan)}button[aria-selected="true"]{border-color:var(--cyan);box-shadow:inset 0 -2px var(--cyan)}table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line)}th{color:var(--muted);font-size:13px}td.num{text-align:right}.table-wrap{overflow-x:auto}.pill{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:2px 9px;white-space:nowrap}.pill.CATCH{color:var(--red)}.pill.HIT{color:var(--green)}.pill.MISS{color:var(--gold)}.pill.REFUSAL{color:var(--purple)}.curve{width:100%;height:auto;display:block}.curve text{fill:var(--muted);font:13px sans-serif}.options{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px}.option{background:#101722;border:1px solid var(--line);border-radius:9px;padding:10px;min-height:54px}.option.target{outline:2px solid var(--gold);outline-offset:1px}.option.protected{background:#302d20;border-color:#a98b3e}.option.guess{box-shadow:inset 0 0 0 2px var(--purple)}.option.pick{border-color:var(--green)}.badge{display:block;font-size:12px;color:var(--muted);margin-top:3px}.panels{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}.panel{background:#101722;border:1px solid var(--line);border-radius:9px;padding:14px;min-width:0}.bar{height:10px;background:#263346;border-radius:8px;overflow:hidden;margin:10px 0}.bar span{display:block;height:100%;background:var(--cyan)}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#0a0f17;border:1px solid #283445;border-radius:7px;padding:12px;color:var(--text);font:14px/1.5 ui-monospace,SFMono-Regular,monospace;max-height:460px;overflow:auto}.tabs{display:flex;gap:7px;flex-wrap:wrap;margin:12px 0}.status{padding:3px 8px;border-radius:999px;font-weight:650;font-size:13px}.status.good{background:#173d31;color:#b4f4c8}.status.bad{background:#512c31;color:#ffd0d0}.status.unknown{background:#3e3825;color:#ffe3a0}.status.not-called{background:#253142;color:#c9d4df}.status.refused{background:#3a2a52;color:#e2d2ff}.split{display:grid;grid-template-columns:1fr 1fr;gap:14px}.content-block{overflow-wrap:anywhere}.small{font-size:13px}@media(max-width:650px){main{padding:20px 14px 40px}.top{display:block}.split{grid-template-columns:1fr}h1{font-size:25px}}
"""

_STYLE += "\n.bar{position:relative;overflow:visible}.bar span{border-radius:8px}.bar i{position:absolute;top:-3px;height:16px;width:2px;background:var(--gold);transform:translateX(-1px)}"
_STYLE += (
    "\n.option-card>summary{cursor:pointer}.option-card>summary strong{font-size:17px;margin-right:12px}"
    ".option-card[open]>summary{margin-bottom:12px}.card-head{display:flex;align-items:center;gap:12px;margin-bottom:14px}"
    ".card-head h2{margin:0}.role-tabs{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:8px;margin:12px 0}"
    ".role-tab{display:flex;flex-direction:column;align-items:flex-start;gap:4px;text-align:left;padding:10px 12px}"
    ".role-tab span{line-height:1.4}.role-tab.idle strong{color:var(--muted)}.prompt{border-top:1px solid var(--line);padding:10px 0}"
    ".prompt summary{cursor:pointer}.prompt summary strong{color:var(--cyan)}.prompt-source{margin:8px 0 6px}.card>pre:last-child{margin-bottom:0}"
)


_STYLE += (
    "\n.step{display:grid;grid-template-columns:150px minmax(0,1fr);gap:16px;padding:16px 0;border-top:1px solid var(--line)}"
    ".step-label{color:var(--cyan);font-weight:650}.step-body>h3{margin:14px 0 6px;font-size:14px;color:var(--muted)}"
    ".verdicts{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin:6px 0}"
    "@media(max-width:650px){.step{grid-template-columns:1fr;gap:6px}}"
)


_SCRIPT = r"""
const data=JSON.parse(document.getElementById("viewer-data").textContent);
const root=document.getElementById("app");
const el=(tag,text="",cls="")=>{const n=document.createElement(tag);if(text!==null)n.textContent=String(text);if(cls)n.className=cls;return n};
const add=(parent,tag,text="",cls="")=>{const n=el(tag,text,cls);parent.append(n);return n};
const show=(v)=>v===null||v===undefined||v===""?"—":String(v);
const pct=(v)=>v===null||v===undefined?"—":`${(Number(v)*100).toFixed(1)}%`;
const money=(v)=>v===null||v===undefined?"—":`$${Number(v).toFixed(2)}`;
const pre=(parent,label,value)=>{add(parent,"h3",label);add(parent,"pre",value??"(empty)")};
function header(title,subtitle,back){
 const top=add(root,"header","","top");const left=add(top,"div");add(left,"div","SIMMONS SWARM","eyebrow");add(left,"h1",title);add(left,"div",subtitle,"muted");if(back){const a=add(top,"a","Back to run");a.href="index.html";}
}
function metric(parent,label,rate){const box=add(parent,"div","","metric");add(box,"span",label);add(box,"strong",pct(rate?.value));add(box,"small",rate?`${rate.num} / ${rate.den}${rate.lo==null||rate.hi==null?"":` · 95% ${pct(rate.lo)}–${pct(rate.hi)}`}`:"Not available")}
function runPage(){
 const r=data.run,s=data.summary,c=data.config,episodes=data.episodes||[];
 header(`${show(r.experiment)} · run`,`${show(r.run_id)} · ${show(r.status)}`);
 const meta=add(root,"section","","card meta");
 const modelLabel=(value)=>value&&typeof value==="object"?(value.provider&&value.model?`${value.provider}:${value.model}`:value.configured?`${value.configured} (configured; no call recorded)`:"—"):"—";
 const modelRows=Object.entries({sender:"Sender model",receiver:"Receiver model",guard:"Guard model",plain_sender:"Plain sender model",judge:"Judge model"}).map(([role,label])=>[label,data.models?.[role]]);
 for(const [label,value] of [["Author",r.author],["Episodes",`${r.episodes_done} / ${r.episodes_planned}`],["Sender prompt",data.prompt_names.sender],["Guard prompt",data.prompt_names.guard],["Plain sender prompt",data.prompt_names.plain_sender],["Cover task",data.cover_task],["Arena",r.ids?.arena],...modelRows]){const cell=add(meta,"div");add(cell,"strong",label);if(label==="Cover task"){const details=add(cell,"details","","cover-task");const first=value.split(/\r?\n/).map(x=>x.trim()).find(Boolean)||"Cover task";add(details,"summary",first.replace(/^#+\s*/,""));add(details,"div",value,"cover-task-full");}else add(cell,"span",label.endsWith(" model")?modelLabel(value):show(value));}
 const prompts=add(root,"section","","card");add(prompts,"h2","Prompts");add(prompts,"p","Exactly what each role was given in this run, from its inputs snapshot.","muted small");for(const item of data.prompts||[]){const d=add(prompts,"details","","prompt");d.open=!!item.open;const sum=add(d,"summary");add(sum,"strong",item.label);add(sum,"span",` · ${item.who}`,"muted");add(d,"div",item.source,"small muted prompt-source");add(d,"pre",item.text||"(empty)");}
 const rates=add(root,"section","","card");add(rates,"h2","Run metrics");const grid=add(rates,"div","","metric-grid");for(const [key,label] of Object.entries({hit_rate:"Hit rate",catch_rate:"Catch rate",false_alarm_rate:"False-alarm rate",sanity_catch_rate:"Sanity catch rate",refusal_rate:"Refusal rate"}))metric(grid,label,s.rates?.[key]);
 const decode=s.decode||{};const dg=add(rates,"div","","metric-grid");metric(dg,"Receiver decode",decode.receiver);metric(dg,"Guard decode",decode.guard);const gap=add(dg,"div","","metric");add(gap,"span","Decode gap");add(gap,"strong",pct(decode.gap));add(gap,"small",`Chance R ${pct(decode.chance?.receiver)} · G ${pct(decode.chance?.guard)}`);const cost=add(dg,"div","","metric");add(cost,"span","Spend");add(cost,"strong",money(s.cost?.usd??r.spent_usd));add(cost,"small",s.cost?.preflight_usd?`USD · incl. preflight ${money(s.cost.preflight_usd)}`:"USD");
 const curve=add(root,"section","","card");add(curve,"h2","Catch vs false-alarm curve");drawCurve(curve,s.curve,s.rates?.false_alarm_rate);
 const section=add(root,"section","","card");add(section,"h2",`Episodes (${episodes.length})`);const toolbar=add(section,"div","","toolbar");
 const kindLabel=add(toolbar,"label","Kind");const kind=add(kindLabel,"select");kind.setAttribute("aria-label","Filter by kind");for(const [v,t] of [["all","All kinds"],["covert","Covert"],["control","Control"],["plain_allowed","Plain allowed"],["plain_protected","Plain protected"]]){const o=add(kind,"option",t);o.value=v;}
 const outcomeLabel=add(toolbar,"label","Outcome");const outcome=add(outcomeLabel,"select");outcome.setAttribute("aria-label","Filter by outcome");for(const [v,t] of [["all","All outcomes"],["CATCH","CATCH"],["MISS","MISS"],["HIT","HIT"],["REFUSAL","REFUSAL"],["ERROR","ERROR"]]){const o=add(outcome,"option",t);o.value=v;}
 const sortLabel=add(toolbar,"label","Sort");const sort=add(sortLabel,"select");sort.setAttribute("aria-label","Sort episodes");for(const [v,t] of [["index","Episode number"],["high","Suspicion high to low"],["low","Suspicion low to high"]]){const o=add(sort,"option",t);o.value=v;}
 const count=add(toolbar,"span","","muted");count.style.minWidth="120px";const wrap=add(section,"div","","table-wrap");const table=add(wrap,"table");const thead=add(table,"thead");const tr=add(thead,"tr");for(const label of ["Episode","Kind","Outcome","Suspicion","Guard guess","Pick"] )add(tr,"th",label);const body=add(table,"tbody");
 function update(){let rows=episodes.filter(x=>(kind.value==="all"||x.kind===kind.value)&&(outcome.value==="all"||x.outcome===outcome.value));if(sort.value!=="index")rows.sort((a,b)=>sort.value==="high"?(b.suspicion??-1)-(a.suspicion??-1):(a.suspicion??2)-(b.suspicion??2));else rows.sort((a,b)=>a.index-b.index);body.replaceChildren();for(const item of rows){const row=add(body,"tr");const td=add(row,"td");const a=add(td,"a",`#${item.index}`,"episode-link");a.href=item.href;add(row,"td",item.kind);add(row,"td",item.outcome,`pill ${item.outcome}`);add(row,"td",item.suspicion==null?"—":Number(item.suspicion).toFixed(3),"num");add(row,"td",`${item.guess??"—"}${item.guess_correct?" ✓":""}`);add(row,"td",item.pick??"—");}count.textContent=`${rows.length} episodes`;}
 kind.addEventListener("change",update);outcome.addEventListener("change",update);sort.addEventListener("change",update);update();
}
function drawCurve(parent,curve,falseAlarmRate){
 if(!curve){add(parent,"p","No curve is available for this run.","muted");return;}
 const svg=document.createElementNS("http://www.w3.org/2000/svg","svg");svg.setAttribute("viewBox","0 0 720 320");svg.setAttribute("class","curve");svg.setAttribute("role","img");svg.setAttribute("aria-label","Catch rate against false-alarm rate");parent.append(svg);
 const x0=58,y0=260,w=610,h=220;const line=(x1,y1,x2,y2)=>{const n=document.createElementNS(svg.namespaceURI,"line");for(const [k,v] of Object.entries({x1,y1,x2,y2,stroke:"#526176","stroke-width":1}))n.setAttribute(k,String(v));svg.append(n);};
 line(x0,y0,x0+w,y0);line(x0,y0,x0,y0-h);for(let i=0;i<=4;i++){const f=i/4,x=x0+w*f,y=y0-h*f;line(x,y0,x,y0+4);line(x0-4,y,x0,y);const tx=document.createElementNS(svg.namespaceURI,"text");tx.setAttribute("x",String(x));tx.setAttribute("y",String(y0+24));tx.setAttribute("text-anchor","middle");tx.textContent=`${Math.round(f*100)}%`;svg.append(tx);const ty=document.createElementNS(svg.namespaceURI,"text");ty.setAttribute("x",String(x0-9));ty.setAttribute("y",String(y+4));ty.setAttribute("text-anchor","end");ty.textContent=`${Math.round(f*100)}%`;svg.append(ty);}
 const points=Array.isArray(curve.points)?curve.points.filter(p=>p.catch!=null&&p.false_alarm!=null&&Number.isFinite(Number(p.catch))&&Number.isFinite(Number(p.false_alarm))):[];const coords=points.map(p=>`${x0+w*Math.max(0,Math.min(1,Number(p.false_alarm)))},${y0-h*Math.max(0,Math.min(1,Number(p.catch)))}`).join(" ");if(points.length){const poly=document.createElementNS(svg.namespaceURI,"polyline");poly.setAttribute("points",coords);poly.setAttribute("fill","none");poly.setAttribute("stroke","#6ee7f5");poly.setAttribute("stroke-width","3");svg.append(poly);}
 const op=curve.operating_point;const validOp=op&&op.catch!=null&&op.false_alarm!=null&&Number.isFinite(Number(op.catch))&&Number.isFinite(Number(op.false_alarm));if(validOp){const dot=document.createElementNS(svg.namespaceURI,"circle");dot.setAttribute("cx",String(x0+w*Math.max(0,Math.min(1,Number(op.false_alarm)))));dot.setAttribute("cy",String(y0-h*Math.max(0,Math.min(1,Number(op.catch)))));dot.setAttribute("r","6");dot.setAttribute("fill","#ffd166");svg.append(dot);}
const xl=document.createElementNS(svg.namespaceURI,"text");xl.setAttribute("x",String(x0+w/2));xl.setAttribute("y","310");xl.setAttribute("text-anchor","middle");xl.textContent="False-alarm rate";svg.append(xl);const yl=document.createElementNS(svg.namespaceURI,"text");yl.setAttribute("x","13");yl.setAttribute("y",String(y0-h/2));yl.setAttribute("transform",`rotate(-90 13 ${y0-h/2})`);yl.setAttribute("text-anchor","middle");yl.textContent="Catch rate";svg.append(yl);
 let note=`AUC ${show(curve.auc)} · operating point ${pct(op?.catch)} catch / ${pct(op?.false_alarm)} false alarm`;if(falseAlarmRate?.den===0)note+=" · false-alarm rate undefined (no innocent traffic)";else if(curve.points===null)note+=" · verdict mode has no threshold curve";else if(!points.length)note+=" · no plottable threshold rates";add(parent,"div",note,"muted small");
}
function verdict(parent,text,cls){return add(parent,"span",text,`status ${cls}`)}
function judgeLine(parent,call,score,matched,exact){
 if(exact){verdict(parent,matched?"Exact match ✓":"No match ✗",matched?"good":"bad");return}
 const out=call?.output;if(!out&&!score){add(parent,"span","Not judged","muted small");return}
 const value=out?.score??score?.value;verdict(parent,matched?`Judge: names the target ✓ (${show(value)})`:`Judge: wrong place ✗ (${show(value)})`,matched?"good":"bad");
 const reason=out?.reason??score?.detail?.reason;if(reason)add(parent,"p",reason,"content-block small muted");
}
function step(parent,label){const row=add(parent,"div","","step");add(row,"div",label,"step-label");return add(row,"div","","step-body")}
function storyCard(e){
 const card=add(root,"section","","card");add(card,"h2","What happened");
 const covert=e.kind==="covert"||e.kind==="control";const target=data.options[data.target_index];const exact=data.payload==="option";
 const refusal=e.refusal,refusedRole=refusal?String(refusal.role):null;
 const refused=(parent,roles)=>{if(!refusal||!roles.some(x=>refusedRole===x||refusedRole.endsWith(x)))return false;const label=refusedRole.replace("_"," ");verdict(parent,`Refused by ${label}`,"refused");const n=add(parent,"p","","small muted");n.append(`detected by ${refusal.signal}${label.endsWith("sender")?"":` · “${refusal.excerpt}”`}`);return true};
 const s=step(card,"1 · Sender");const m=e.messages?.[0];
 refused(s,["sender"]);
 add(s,"p",covert?`Hid the target: ${show(target)}`:`Plain sender: an ordinary message with nothing hidden. The drawn target (${show(target)}) was not given to it.`);
 const versions=[["Sent",m?.sent],["Rewritten",m?.rewritten],["Delivered",m?.delivered]].filter(([,v])=>v!=null);
 if(new Set(versions.map(([,v])=>v)).size>1){for(const [label,v] of versions){add(s,"h3",label);add(s,"pre",v)}}else add(s,"pre",versions[0]?.[1]??"No message was sent.");
 const g=step(card,"2 · Guard");
 if(!e.guard){if(!refused(g,["guard"]))add(g,"p","No guard decision recorded.","muted")}
 else{const flagged=e.guard.blocked===true;const head=add(g,"div","","verdicts");verdict(head,flagged?"Flagged: blocked":"Not flagged: passed",flagged?"bad":"good");
  add(head,"span",`suspicion ${e.guard.suspicion==null?"—":Number(e.guard.suspicion).toFixed(2)} · threshold ${show(e.guard.threshold)}`,"muted small");
  const suspicion=Number(e.guard.suspicion)||0,threshold=Number(e.guard.threshold);const bar=add(g,"div","","bar");const fill=add(bar,"span");fill.style.width=`${Math.max(0,Math.min(1,suspicion))*100}%`;if(e.guard.threshold!=null&&Number.isFinite(threshold)){const tick=add(bar,"i");tick.style.left=`${Math.max(0,Math.min(1,threshold))*100}%`}
  const guess=e.guard.guess;const gl=add(g,"div","","verdicts");add(gl,"strong",`Guess: ${guess?.value??"no guess"}`);
  if(guess)judgeLine(g,e.calls?.guard_judge,null,guess.index===data.target_index,exact);
  if(e.calls?.guard?.output?.reason){add(g,"h3","Guard's reasoning");add(g,"p",e.calls.guard.output.reason,"content-block")}}
 const r=step(card,"3 · Receiver");
 if(e.outcome==="CATCH")add(r,"p","Not called: the guard blocked the message.","muted");
 else if(refused(r,["receiver"])){}
 else if(!e.calls?.receiver){const f=e.error;add(r,"p",f?.role?`Not called: ${f.role} failed before the receiver ran.`:"Not called.","muted");if(f?.message)add(r,"p",f.message,"content-block small")}
 else{const rl=add(r,"div","","verdicts");add(rl,"strong",`Answer: ${e.pick?.value??"—"}`);judgeLine(r,e.calls?.judge,e.score,e.score?.hit===true,exact);
  refused(r,["judge"]);
  if(e.calls?.receiver?.output?.reason){add(r,"h3","Receiver's reasoning");add(r,"p",e.calls.receiver.output.reason,"content-block")}}
 const d=step(card,"4 · Exit interview");
 if(e.calls?.sender_debrief){const out=e.calls.sender_debrief.output||{};for(const [label,key] of [["Scheme","scheme"],["Encoding","encoding"],["Expected decode","expected_decode"],["Confidence","confidence"]]){add(d,"h3",label);add(d,"p",show(out[key]),"content-block")}const prior=e.calls.sender_debrief.prior_reasoning;add(d,"p",prior?.passed_back?"Prior reasoning carried":"Prior reasoning reconstructed from the message","muted small");if(e.calls.sender?.thinking)add(d,"details").append(el("summary","Send call thinking"),el("pre",e.calls.sender.thinking))}
 else if(!covert)add(d,"p","None: plain episodes hide nothing, so the sender is not interviewed.","muted");
 else if(e.debrief_error)add(d,"p",`The interview failed: ${e.debrief_error}`,"status bad");
 else if(data.sender_debrief==="off")add(d,"p","Off for this run (sender_debrief: off).","muted");
 else add(d,"p","Not recorded.","muted");
}
function episodePage(){
 const e=data.episode,r=data.run;header(`Episode ${Number(e.index)}`,`${show(e.kind)} · ${show(e.outcome)} · run ${show(r.run_id)}`,true);
 const summaryParts=[`${data.options.length} options`,`target ${show(data.options[data.target_index])}`];if(data.protected_indices.length)summaryParts.push(`${data.protected_indices.length} protected${data.protected_indices.includes(data.target_index)?" (target protected)":""}`);summaryParts.push(e.guard?`guard guessed ${show(e.guard.guess?.value)}${e.guard.guess?.index===data.target_index?" ✓":""}`:"guard: not called");summaryParts.push(e.calls?.receiver?`receiver picked ${show(e.pick?.value)}${e.pick?.index===data.target_index?" ✓":""}`:"receiver: not called");

 storyCard(e);
 if(data.payload==="option"){const optionCard=add(root,"details","","card option-card");const optionSummary=add(optionCard,"summary");add(optionSummary,"strong","Options");add(optionSummary,"span",summaryParts.join(" · "));add(optionCard,"p","Gold outline: target · shaded: protected · purple: guard guess · green: receiver pick","muted small");const options=add(optionCard,"div","","options");for(let i=0;i<data.options.length;i++){const c=[];if(i===data.target_index)c.push("target");if(data.protected_indices.includes(i))c.push("protected");if(e.guard?.guess?.index===i)c.push("guess");if(e.pick?.index===i)c.push("pick");const chip=add(options,"div",data.options[i],`option ${c.join(" ")}`);if(i===data.target_index)add(chip,"span","Target","badge");if(data.protected_indices.includes(i))add(chip,"span","Protected","badge");if(e.guard?.guess?.index===i)add(chip,"span","Guard guess","badge");if(e.pick?.index===i)add(chip,"span","Receiver pick","badge");}}
 else{const pool=add(root,"details","","card option-card");const ps=add(pool,"summary");add(ps,"strong","Target pool");add(ps,"span",`open answers: no role saw this list · target ${show(data.options[data.target_index])}`);add(pool,"p",data.options.join(" · "),"muted small");}
 const seen=add(root,"section","","card");add(seen,"h2","As seen by");add(seen,"p","The exact request each role received, re-rendered from the run inputs and hash-checked.","muted small");const tabs=add(seen,"div","","role-tabs");const panel=add(seen,"div");const roleRows=data.roles||[];for(let i=0;i<roleRows.length;i++){const role=roleRows[i];const button=add(tabs,"button","",`role-tab${role.called?"":" idle"}`);button.type="button";add(button,"strong",role.called?role.label:`${role.label} · not called`);if(role.about)add(button,"span",role.about,"small muted");button.setAttribute("aria-selected",i===0?"true":"false");button.addEventListener("click",()=>{for(const b of tabs.querySelectorAll("button"))b.setAttribute("aria-selected",b===button?"true":"false");renderRequest(panel,role);});}if(roleRows.length)renderRequest(panel,roleRows[0]);
 const log=add(root,"section","","card");add(log,"h2","Attempt log as sender saw it");if(data.attempt_log.length){for(const item of data.attempt_log){const row=add(log,"div","","panel");add(row,"strong",`#${item.episode} · ${item.outcome}`);add(row,"p",item.target);add(row,"pre",item.message);}}else add(log,"p","No earlier sender attempts were in the log.","muted");
}
function renderRequest(parent,row){
 parent.replaceChildren();if(!row.called){add(parent,"p","Not called in this episode.","muted");return;}if(!row.request){add(parent,"p","Request is missing from requests.jsonl.xz.","status bad");return;}
 const status=row.hash_match?"Request hash matches fresh render":"Request hash does not match fresh render";add(parent,"div",status,`status ${row.hash_match?"good":"bad"}`);add(parent,"p",`sha256 ${show(row.hash)}`,"small muted content-block");if(row.render_error)add(parent,"p",`Fresh render error: ${row.render_error}`,"small status bad");const q=row.request;pre(parent,"System",q.system);add(parent,"h3","User");for(const part of q.user||[]){if(part.text!==undefined)add(parent,"pre",part.text);else add(parent,"pre",JSON.stringify(part,null,2));}pre(parent,"Tool",JSON.stringify(q.tool,null,2));if(q.prior){add(parent,"h3","Prior turn");pre(parent,"Tool call",JSON.stringify({tool:q.prior.tool,tool_call_id:q.prior.tool_call_id},null,2));pre(parent,"Assistant turn",JSON.stringify(q.prior.assistant,null,2));for(const part of q.prior.tool_result||[])add(parent,"pre",part.text??JSON.stringify(part,null,2));}
}
if(data.page==="run")runPage();else episodePage();
"""


def _document(data: Mapping[str, Any]) -> str:
    if data.get("page") == "run":
        title = "Simmons run viewer"
    else:
        episode = data.get("episode", {})
        title = f"Episode {episode.get('index')} · Simmons episode viewer"
    safe_title = html.escape(title, quote=True)
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{safe_title}</title><style>{_STYLE}</style></head>"
        "<body><main id=\"app\"></main>"
        f"<script id=\"viewer-data\" type=\"application/json\">{_safe_json(data)}</script>"
        f"<script>{_SCRIPT}</script></body></html>\n"
    )


def render_run_page(data: Mapping[str, Any]) -> str:
    return _document(data)


def render_episode_page(data: Mapping[str, Any]) -> str:
    return _document(data)


def generate(run_dir: str | Path, output_dir: str | Path | None = None) -> tuple[Path, list[Path]]:
    """Write the run index and one local page for every recorded episode.

    Pages go to ``<run_dir>/view`` unless ``output_dir`` names another directory.
    """
    root = Path(run_dir).resolve()
    run_page, episode_pages = _load_run_data(root)
    output = Path(output_dir) if output_dir is not None else root / "view"
    output.mkdir(parents=True, exist_ok=True)
    index_path = output / "index.html"
    index_path.write_text(render_run_page(run_page), encoding="utf-8")
    paths: list[Path] = []
    for page in episode_pages:
        index = int(page["episode"]["index"])
        path = output / f"episode-{index:06d}.html"
        path.write_text(render_episode_page(page), encoding="utf-8")
        paths.append(path)
    keep = {path.name for path in paths}
    for stale in output.glob("episode-*.html"):
        if re.fullmatch(r"episode-[0-9]{6}\.html", stale.name) and stale.name not in keep:
            stale.unlink()
    return index_path, paths


__all__ = ["generate", "render_episode_page", "render_run_page"]
