"""M1 run orchestration: preflight, sequential waves, records, and summaries."""

from __future__ import annotations

import hashlib
import json
import lzma
import math
import os
import re
import socket
import subprocess
import tempfile
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from . import __version__, events, metrics
from .catalog import load_catalog
from .config import Config, PROTOCOL_VERSION, compute_ids, resolve_models
from .episode import CallFailure, _call_role, run_episode
from .inputs import load as load_inputs, snapshot
from .payloads import get_payload
from .payloads import option as _option_payload  # register the M1 option payload
from .payloads import text as _text_payload  # register the open-answer payload
from .plan import build_plan, plan_hash
from .providers.base import get_provider
from .records import append_jsonl, canonical_json, read_jsonl, request_sha256, write_metadata
from .scorers import get_scorer
from .scorers import exact_match as _exact_match_scorer  # register the M1 scorer
from .scorers import llm_judge as _llm_judge_scorer  # register the model-judged scorer
from .spend import Breaker, BreakerTripped, between_waves, check_before_run, estimate_breakdown, run_cap
from .types import EpisodeContext, EpisodeState, ModelSpec, Pick, RoleResponse, Target, TransientError
from .visibility import build_view
from .roles import tool_for


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = stream.name
            stream.write(canonical_json(value))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug or "unknown"


