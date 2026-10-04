"""Read a run, an episode, or one saved role request."""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from jsonschema import ValidationError

from ..records import read_jsonl, read_requests_xz, request_sha256, validate_record

_ROLES = ("sender", "guard", "receiver", "sender_debrief")
_OUTCOME_ORDER = ("CATCH", "MISS", "HIT", "ERROR")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    command = subparsers.add_parser("show", help="read a run or one episode")
    command.add_argument("run_dir", type=Path, help="run directory containing run.json")
    command.add_argument("--episode", type=int, help="show one zero-based episode index")
    command.add_argument("--role", choices=_ROLES, help="role whose saved request to show")
    command.add_argument("--prompt", action="store_true", help="show the role's exact saved request")
    command.add_argument("--sample", type=int, metavar="N", help="show N random messages beside the cover task")
    command.add_argument("--json", action="store_true", help="print machine-readable JSON")
    command.set_defaults(handler=_run)


def _run(args: argparse.Namespace) -> int:
    invalid = _validate_options(args)
    if invalid:
        print(f"show: {invalid}", file=sys.stderr)
        return 2

    run_dir: Path = args.run_dir
    try:
        run_record = _read_object(run_dir / "run.json")
        episodes = read_jsonl(run_dir / "episodes.jsonl")
    except (OSError, ValueError, ValidationError) as exc:
        print(f"show: {exc}", file=sys.stderr)
        return 2

    if args.sample is not None:
        return _show_sample(run_dir, run_record, episodes, args.sample, args.json)
    if args.episode is None:
        summary_path = run_dir / "summary.json"
        try:
            summary = _read_object(summary_path) if summary_path.is_file() else None
        except (OSError, ValueError, ValidationError) as exc:
            print(f"show: {exc}", file=sys.stderr)
            return 2
        if args.json:
            _print_json(_overview_json(run_record, summary, episodes))
        else:
            _print_overview(run_record, summary, episodes)
        return 0

    episode = next((row for row in episodes if row.get("index") == args.episode), None)
    if episode is None:
        print(f"show: episode {args.episode} is not present in {run_dir}", file=sys.stderr)
        return 2

    output = dict(episode)
    if args.prompt:
        call = episode.get("calls", {}).get(args.role)
        if call is None:
            note = _not_called_note(episode, args.role)
            output["request"] = None
            if args.json:
                _print_json(output)
            else:
                print(f"Episode {args.episode} · {episode.get('kind')} · {episode.get('outcome')}")
                print(note)
            return 0
        try:
            request, matches = _saved_request(run_dir, call)
        except (OSError, ValueError, ValidationError, KeyError) as exc:
            print(f"show: {exc}", file=sys.stderr)
            return 2
        output["request"] = request
        if args.json:
            _print_json(output)
        else:
            print(f"Episode {args.episode} · {args.role} request")
            print(json.dumps(request, ensure_ascii=False, indent=2))
            print("sha256 matches" if matches else "sha256 mismatch")
        return 0 if matches else 2

    if args.json:
        _print_json(output)
    else:
        _print_episode(episode)
    return 0


def _validate_options(args: argparse.Namespace) -> str | None:
    if args.episode is not None and args.episode < 0:
        return "--episode must be zero or greater"
    if args.sample is not None:
        if args.sample < 1:
            return "--sample must be at least 1"
        if args.episode is not None or args.role is not None or args.prompt:
            return "--sample cannot be combined with --episode, --role, or --prompt"
    if args.prompt and args.role is None:
        return "--prompt requires --role"
    if args.role is not None and not args.prompt:
        return "--role requires --prompt"
    if args.role is not None and args.episode is None:
        return "--role --prompt requires --episode"
    return None


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    validate_record(value)
    return value


def _overview_json(
    run_record: dict[str, Any], summary: dict[str, Any] | None, episodes: list[dict[str, Any]]
) -> dict[str, Any]:
    output = dict(run_record)
    output["summary"] = summary
    output["episode_count"] = len(episodes)
    output["outcomes"] = _outcomes(episodes)
    return output


