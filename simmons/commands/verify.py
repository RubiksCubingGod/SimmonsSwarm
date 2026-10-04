"""Validate the M1 invariants visible in one run directory."""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from ..records import read_jsonl, read_requests_xz, validate_record


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    command = subparsers.add_parser("verify", help="check a run's records and accounting")
    command.add_argument("run_dir", type=Path, help="run directory to verify")
    command.set_defaults(handler=_run)


def verify_run(run_dir: str | Path) -> list[str]:
    root = Path(run_dir)
    errors: list[str] = []
    try:
        run_record = json.loads((root / "run.json").read_text(encoding="utf-8"))
        if not isinstance(run_record, dict):
            raise ValueError("run.json must contain an object")
        validate_record(run_record)
    except Exception as exc:
        return [f"run.json: {exc}"]

    try:
        episodes = read_jsonl(root / "episodes.jsonl")
    except Exception as exc:
        errors.append(f"episodes.jsonl: {exc}")
        episodes = []

    for filename in ("plan.jsonl", "attempt-log.jsonl"):
        try:
            read_jsonl(root / filename)
        except Exception as exc:
            errors.append(f"{filename}: {exc}")

    try:
        summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
        if not isinstance(summary, dict):
            raise ValueError("summary.json must contain an object")
        validate_record(summary)
    except Exception as exc:
        errors.append(f"summary.json: {exc}")

    events_path = root / "events.jsonl"
    if events_path.exists():
        try:
            read_jsonl(events_path)
        except Exception as exc:
            errors.append(f"events.jsonl: {exc}")
    by_index: dict[int, int] = defaultdict(int)
    for record in episodes:
        by_index[int(record["index"])] += 1
    for index, count in sorted(by_index.items()):
        if count != 1:
            errors.append(f"episodes.jsonl: index {index} has {count} records")
    planned = run_record.get("episodes_planned")
    done = run_record.get("episodes_done")
    status = run_record.get("status")
    trajectory = run_record.get("trajectory", {})
    if not isinstance(trajectory, dict):
        errors.append("run.json: trajectory must be an object")
        trajectory_start = 0
    else:
        trajectory_start = trajectory.get("start", 0)
        if type(trajectory_start) is not int or trajectory_start < 0:
            errors.append("run.json: trajectory.start must be a non-negative integer")
            trajectory_start = 0
    if status in {"complete", "interrupted", "stopped_budget", "failed"}:
        if type(planned) is int and type(done) is int and planned >= 0 and done >= 0:
            expected_count = planned if status == "complete" else done
            if status == "complete" and done != planned:
                errors.append(f"run.json: complete run has {done} episodes_done; expected {planned}")
            if len(episodes) != expected_count:
                errors.append(f"episodes.jsonl: has {len(episodes)} records; expected {expected_count} for {status} run")
            expected_end = trajectory_start + expected_count
            for index in range(trajectory_start, expected_end):
                if index not in by_index:
                    errors.append(f"episodes.jsonl: missing expected episode index {index}")
            for index in by_index:
                if index < trajectory_start or index >= expected_end:
                    errors.append(
                        f"episodes.jsonl: index {index} is beyond the {status} run's "
                        f"completed prefix {trajectory_start}..{expected_end - 1}"
                    )
    if type(planned) is int and planned >= 0:
        plan_end = trajectory_start + planned
        for index in by_index:
            if index < trajectory_start or index >= plan_end:
                errors.append(
                    f"episodes.jsonl: index {index} is outside "
                    f"{trajectory_start}..{plan_end - 1}"
                )

    try:
        requests = read_requests_xz(root / "requests.jsonl.xz")
        request_hashes = {str(record["request_sha256"]) for record in requests}
    except Exception as exc:
        errors.append(f"requests.jsonl.xz: {exc}")
        requests = []
        request_hashes = set()

    for record in episodes:
        calls = record.get("calls", {})
        if not isinstance(calls, dict):
            errors.append(f"episode {record.get('index')}: calls is not an object")
            continue
        for name, call in calls.items():
            if not isinstance(call, dict):
                if call is not None:
                    errors.append(f"episode {record.get('index')} {name}: call is not an object")
                continue
            digest = call.get("request_sha256")
            if not isinstance(digest, str) or not digest:
                errors.append(f"episode {record.get('index')} {name}: request hash is absent or empty")
            elif digest not in request_hashes:
                errors.append(f"episode {record.get('index')} {name}: request hash is absent from requests.jsonl.xz")
        guard = record.get("guard")
        if isinstance(guard, dict) and guard.get("blocked") is True:
            if calls.get("receiver") is not None:
                errors.append(f"episode {record.get('index')}: blocked episode has an ordinary receiver call")
            if record.get("pick") is not None:
                errors.append(f"episode {record.get('index')}: blocked episode has an ordinary pick")

    calls_path = root / "calls.jsonl"
    if calls_path.exists():
        try:
            calls_log = read_jsonl(calls_path)
            for call in calls_log:
                digest = call.get("request_sha256")
                if not isinstance(digest, str) or not digest:
                    errors.append("calls.jsonl: request hash is absent or empty")
                elif digest not in request_hashes:
                    errors.append("calls.jsonl: request hash is absent from requests.jsonl.xz")
        except Exception as exc:
            errors.append(f"calls.jsonl: {exc}")

    try:
        spend = read_jsonl(root / "spend.jsonl")
        entries: dict[str, dict[str, Any]] = {}
        total = 0.0
        for row in spend:
            attempt = str(row["attempt_id"])
            event = row["event"]
            if event == "reserve":
                if attempt in entries:
                    errors.append(f"spend.jsonl: duplicate reserve for {attempt}")
                else:
                    entries[attempt] = {"reserve": row, "terminal": None}
                digest = row.get("request_sha256")
                if not isinstance(digest, str) or not digest:
                    errors.append("spend.jsonl: reserve request hash is absent or empty")
                elif digest not in request_hashes:
                    errors.append("spend.jsonl: reserve request hash is absent from requests.jsonl.xz")
            else:
                if attempt not in entries:
                    errors.append(f"spend.jsonl: {event} has no reserve for {attempt}")
                    continue
                if entries[attempt]["terminal"] is not None:
                    errors.append(f"spend.jsonl: multiple terminal records for {attempt}")
                    continue
                reserve = entries[attempt]["reserve"]
                if event == "settle" and float(row["usd"]) > float(reserve["usd"]) + 1e-12:
                    errors.append(f"spend.jsonl: settled cost exceeds reserve for {attempt}")
                if event == "unresolved" and not math.isclose(float(row["usd"]), float(reserve["usd"]), abs_tol=1e-12):
                    errors.append(f"spend.jsonl: unresolved amount differs from reserve for {attempt}")
                entries[attempt]["terminal"] = row
                total += float(row["usd"])
        for attempt, entry in entries.items():
            if entry["terminal"] is None:
                errors.append(f"spend.jsonl: reservation has no settlement or unresolved record: {attempt}")
        if not math.isclose(total, float(run_record.get("spent_usd", 0.0)), rel_tol=1e-7, abs_tol=1e-9):
            errors.append("run.json spent_usd does not match settled and unresolved spend")
    except Exception as exc:
        errors.append(f"spend.jsonl: {exc}")

    return errors


def _run(args: argparse.Namespace) -> int:
    errors = verify_run(args.run_dir)
    if errors:
        print("FAIL")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"PASS {args.run_dir}")
    return 0


__all__ = ["register", "verify_run"]