def _author() -> str:
    configured = os.environ.get("SIMMONS_AUTHOR")
    if configured:
        return _slug(configured)
    try:
        value = subprocess.run(
            ["git", "config", "user.name"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        value = "unknown"
    return _slug(value)


def _git_value(*args: str) -> str | None:
    try:
        value = subprocess.run(
            ["git", *args], check=True, capture_output=True, text=True, timeout=3
        ).stdout.strip()
        return value or None
    except (subprocess.SubprocessError, OSError):
        return None


def _base_runs_root(repo_root: Path, local: bool) -> Path:
    override = os.environ.get("SIMMONS_RUNS_ROOT")
    root = Path(override).expanduser() if override else repo_root
    return root / ("runs-local" if local else "runs")


@dataclass(frozen=True)
class _RunMetadata:
    path: Path
    run: dict[str, Any]
    config: dict[str, Any]

    @property
    def run_id(self) -> str:
        value = self.run.get("run_id")
        return value if isinstance(value, str) and value else self.path.name

    @property
    def config_hash(self) -> str | None:
        for record in (self.run, self.config):
            ids = record.get("ids")
            if isinstance(ids, Mapping) and isinstance(ids.get("config"), str):
                return str(ids["config"])
        return None

    @property
    def seed(self) -> int | None:
        value = self.config.get("seed")
        return value if type(value) is int else None


@dataclass(frozen=True)
class _RunHistory:
    metadata: _RunMetadata
    episodes: list[dict[str, Any]]
    attempts: list[dict[str, Any]]


def _history_roots(repo_root: Path, local: bool) -> tuple[Path, ...]:
    """Return only roots this run is allowed to inherit from."""
    primary = _base_runs_root(repo_root, local)
    roots = (primary, primary.parent / "runs") if local else (primary,)
    return tuple(dict.fromkeys(root.resolve() for root in roots))


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def _run_metadata(run_dir: Path) -> _RunMetadata:
    return _RunMetadata(
        path=run_dir,
        run=_read_object(run_dir / "run.json"),
        config=_read_object(run_dir / "config.json"),
    )


def _iter_run_metadata(roots: Iterable[Path]) -> list[_RunMetadata]:
    result: list[_RunMetadata] = []
    seen_paths: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for run_json in sorted(root.rglob("run.json")):
            run_dir = run_json.parent
            resolved = run_dir.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            result.append(_run_metadata(run_dir))
    return result


def _trajectory_ancestors(metadata: _RunMetadata) -> list[str]:
    trajectory = metadata.run.get("trajectory", {})
    if not isinstance(trajectory, Mapping):
        raise ValueError(f"{metadata.path / 'run.json'}: trajectory must be an object")
    raw = trajectory.get("inherited_from", [])
    if not isinstance(raw, list):
        raise ValueError(
            f"{metadata.path / 'run.json'}: trajectory.inherited_from must be an array"
        )
    result: list[str] = []
    for item in raw:
        if not isinstance(item, Mapping) or not isinstance(item.get("run_id"), str):
            raise ValueError(f"{metadata.path / 'run.json'}: invalid trajectory ancestor")
        run_id = str(item["run_id"])
        if run_id not in result:
            result.append(run_id)
    return result


def _history_signature(metadata: _RunMetadata) -> tuple[tuple[str, int], ...]:
    trajectory = metadata.run.get("trajectory", {})
    if not isinstance(trajectory, Mapping):
        raise ValueError(f"{metadata.path / 'run.json'}: trajectory must be an object")
    raw = trajectory.get("inherited_from", [])
    if not isinstance(raw, list):
        raise ValueError(f"{metadata.path / 'run.json'}: trajectory.inherited_from must be an array")
    signature: list[tuple[str, int]] = []
    for item in raw:
        if not isinstance(item, Mapping) or not isinstance(item.get("run_id"), str):
            raise ValueError(f"{metadata.path / 'run.json'}: invalid trajectory ancestor")
        entries = item.get("entries", 0)
        if type(entries) is not int or entries < 0:
            raise ValueError(f"{metadata.path / 'run.json'}: ancestor entries must be a non-negative integer")
        signature.append((str(item["run_id"]), entries))
    return tuple(sorted(signature))


def _find_trajectory(
    repo_root: Path,
    *,
    local: bool,
    config_hash: str,
    seed: int,
    log_scope: str,
) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
    """Return the next global index, inherited attempt log, and lineage."""
    if log_scope != "experiment":
        return 0, [], []

    matching = [
        item
        for item in _iter_run_metadata(_history_roots(repo_root, local))
        if item.config_hash == config_hash and item.seed == seed
    ]
    if not matching:
        return 0, [], []

    by_id: dict[str, _RunHistory] = {}
    for metadata in matching:
        run_dir = metadata.path
        episodes_path = run_dir / "episodes.jsonl"
        attempts_path = run_dir / "attempt-log.jsonl"
        episodes = read_jsonl(episodes_path) if episodes_path.exists() else []
        attempts = read_jsonl(attempts_path) if attempts_path.exists() else []
        by_id.setdefault(metadata.run_id, _RunHistory(metadata, episodes, attempts))

    referenced = {
        ancestor
        for history in by_id.values()
        for ancestor in _trajectory_ancestors(history.metadata)
        if ancestor in by_id
    }
    heads = [history for run_id, history in by_id.items() if run_id not in referenced]
    if not heads:
        heads = list(by_id.values())

    def branch_end(head: _RunHistory) -> int:
        run_ids = [*_trajectory_ancestors(head.metadata), head.metadata.run_id]
        missing = [run_id for run_id in run_ids if run_id not in by_id]
        if missing:
            raise ValueError(
                f"{head.metadata.path / 'run.json'}: missing trajectory history run {missing[0]}"
            )
        highest = -1
        for run_id in dict.fromkeys(run_ids):
            history = by_id[run_id]
            highest = max(
                highest,
                max((int(record["index"]) for record in history.episodes), default=-1),
            )
        return highest

    # The tie-break is stable across machines and agrees with the run-id order.
    head = min(heads, key=lambda item: (-branch_end(item), item.metadata.run_id))
    selected_ids = list(dict.fromkeys([*_trajectory_ancestors(head.metadata), head.metadata.run_id]))
    inherited_log: list[dict[str, Any]] = []
    inherited_from: list[dict[str, Any]] = []
    highest_index = -1
    for run_id in selected_ids:
        history = by_id[run_id]
        highest_index = max(
            highest_index,
            max((int(record["index"]) for record in history.episodes), default=-1),
        )
        inherited_log.extend(history.attempts)
        inherited_from.append({"run_id": run_id, "entries": len(history.attempts)})

    inherited_log.sort(key=lambda record: int(record["episode"]))
    return highest_index + 1, inherited_log, inherited_from


def _forked_run_ids(metadata: Iterable[_RunMetadata]) -> set[str]:
    groups: dict[tuple[str | None, int | None, tuple[tuple[str, int], ...]], set[str]] = {}
    for item in metadata:
        if item.config.get("log_scope", "experiment") != "experiment":
            continue
        key = (item.config_hash, item.seed, _history_signature(item))
        groups.setdefault(key, set()).add(item.run_id)
    return {run_id for run_ids in groups.values() if len(run_ids) > 1 for run_id in run_ids}


def _resolve_config(
    raw: Any,
    *,
    fake: bool,
    episodes: int | None,
    seed: int | None,
    max_cost: float | None,
) -> tuple[Config, dict[str, Any]]:
    if not isinstance(raw, dict):
        raise ValueError("experiment file must contain a JSON object")
    fields = dict(raw)
    overrides: dict[str, Any] = {}
    if fake:
        fake_models: Any = "fake:covert"
        if fields.get("scorer", "exact_match") != "exact_match":
            # A model scorer needs a judge, which a bare model string leaves unset.
            fake_models = {"default": "fake:covert", "judge": "fake:covert"}
        fields["models"] = fake_models
        overrides["models"] = fake_models
    if episodes is not None:
        fields["episodes"] = episodes
        overrides["episodes"] = episodes
    if seed is not None:
        fields["seed"] = seed
        overrides["seed"] = seed
    config = Config.model_validate(fields)
    if max_cost is not None:
        cap = run_cap(config, max_cost)
        fields = config.model_dump(mode="python")
        fields["max_cost_usd"] = cap
        config = Config.model_validate(fields)
        overrides["max_cost_usd"] = cap
    return config, overrides


def _model_mapping(config: Config) -> dict[str, ModelSpec | None]:
    # Resolve once, before preflight or any spend reservation.
    return resolve_models(config)


def _provider_instances(models: dict[str, ModelSpec | None]) -> tuple[dict[str, Any], dict[str, Any]]:
    # Registries are intentionally explicit: importing an adapter registers it.
    from .providers import fake as _fake  # noqa: F401
    from .providers import openrouter as _openrouter  # noqa: F401

    by_role: dict[str, Any] = {}
    by_provider: dict[str, Any] = {}
    initialized: dict[tuple[str, str], Any] = {}
    for role, spec in models.items():
        if spec is None:
            continue
        key = (spec.provider, canonical_json(dict(spec.params)))
        if key not in initialized:
            provider_type = get_provider(spec.provider)
            initialized[key] = provider_type(**dict(spec.params))
        by_role[role] = initialized[key]
        by_provider.setdefault(spec.provider, initialized[key])
    return {**by_provider, **by_role}, by_role


def _limit_remaining(detail: str) -> float | None:
    """Parse OpenRouter's optional limit_remaining field from ProviderStatus.detail."""
    found: str | None = None
    for item in detail.split(";"):
        name, separator, value = item.strip().partition("=")
        if name != "limit_remaining":
            continue
        if not separator or found is not None:
            raise ValueError("OpenRouter key check returned an invalid limit_remaining field")
        found = value.strip()
    if found is None or found.casefold() in {"none", "null"}:
        return None
    try:
        remaining = Decimal(found)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("OpenRouter key check returned an invalid limit_remaining field") from exc
    if not remaining.is_finite() or remaining < 0:
        raise ValueError("OpenRouter key check returned an invalid limit_remaining field")
    result = float(remaining)
    if not math.isfinite(result):
        raise ValueError("OpenRouter key check returned an invalid limit_remaining field")
    return result


def _check_provider_limits(providers: dict[str, Any]) -> float | None:
    """Check each provider once and return the tightest reported OpenRouter limit."""
    checked: set[int] = set()
    limits: list[float] = []
    for provider in providers.values():
        if id(provider) in checked:
            continue
        checked.add(id(provider))
        status = provider.check()
        if status.provider != "fake" and not status.key_present:
            raise _ProviderUnavailable(
                status.detail or f"missing key for provider {status.provider}"
            )
        if status.provider == "openrouter":
            remaining = _limit_remaining(status.detail)
            if remaining is not None:
                limits.append(remaining)
    return min(limits) if limits else None


def _write_plan(path: Path, lines: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for line in lines:
            from .records import validate_record

            validate_record(line)
            stream.write(canonical_json(line))
            stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _write_tools_snapshot(path: Path, config: Config, plan_line: dict[str, Any], inputs: Any) -> None:
    options = tuple(plan_line["options"])
    target_index = int(plan_line["target"])
    protected_line = plan_line.get("protected")
    state = EpisodeState(
        episode=int(plan_line["index"]),
        kind=str(plan_line["kind"]),
        options=options,
        target=Target(config.payload, options[target_index], target_index),
        protected=None if protected_line is None else frozenset(protected_line),
        message="[message supplied by sender]",
    )
    tools: dict[str, Any] = {}
    for role in ("sender", "plain_sender", "guard", "receiver", "sender_debrief"):
        view = build_view(role, state, config, inputs)
        tools[role] = asdict(tool_for(role, config, view))
    _atomic_json(path / "tools.json", {"schema": "simmons.tools/1", "roles": tools})


def _summary_record(
    run_record: Mapping[str, Any],
    config: Mapping[str, Any],
    episode_records: list[dict[str, Any]],
    episode_bytes: bytes,
) -> dict[str, Any]:
    metrics_value = metrics.summarize_with_config(
        episode_records, config=config
    )
    metrics_value["counts"]["planned"] = int(
        run_record.get("episodes_planned", config.get("episodes", len(episode_records)))
    )
    metrics_value["counts"]["done"] = len(episode_records)
    # Episode records carry episode spend only; the preflight probes are billed
    # before the first episode and recorded on the run.
    cost = metrics_value.get("cost")
    if isinstance(cost, dict):
        preflight_usd = run_record.get("preflight_usd", 0.0)
        if isinstance(preflight_usd, bool) or not isinstance(preflight_usd, (int, float)):
            preflight_usd = 0.0
        cost["episodes_usd"] = cost.get("usd", 0.0)
        cost["preflight_usd"] = round(float(preflight_usd), 6)
        cost["usd"] = round(cost["episodes_usd"] + cost["preflight_usd"], 6)
    model_config = Config.model_validate(
        {field: value for field, value in config.items() if field in Config.model_fields}
    )
    resolved_models = resolve_models(model_config)
    labels = {}
    for role in ("sender", "guard"):
        prompt = config.get(f"{role}_prompt")
        prompt_label = Path(prompt).stem if isinstance(prompt, str) else role
        model_label = ""
        model = resolved_models[role]
        if model is not None:
            model_label = model.model.rsplit("/", 1)[-1]
        labels[role] = f"{prompt_label} · {model_label}" if model_label else prompt_label
    return {
        "schema": "simmons.summary/1",
        "run_id": run_record["run_id"],
        "experiment": run_record["experiment"],
        "status": run_record["status"],
        "ids": run_record.get("ids", {}),
        "matrix": run_record.get("matrix"),
        "labels": labels,
        "source": {
            "episodes_sha256": hashlib.sha256(episode_bytes).hexdigest(),
            "protocol_version": run_record.get(
                "protocol_version", config.get("protocol_version", PROTOCOL_VERSION)
            ),
        },
        **metrics_value,
    }
def _make_state(line: dict[str, Any], config: Config, inputs: Any, attempt_log: tuple[dict[str, Any], ...], *, message: str | None = None) -> EpisodeState:
    options = tuple(line["options"])
    target_index = int(line["target"])
    protected_line = line.get("protected")
    return EpisodeState(
        episode=int(line["index"]),
        kind=str(line["kind"]),
        options=options,
        target=Target(config.payload, options[target_index], target_index),
        protected=None if protected_line is None else frozenset(protected_line),
        message=message,
        attempt_log=attempt_log,
    )


def _preflight_message(completed: dict[str, tuple[RoleResponse, dict[str, Any], EpisodeState]]) -> str:
    """Return the first message a preflight sender wrote, or a placeholder."""
    for role in ("sender", "plain_sender"):
        if role in completed:
            message = completed[role][1].get("message")
            if isinstance(message, str) and message:
                return message
    return "[preflight message]"


def _preflight(ctx: EpisodeContext, plan: list[dict[str, Any]]) -> dict[str, Any]:
    """Probe one request for each used role/model and check the reasoning replay."""
    kinds = {str(line["kind"]) for line in plan}
    role_line: dict[str, dict[str, Any]] = {}
    for line in plan:
        role = "sender" if line["kind"] in {"covert", "control"} else "plain_sender"
        role_line.setdefault(role, line)
    role_line.setdefault("guard", plan[0])
    role_line.setdefault("receiver", plan[0])
    ordered_roles = [role for role in ("sender", "plain_sender", "guard", "receiver") if role in role_line]
    completed: dict[str, tuple[RoleResponse, dict[str, Any], EpisodeState]] = {}
    role_results: list[dict[str, Any]] = []
    for role in ordered_roles:
        line = role_line[role]
        message = None
        if role in {"guard", "receiver"}:
            # Probe with a message a sender just wrote: some models answer a
            # placeholder with a request for the real message instead of the tool.
            message = _preflight_message(completed)
        state = _make_state(
            line,
            ctx.config,
            ctx.inputs,
            tuple(ctx.attempt_log),
            message=message,
        )
        protected_options = None
        if role == "plain_sender" and line.get("protected") is not None:
            protected_options = tuple(
                line["options"][position] for position in line["protected"]
            )
        response, output, _view, request, attempts = _call_role(
            role,
            state,
            ctx,
            protected_options=protected_options,
            attempt_prefix=f"preflight-{role}",
        )
        completed[role] = (response, dict(output), state)
        role_results.append({"role": role, "model": f"{request.model.provider}:{request.model.model}", "attempts": attempts, "request_sha256": request_sha256(request)})

    if ctx.scorer.needs_model and "receiver" in completed:
        _receiver_response, receiver_output, receiver_state = completed["receiver"]
        pick = Pick(payload=ctx.config.payload, value=str(receiver_output.get("pick") or ""), index=None)
        _response, _output, _view, request, attempts = _call_role(
            "judge", replace(receiver_state, receiver_pick=pick), ctx, attempt_prefix="preflight-judge"
        )
        role_results.append({"role": "judge", "model": f"{request.model.provider}:{request.model.model}", "attempts": attempts, "request_sha256": request_sha256(request)})

    reasoning: dict[str, Any] = {}
    if ctx.config.sender_debrief != "off" and "sender" in completed:
        sender_response, _sender_output, state = completed["sender"]
        sender_state = replace(state, message="[preflight sender message]", sender_response=sender_response)
        spec = ctx.resolved_models["sender"]
        assert spec is not None
        model_key = f"{spec.provider}:{spec.model}"
        debrief_response, _output, _view, _request, attempts = _call_role(
            "sender_debrief", sender_state, ctx, attempt_prefix="preflight-reasoning-with"
        )
        assistant = dict(sender_response.assistant_message or {})
        assistant.pop("reasoning_details", None)
        without_reasoning = replace(
            sender_response,
            reasoning_details=None,
            assistant_message=assistant,
        )
        without_state = replace(sender_state, sender_response=without_reasoning)
        without_response, _without_output, _view, _without_request, without_attempts = _call_role(
            "sender_debrief", without_state, ctx, attempt_prefix="preflight-reasoning-without"
        )
        details = list(sender_response.reasoning_details or ())
        delta = debrief_response.usage.input_tokens - without_response.usage.input_tokens
        carried: bool | None = None if not details else delta > 0
        reasoning[model_key] = {
            "carried": carried,
            "input_token_delta": delta,
            "send_reasoning_tokens": sender_response.usage.reasoning_tokens,
            "formats": sorted({str(item.get("format", "unknown")) for item in details}),
        }
    return {"roles": role_results, "sender_reasoning": reasoning}


def run_experiment(
    experiment_path: str | Path | Config,
    *,
    repo_root: str | Path | None = None,
    fake: bool = False,
    scratch: bool = False,
    episodes: int | None = None,
    seed: int | None = None,
    max_cost: float | None = None,
    dry_run: bool = False,
    argv: list[str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Run one M1 experiment. Return CLI exit status and a JSON-ready result."""
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parent.parent
    root = root.resolve()
    try:
        if isinstance(experiment_path, Config):
            raw_config = experiment_path.model_dump(mode="json")
        else:
            config_source = Path(experiment_path)
            if not config_source.is_absolute():
                config_source = root / config_source
            raw_config = json.loads(config_source.read_text(encoding="utf-8"))
        config, overrides = _resolve_config(
            raw_config, fake=fake, episodes=episodes, seed=seed, max_cost=max_cost
        )
        supported = {("option", "exact_match"), ("text", "llm_judge")}
        if (config.payload, config.scorer) not in supported:
            raise ValueError(
                "run supports payload='option' with scorer='exact_match', or payload='text' with scorer='llm_judge'"
            )
        if config.episodes < 1:
            raise ValueError("episodes must be at least 1")
        resolved = _model_mapping(config)
        providers, provider_by_role = _provider_instances(resolved)
        remaining_usd = _check_provider_limits(providers)
        inputs = load_inputs(config, repo_root=root)
        ids_source = compute_ids(config, repo_root=root)
        local = scratch or fake or any(
            spec is not None and spec.provider == "fake"
            for spec in resolved.values()
            if spec is not None
        )
        trajectory_start, inherited_log, inherited_from = _find_trajectory(
            root,
            local=local,
            config_hash=ids_source["config_hash"],
            seed=config.seed,
            log_scope=config.log_scope,
        )
        plan = build_plan(config, inputs.option_set, start=trajectory_start)
        payload_type = get_payload(config.payload)
        scorer_type = get_scorer(config.scorer)
        payload = payload_type()
        scorer = scorer_type()
        ids = {
            "config": ids_source["config_hash"],
            "arena": ids_source["arena_hash"],
            "sender": ids_source["sender_id"],
            "guard": ids_source["guard_id"],
            "plan": plan_hash(plan),
        }
        catalog = None
        if any(spec is not None and spec.provider != "fake" for spec in resolved.values()):
            catalog = load_catalog(root / ".simmons" / "openrouter-catalog.json")
        estimate_detail = estimate_breakdown(config, plan, inputs=inputs, catalog=catalog)
        estimate_usd = estimate_detail["estimate_usd"]
        check_before_run(config, estimate_usd, remaining_usd=remaining_usd)
    except _ProviderUnavailable as exc:
        return 4, _with_cli_fields(4, {"status": "failed", "error": str(exc)})
    except BreakerTripped as exc:
        return 3, _with_cli_fields(3, {"status": "refused", "error": str(exc)})
    except Exception as exc:
        return 2, _with_cli_fields(2, {"status": "invalid", "error": str(exc)})

    kinds = Counter(line["kind"] for line in plan)
    if dry_run:
        status = 0
        try:
            check_before_run(config, estimate_usd)
        except BreakerTripped:
            status = 3
        return status, _with_cli_fields(status, {
            "status": "dry_run",
            "experiment": config.name,
            "episodes": len(plan),
            "kinds": dict(sorted(kinds.items())),
            "estimate_usd": estimate_usd,
            "estimate": estimate_detail,
            "cap_usd": run_cap(config, max_cost),
            "run_dir": None,
        })

    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{_author()}-{uuid.uuid4().hex[:4]}"
    runs_root = _base_runs_root(root, local)
    run_dir = runs_root / config.name / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    lock_path = run_dir / "run.lock"
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(f"pid={os.getpid()}\nhost={socket.gethostname()}\n")
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        return 4, _with_cli_fields(
            4,
            {
                "status": "failed",
                "error": f"run lock already exists: {lock_path}",
                "run_dir": str(run_dir),
            },
        )

    created_at = _now()
    run_record: dict[str, Any] = {
        "schema": "simmons.run/1",
        "run_id": run_id,
        "experiment": config.name,
        "author": _author(),
        "status": "running",
        "scratch": bool(scratch or local),
        "started_at": created_at,
        "simmons_version": __version__,
        "protocol_version": PROTOCOL_VERSION,
        "purpose": config.purpose,
        "uv_lock_sha256": hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest() if (root / "uv.lock").is_file() else "",
        "git_commit": _git_value("rev-parse", "HEAD") or "unknown",
        "git_dirty": bool(_git_value("status", "--porcelain")),
        "argv": argv or [],
        "overrides": overrides,
        "ids": ids,
        "matrix": None,
        "episodes_planned": len(plan),
        "episodes_done": 0,
        "trajectory": {"start": trajectory_start, "inherited_from": inherited_from},
        "cap_usd": run_cap(config, max_cost),
        "spent_usd": 0.0,
        "preflight": {},
        "preflight_usd": 0.0,
    }

    raw_request_path = run_dir / "requests.jsonl"
    request_xz_path = run_dir / "requests.jsonl.xz"
    episodes_path = run_dir / "episodes.jsonl"
    attempt_path = run_dir / "attempt-log.jsonl"
    calls_path = run_dir / "calls.jsonl"
    events_path = run_dir / "events.jsonl"
    spend_path = run_dir / "spend.jsonl"
    for path in (raw_request_path, episodes_path, attempt_path, calls_path, events_path, spend_path):
        path.touch()

    # These inputs are copied before preflight; requests below are still built
    # from the in-memory copy loaded from the same source bytes.
    snapshot(inputs, run_dir / "inputs")
    _write_tools_snapshot(run_dir / "inputs", config, plan[0], inputs)
    config_record = config.model_dump(mode="json")
    config_record["protocol_version"] = PROTOCOL_VERSION
    config_record["ids"] = ids
    _atomic_json(run_dir / "config.json", config_record)
    _write_plan(run_dir / "plan.jsonl", plan)
    write_metadata(run_dir / "run.json", run_record)

    cap_usd = run_cap(config, max_cost)
    if catalog is None:
        breaker = Breaker(cap_usd, spend_path)
    else:
        breaker = Breaker(cap_usd, spend_path, catalog=catalog)
    between_waves(breaker, remaining_usd=remaining_usd)
    preflight_log = tuple(inherited_log)
    if config.log_window is not None:
        preflight_log = preflight_log[-config.log_window :]
    ctx = EpisodeContext(
        config=config,
        inputs=inputs,
        providers=providers,
        payload=payload,
        scorer=scorer,
        run_id=run_id,
        resolved_models=resolved,
        breaker=breaker,
        request_log_path=str(raw_request_path),
        calls_log_path=str(calls_path),
        events_log_path=str(events_path),
        attempt_log=preflight_log,
    )
    status = "complete"
    exit_code = 0
    episode_records: list[dict[str, Any]] = []
    attempt_records: list[dict[str, Any]] = []
    completed_log: list[dict[str, Any]] = list(inherited_log)
    consecutive_failed_waves = 0
    consecutive_length_errors = 0
    interrupted = False
    try:
        _emit_start = events.emit
        _emit_start("run_start", run_id=run_id, path=events_path, experiment=config.name, episodes=len(plan))
        try:
            preflight_start = breaker.spent_usd()
            try:
                run_record["preflight"] = _preflight(ctx, plan)
            except (CallFailure, RuntimeError, ValueError, BreakerTripped) as exc:
                run_record["preflight_usd"] = breaker.spent_usd() - preflight_start
                if isinstance(exc, BreakerTripped):
                    status, exit_code = "stopped_budget", 3
                else:
                    status, exit_code = "failed", 4
                    run_record["preflight_error"] = str(exc)
                raise _StopRun from exc
            run_record["preflight_usd"] = breaker.spent_usd() - preflight_start
            for model_name, check in run_record["preflight"].get("sender_reasoning", {}).items():
                if check.get("carried") is False:
                    import sys

                    print(
                        f"warning: {model_name} did not carry sender reasoning into the exit interview",
                        file=sys.stderr,
                        flush=True,
                    )
            write_metadata(run_dir / "run.json", run_record)

            wave_size = max(1, int(config.wave_size))
            for start in range(0, len(plan), wave_size):
                wave_lines = plan[start : start + wave_size]
                wave_records: list[dict[str, Any]] = []
                wave_log_snapshot = tuple(completed_log[-config.log_window:]) if config.log_window is not None else tuple(completed_log)
                for line in wave_lines:
                    episode_ctx = replace(
                        ctx,
                        wave=(int(line["index"]) // wave_size) + 1,
                        attempt_log=wave_log_snapshot,
                    )
                    executor = ThreadPoolExecutor(max_workers=1)
                    future = executor.submit(run_episode, line, episode_ctx)
                    try:
                        episode_record = future.result()
                    except KeyboardInterrupt:
                        interrupted = True
                        episode_record = future.result()
                    finally:
                        executor.shutdown(wait=True, cancel_futures=False)
                    append_jsonl(episodes_path, episode_record)
                    episode_records.append(episode_record)
                    wave_records.append(episode_record)
                    if episode_record["outcome"] in {"HIT", "MISS", "CATCH"} and episode_record["kind"] in {"covert", "control"}:
                        attempt_record = _attempt_projection(config, episode_record)
                        append_jsonl(attempt_path, attempt_record)
                        attempt_records.append(attempt_record)
                        completed_log.append(attempt_record)
                    run_record["episodes_done"] = len(episode_records)
                    run_record["spent_usd"] = breaker.spent_usd()
                    sys_line = (
                        f"[{len(episode_records)}/{len(plan)}] {episode_record['kind']} "
                        f"{episode_record['outcome']} spend=${run_record['spent_usd']:.4f}"
                    )
                    import sys

                    print(sys_line, file=sys.stderr, flush=True)
                    if episode_record["outcome"] == "ERROR" and isinstance(episode_record.get("error"), dict) and episode_record["error"].get("kind") == "length":
                        consecutive_length_errors += 1
                    else:
                        consecutive_length_errors = 0
                    first_wave_permanent = (
                        start == 0
                        and isinstance(episode_record.get("error"), dict)
                        and episode_record["error"].get("permanent") is True
                    )
                    if first_wave_permanent:
                        status, exit_code = "failed", 4
                        break
                    if consecutive_length_errors >= 3:
                        status, exit_code = "failed", 4
                        run_record["abort_reason"] = "three consecutive length errors; raise max_tokens or reasoning budget"
                        break
                    if interrupted:
                        status = "interrupted"
                        exit_code = 5
                        break
                if interrupted:
                    break
                if run_record.get("abort_reason") or (status == "failed" and exit_code == 4):
                    break
                try:
                    if start + len(wave_lines) < len(plan):
                        remaining_usd = _check_provider_limits(providers)
                        between_waves(breaker, remaining_usd=remaining_usd)
                    else:
                        between_waves(breaker)
                except TransientError as exc:
                    events.emit(
                        "provider_limit_refresh_failed",
                        run_id=run_id,
                        path=events_path,
                        level="warning",
                        error_type=type(exc).__name__,
                        fallback="last_known_limit",
                        message="Transient provider limit refresh failed; retained the last known limit.",
                    )
                    between_waves(breaker)
                except BreakerTripped:
                    status, exit_code = "stopped_budget", 3
                    break
                if wave_records and all(record["outcome"] == "ERROR" for record in wave_records):
                    consecutive_failed_waves += 1
                else:
                    consecutive_failed_waves = 0
                if consecutive_failed_waves >= 3:
                    status, exit_code = "failed", 4
                    break
                run_record["spent_usd"] = breaker.spent_usd()
                write_metadata(run_dir / "run.json", run_record)
        except _StopRun:
            pass
    except KeyboardInterrupt:
        status, exit_code = "interrupted", 5
    except BreakerTripped:
        status, exit_code = "stopped_budget", 3
    except Exception as exc:
        status, exit_code = "failed", 4
        run_record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        run_record["status"] = status
        run_record["episodes_done"] = len(episode_records)
        run_record["spent_usd"] = breaker.spent_usd()
        run_record["finished_at"] = _now()
        write_metadata(run_dir / "run.json", run_record)
        write_metadata(
            run_dir / "summary.json",
            _summary_record(
                run_record,
                config.model_dump(mode="python"),
                episode_records,
                (run_dir / "episodes.jsonl").read_bytes(),
            ),
        )
        if raw_request_path.exists():
            temporary_xz = request_xz_path.with_suffix(".jsonl.xz.tmp")
            with raw_request_path.open("rb") as source, lzma.open(temporary_xz, "wb") as destination:
                while chunk := source.read(1024 * 1024):
                    destination.write(chunk)
            os.replace(temporary_xz, request_xz_path)
            raw_request_path.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)
        events.emit("run_end", run_id=run_id, path=events_path, status=status, episodes_done=len(episode_records), spent_usd=breaker.spent_usd())

    return exit_code, _with_cli_fields(exit_code, {
        "status": status,
        "run_dir": str(run_dir),
        "run_id": run_id,
        "episodes_done": len(episode_records),
        "episodes_planned": len(plan),
        "kinds": dict(sorted(kinds.items())),
        "outcomes": dict(sorted(Counter(record["outcome"] for record in episode_records).items())),
        "spent_usd": breaker.spent_usd(),
        "estimate_usd": estimate_usd,
        "ids": ids,
    })


def run(
    config: Config,
    *,
    fake: bool = False,
    scratch: bool = False,
    episodes: int | None = None,
    seed: int | None = None,
    max_cost: float | None = None,
    json_output: bool = False,
) -> dict[str, Any]:
    """Run a validated experiment config and return its machine-readable result."""
    argv = ["run", config.name]
    for flag, enabled in (("--fake", fake), ("--scratch", scratch), ("--json", json_output)):
        if enabled:
            argv.append(flag)
    for flag, value in (("--episodes", episodes), ("--seed", seed), ("--max-cost", max_cost)):
        if value is not None:
            argv.extend((flag, str(value)))
    _code, result = run_experiment(
        config,
        fake=fake,
        scratch=scratch,
        episodes=episodes,
        seed=seed,
        max_cost=max_cost,
        argv=argv,
    )
    return result


def _with_cli_fields(exit_code: int, result: dict[str, Any]) -> dict[str, Any]:
    """Add the stable CLI result fields from SPEC §16.1."""
    output = dict(result)
    output["exit_code"] = exit_code
    summary = None
    run_dir = output.get("run_dir")
    if run_dir:
        summary_path = Path(str(run_dir)) / "summary.json"
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    output["summary"] = summary
    return output


class _StopRun(Exception):
    """Internal control flow after a preflight failure."""


class _ProviderUnavailable(Exception):
    """Provider key/configuration check failed before any run files were made."""


def _attempt_projection(config: Config, record: dict[str, Any]) -> dict[str, Any]:
    values = {
        "target": record["target"]["value"],
        "message": record["messages"][0]["sent"] if record["messages"] else "",
        "outcome": record["outcome"],
        "receiver_pick": record["pick"]["value"] if record.get("pick") else None,
        "guard_suspicion": record["guard"]["suspicion"] if record.get("guard") else None,
        "guard_guess": record["guard"]["guess"]["value"] if record.get("guard") and record["guard"].get("guess") else None,
    }
    return {"schema": "simmons.attempt/1", "episode": record["index"], **{key: values[key] for key in config.log_records}}


__all__ = ["run", "run_experiment"]