def _outcomes(episodes: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(str(row.get("outcome", "UNKNOWN")) for row in episodes)
    return {name: counts[name] for name in _OUTCOME_ORDER if counts[name]} | {
        name: counts[name] for name in sorted(counts) if name not in _OUTCOME_ORDER
    }


def _print_overview(
    run_record: dict[str, Any], summary: dict[str, Any] | None, episodes: list[dict[str, Any]]
) -> None:
    experiment = run_record.get("experiment", "unknown")
    run_id = run_record.get("run_id", "unknown")
    print(f"Run: {experiment}/{run_id}")
    print(f"Status: {run_record.get('status', 'unknown')}")
    planned = run_record.get("episodes_planned", len(episodes))
    print(f"Episodes: {len(episodes)}/{planned}")
    outcome_text = ", ".join(f"{key} {value}" for key, value in _outcomes(episodes).items())
    print(f"Outcomes: {outcome_text or 'none'}")
    print(f"Spent: ${float(run_record.get('spent_usd', 0.0)):.4f}")
    rates = summary.get("rates", {}) if summary else {}
    for key, label in (
        ("hit_rate", "Hit rate"),
        ("catch_rate", "Catch rate"),
        ("false_alarm_rate", "False-alarm rate"),
    ):
        rate = rates.get(key) if isinstance(rates, dict) else None
        if isinstance(rate, dict):
            value = rate.get("value")
            fraction = f"{rate.get('num', 0)}/{rate.get('den', 0)}"
            rendered = "—" if value is None else f"{100 * float(value):.1f}%"
            print(f"{label}: {rendered} ({fraction})")


def _print_episode(episode: dict[str, Any]) -> None:
    print(f"Episode {episode.get('index')} · {episode.get('kind')} · {episode.get('outcome')}")
    target = episode.get("target")
    if isinstance(target, dict):
        print(f"Target: {target.get('value', '—')}")
    protected = episode.get("protected")
    protected_values = [item.get("value") for item in protected or [] if isinstance(item, dict)]
    print(f"Protected: {', '.join(protected_values) if protected_values else 'none'}")

    calls = episode.get("calls", {})
    guard = episode.get("guard")
    if isinstance(guard, dict):
        verdict = "blocked" if guard.get("blocked") else "passed"
        threshold = guard.get("threshold")
        threshold_text = f" at threshold {threshold}" if threshold is not None else ""
        print(f"Guard: {verdict}; suspicion {guard.get('suspicion')}{threshold_text}")
        guess = guard.get("guess")
        if isinstance(guess, dict):
            print(f"Guard guess: {guess.get('value')}")
        guard_call = calls.get("guard")
        guard_output = guard_call.get("output", {}) if isinstance(guard_call, dict) else {}
        if isinstance(guard_output, dict) and guard_output.get("reason"):
            print(f"Guard reason: {guard_output['reason']}")

    messages = episode.get("messages", [])
    for message in messages:
        if not isinstance(message, dict):
            continue
        print(f"Message {message.get('n', 0)} sent: {message.get('sent', '')}")
        if message.get("rewritten") is not None:
            print(f"Message {message.get('n', 0)} rewritten: {message['rewritten']}")
        if message.get("delivered") is not None and message.get("delivered") != message.get("sent"):
            print(f"Message {message.get('n', 0)} delivered: {message['delivered']}")

    if calls.get("receiver") is None:
        if isinstance(guard, dict) and guard.get("blocked") is True:
            print("Receiver: Not called: the guard blocked the message.")
    else:
        output = calls["receiver"].get("output", {})
        pick_record = episode.get("pick")
        pick = pick_record.get("value") if isinstance(pick_record, dict) else None
        if pick is None and isinstance(output, dict):
            pick = output.get("pick")
        print(f"Receiver pick: {pick if pick is not None else '—'}")
        if isinstance(output, dict) and output.get("reason"):
            print(f"Receiver reason: {output['reason']}")

    score = episode.get("score")
    if isinstance(score, dict):
        print(f"Score: {score.get('value')} (hit: {score.get('hit')})")
    if episode.get("error") is not None:
        print(f"Episode error: {episode['error']}")
    if episode.get("debrief_error") is not None:
        print(f"Sender debrief error: {episode['debrief_error']}")

    debrief = calls.get("sender_debrief")
    if isinstance(debrief, dict):
        output = debrief.get("output", {})
        print("Sender exit interview:")
        print(json.dumps(output, ensure_ascii=False, indent=2))


def _show_sample(
    run_dir: Path,
    run_record: dict[str, Any],
    episodes: list[dict[str, Any]],
    count: int,
    json_output: bool,
) -> int:
    try:
        cover_task = (run_dir / "inputs" / "cover-task.md").read_text(encoding="utf-8")
    except OSError as exc:
        print(f"show: {exc}", file=sys.stderr)
        return 2

    messages = [
        {"episode": episode.get("index"), "message": message["sent"]}
        for episode in episodes
        for message in episode.get("messages", [])
        if isinstance(message, dict) and isinstance(message.get("sent"), str)
    ]
    if count > len(messages):
        print(f"show: requested {count} messages, but the run has only {len(messages)}", file=sys.stderr)
        return 2
    samples = random.sample(messages, count)
    if json_output:
        _print_json({"run_id": run_record.get("run_id"), "cover_task": cover_task, "samples": samples})
        return 0

    print("Cover task:")
    print(cover_task.rstrip())
    print("\nSample messages:")
    for sample in samples:
        print(f"\nEpisode {sample['episode']}:")
        print(sample["message"])
    return 0


def _saved_request(run_dir: Path, call: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    digest = call.get("request_sha256")
    if not isinstance(digest, str) or not digest:
        raise ValueError("episode call has no request_sha256")

    rows = read_requests_xz(run_dir / "requests.jsonl.xz")
    saved = next((row for row in rows if row.get("request_sha256") == digest), None)
    if saved is None:
        raise ValueError(f"request {digest} is absent from requests.jsonl.xz")

    fields = ("model", "system", "user", "tool")
    payload = {key: saved[key] for key in fields}
    if "prior" in saved:
        payload["prior"] = saved["prior"]
    matches = request_sha256(payload) == digest

    request = {key: saved[key] for key in ("system", "user", "tool")}
    if "prior" in saved:
        request["prior"] = saved["prior"]
    request["sha256_matches"] = matches
    return request, matches


def _not_called_note(episode: dict[str, Any], role: str | None) -> str:
    if role == "receiver" and isinstance(episode.get("guard"), dict) and episode["guard"].get("blocked"):
        return "Not called: the guard blocked the message."
    return f"Not called: no {role} request is recorded for this episode."


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


__all__ = ["register"]
