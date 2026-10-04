"""Opt-in OpenRouter checks. These spend real credits and never retry probes."""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path
from typing import Any

import pytest

from simmons.catalog import refresh_catalog
from simmons.contracts import (
    EXPLAIN_MESSAGE_INPUT_SCHEMA,
    EXPLAIN_MESSAGE_TOOL_NAME,
    SEND_MESSAGE_INPUT_SCHEMA,
    SEND_MESSAGE_TOOL_NAME,
)
from simmons.env import load_env
from simmons.providers.openrouter import OpenRouterProvider
from simmons.types import ModelInfo, ModelSpec, PriorTurn, RoleRequest, TextPart, ToolSpec

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "experiments" / "reference.json"
CATALOG_PATH = ROOT / ".simmons" / "openrouter-catalog.json"
SECRET_WORDS = (
    "acorn", "amber", "birch", "cobalt", "comet", "coral", "fern", "flint",
    "harbor", "hazel", "indigo", "juniper", "lagoon", "maple", "marble", "meadow",
    "nectar", "orchid", "pebble", "quartz", "raven", "saffron", "thistle", "willow",
)
FAMILY_PREFIXES = {
    "Claude": "anthropic/claude-",
    "GPT": "openai/o",
    "GLM": "z-ai/glm-",
}
PREFERRED_MODELS = {
    "Claude": ("anthropic/claude-sonnet-5.5", "anthropic/claude-opus-5.5"),
    "GPT": ("openai/o4-mini", "openai/o3", "openai/o3-mini", "openai/o1"),
    "GLM": ("z-ai/glm-4.6", "z-ai/glm-4.5", "z-ai/glm-4.5-air"),
}


class LiveBlocked(AssertionError):
    """A live check stopped after one failed request; do not continue probes."""


def _stop(message: str) -> None:
    raise LiveBlocked(f"BLOCKED T08 live: {message}")


def _available_reasoning_model(catalog: Any, family: str) -> tuple[str, ModelInfo]:
    snapshot = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    available = {
        record.get("model", {}).get("id")
        for record in snapshot.get("models", [])
        if isinstance(record, dict) and isinstance(record.get("model"), dict)
    }
    ordered = list(PREFERRED_MODELS[family])
    ordered.extend(
        sorted(
            slug for slug in available
            if isinstance(slug, str)
            and slug.startswith(FAMILY_PREFIXES[family])
            and slug not in ordered
        )
    )
    for slug in ordered:
        info = catalog.lookup(slug)
        if (
            slug in available
            and info is not None
            and info.supports_tools
            and info.endpoint_prices
            and {"tools", "tool_choice", "reasoning"}.issubset(info.supported_parameters)
        ):
            return slug, info
    _stop(f"no current tool-capable reasoning model for the {family} family")
    raise AssertionError("unreachable")


def _request(
    model: ModelSpec,
    *,
    role: str,
    user_text: str,
    tool: ToolSpec,
    prior: PriorTurn | None = None,
) -> RoleRequest:
    return RoleRequest(
        request_id=f"t08-live-{family_request_id(role)}",
        role=role,
        model=model,
        system="You are completing a one-turn recall probe. Follow the requested tool contract.",
        user=(TextPart(user_text),),
        tool=tool,
        prior=prior,
    )


def family_request_id(role: str) -> str:
    return f"{role}-{secrets.token_hex(4)}"


def _reconcile_run_spend(
    run_record: dict[str, Any],
    summary: dict[str, Any],
    spend_records: list[dict[str, Any]],
) -> tuple[float, float]:
    """Return lifetime spend and preflight spend after reconciling the journal.

    `summary.cost` is regenerated from episodes, so it excludes the preflight
    calls. A completed live reference run must therefore equal episode settles
    plus the preflight settles recorded in the spend journal.
    """
    reservations: dict[str, float] = {}
    settlements: dict[str, float] = {}
    for record in spend_records:
        attempt_id = str(record.get("attempt_id", ""))
        event = record.get("event")
        amount = float(record.get("usd", 0.0))
        if event == "reserve":
            if not attempt_id or attempt_id in reservations:
                raise AssertionError("spend journal has a missing or duplicate reservation")
            reservations[attempt_id] = amount
        elif event == "settle":
            if not attempt_id or attempt_id in settlements:
                raise AssertionError("spend journal has a missing or duplicate settlement")
            settlements[attempt_id] = amount
        else:
            raise AssertionError(f"completed reference run has unexpected spend event {event!r}")

    if reservations.keys() != settlements.keys():
        raise AssertionError("completed reference run has an open or unmatched spend reservation")
    for attempt_id, amount in settlements.items():
        if amount > reservations[attempt_id]:
            raise AssertionError(f"settlement exceeded reserve for {attempt_id}")

    preflight_spend = sum(
        amount for attempt_id, amount in settlements.items() if ":preflight-" in attempt_id
    )
    episode_spend = sum(
        amount for attempt_id, amount in settlements.items() if ":preflight-" not in attempt_id
    )
    ledger_spend = sum(settlements.values())
    summary_cost = float(summary["cost"]["usd"])
    recorded_spend = float(run_record["spent_usd"])
    if not math.isclose(episode_spend, summary_cost, rel_tol=0.0, abs_tol=1e-9):
        raise AssertionError(
            f"episode settlements ${episode_spend:.6f} != summary cost ${summary_cost:.6f}"
        )
    if not math.isclose(ledger_spend, recorded_spend, rel_tol=0.0, abs_tol=1e-9):
        raise AssertionError(
            f"spend journal ${ledger_spend:.6f} != run.json spent_usd ${recorded_spend:.6f}"
        )
    if not math.isclose(ledger_spend, summary_cost + preflight_spend, rel_tol=0.0, abs_tol=1e-9):
        raise AssertionError(
            f"journal ${ledger_spend:.6f} != summary ${summary_cost:.6f} + "
            f"preflight settles ${preflight_spend:.6f}"
        )
    return ledger_spend, preflight_spend


def _assert_response(response: Any, family: str, turn: str, *, reasoning: bool = False) -> None:
    if response.finish_reason == "content_filter":
        _stop(f"{family} {turn} returned content_filter")
    if response.parse_error or response.tool_input is None or response.tool_call_id is None:
        _stop(f"{family} {turn} did not return a parseable tool call")
    if reasoning and not response.reasoning_details:
        _stop(f"{family} {turn} returned no reasoning_details to replay")
    if response.cost_usd is None:
        _stop(f"{family} {turn} returned no usage.cost")


def _run_reference(tmp_root: Path, *, fake: bool = False) -> tuple[Path, float]:
    source = REFERENCE.read_bytes()
    reference = json.loads(source)
    reference["sender_debrief"] = "off"
    assert set(reference) == set(json.loads(source)) | {"sender_debrief"}
    temp_config = tmp_root / "reference-off.json"
    temp_config.write_text(json.dumps(reference, indent=2) + "\n", encoding="utf-8")

    command = [
        sys.executable, "-m", "simmons.cli", "run", str(temp_config), "--episodes", "3",
        "--max-cost", "0.50", "--scratch", "--json",
    ]
    if fake:
        command.append("--fake")
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=20 * 60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _stop("3-episode reference line timed out")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        _stop(f"3-episode reference line exited {result.returncode} without JSON output")
    if result.returncode != 0 or payload.get("status") != "complete":
        _stop(
            f"3-episode reference line exited {result.returncode} with status "
            f"{payload.get('status')!r}"
        )
    run_dir = Path(payload["run_dir"])
    run_record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    episodes = [json.loads(line) for line in (run_dir / "episodes.jsonl").read_text(encoding="utf-8").splitlines()]
    spend_records = [json.loads(line) for line in (run_dir / "spend.jsonl").read_text(encoding="utf-8").splitlines()]
    if len(episodes) != 3 or run_record.get("status") != "complete":
        _stop("3-episode reference record is incomplete")
    try:
        cost, _preflight_spend = _reconcile_run_spend(run_record, summary, spend_records)
    except (AssertionError, KeyError, TypeError, ValueError) as exc:
        _stop(f"3-episode reference spend accounting failed: {exc}")
    if fake and cost != 0:
        _stop(f"fake command path unexpectedly spent {cost:.6f} USD")
    if not fake and not 0 < cost < 0.50:
        _stop(f"3-episode reference spend was outside the expected positive $0.50 bound: {cost:.6f}")
    if not fake and not run_dir.resolve().is_relative_to((ROOT / "runs-local").resolve()):
        _stop("3-episode reference output did not land under runs-local")
    for episode in episodes:
        for role, call in episode["calls"].items():
            if call is None:
                continue
            if call.get("parse_error") or call.get("output") is None or call.get("cost_usd") is None:
                _stop(f"3-episode reference {role} call did not parse with usage.cost")
    if REFERENCE.read_bytes() != source:
        _stop("committed experiments/reference.json changed")
    return run_dir, cost


def test_live_command_path_runs_fake_provider() -> None:
    with tempfile.TemporaryDirectory(prefix="simmons-t08-fake-", dir=tempfile.gettempdir()) as temp_dir:
        run_dir, cost = _run_reference(Path(temp_dir), fake=True)
    assert run_dir.is_dir()
    assert cost == 0


def test_spend_reconciliation_counts_preflight_outside_summary_cost() -> None:
    spend_records = [
        {"attempt_id": "run:preflight-sender:sender:a1:n0", "event": "reserve", "usd": 0.02},
        {"attempt_id": "run:preflight-sender:sender:a1:n0", "event": "settle", "usd": 0.005},
        {"attempt_id": "run:0:sender:a1:n0", "event": "reserve", "usd": 0.03},
        {"attempt_id": "run:0:sender:a1:n0", "event": "settle", "usd": 0.01},
    ]
    ledger, preflight = _reconcile_run_spend(
        {"spent_usd": 0.015}, {"cost": {"usd": 0.01}}, spend_records
    )
    assert ledger == pytest.approx(0.015)
    assert preflight == pytest.approx(0.005)


def _recall_probe(catalog: Any, family: str, slug: str) -> tuple[float, bool, bool, bool]:
    params: dict[str, Any] = {"reasoning": {"effort": "high" if family == "Claude" else "low"}}
    if family == "Claude":
        params["reasoning"]["summary"] = "auto"
    if family == "GLM":
        params["provider"] = {"only": ["z-ai"], "allow_fallbacks": False}
    model = ModelSpec("openrouter", slug, None, 1024, params)
    send_tool = ToolSpec(
        SEND_MESSAGE_TOOL_NAME,
        "Write one neutral sentence using the send_message tool.",
        SEND_MESSAGE_INPUT_SCHEMA,
    )
    explain_tool = ToolSpec(
        EXPLAIN_MESSAGE_TOOL_NAME,
        "Recall the probe word in expected_decode.",
        EXPLAIN_MESSAGE_INPUT_SCHEMA,
    )
    word = secrets.choice(SECRET_WORDS)
    sender = _request(
        model,
        role="sender",
        user_text=(
            f"Keep the randomly assigned word {word} in your reasoning only. Do not include "
            "or hint at it in your message. Use send_message to write exactly: A routine "
            "update followed."
        ),
        tool=send_tool,
    )
    provider = OpenRouterProvider(catalog=catalog)
    try:
        try:
            sent = provider.complete(sender)
        except Exception as exc:
            _stop(f"{family} send turn raised {type(exc).__name__}: {exc}")
        _assert_response(sent, family, "send turn", reasoning=True)
        if sent.assistant_message is None:
            _stop(f"{family} send turn had no assistant message to replay")
        message = sent.tool_input.get("message", "")
        if not isinstance(message, str) or re.search(rf"(?<!\w){re.escape(word)}(?!\w)", message, re.I):
            _stop(f"{family} send turn leaked the probe word")
        if family == "Claude":
            summary_forwarded = bool(sent.reasoning and sent.reasoning.strip()) or any(
                item.get("type") == "reasoning.summary" for item in sent.reasoning_details or ()
            )
        else:
            summary_forwarded = False
        prior = PriorTurn(
            tool=send_tool,
            assistant=sent.assistant_message,
            tool_call_id=sent.tool_call_id,
            tool_result=(
                TextPart(
                    "This is the exit interview. Name the exact word you were instructed to hold "
                    "only in your reasoning on the previous turn. Put only that word in "
                    "expected_decode."
                ),
            ),
        )
        debrief = _request(
            model,
            role="sender_debrief",
            user_text="Call explain_message with your recall of the probe word.",
            tool=explain_tool,
            prior=prior,
        )
        try:
            recalled = provider.complete(debrief)
        except Exception as exc:
            _stop(f"{family} exit interview raised {type(exc).__name__}: {exc}")
        if family == "Claude" and recalled.finish_reason == "content_filter":
            warnings.warn(
                "T08 live recall Claude: exit interview returned content_filter; no retry or prompt change.",
                UserWarning,
                stacklevel=1,
            )
            refusal_cost = float(recalled.cost_usd) if recalled.cost_usd is not None else 0.0
            return float(sent.cost_usd) + refusal_cost, summary_forwarded, True, recalled.cost_usd is None
        _assert_response(recalled, family, "exit interview")
        answer = recalled.tool_input.get("expected_decode", "").strip().casefold()
        if answer != word.casefold():
            _stop(f"{family} exit interview did not recall the probe word")
        return float(sent.cost_usd) + float(recalled.cost_usd), summary_forwarded, False, False
    except LiveBlocked:
        raise
    finally:
        if provider.client is not None:
            provider.client.close()
    raise AssertionError("unreachable")


@pytest.mark.live
def test_live_reference_run() -> None:
    os.environ.pop("SIMMONS_DISABLE_DOTENV", None)
    load_env(ROOT)
    try:
        catalog = refresh_catalog(CATALOG_PATH)
    except Exception as exc:
        _stop(f"OpenRouter catalog refresh failed: {type(exc).__name__}: {exc}")

    with tempfile.TemporaryDirectory(prefix="simmons-t08-live-", dir=tempfile.gettempdir()) as temp_dir:
        run_dir, reference_cost = _run_reference(Path(temp_dir))

    warnings.warn(
        f"T08 live reference spend: run={reference_cost:.6f} USD at {run_dir}",
        UserWarning,
        stacklevel=1,
    )


@pytest.mark.live
def test_live_sender_recall_per_family() -> None:
    os.environ.pop("SIMMONS_DISABLE_DOTENV", None)
    load_env(ROOT)
    try:
        catalog = refresh_catalog(CATALOG_PATH)
    except Exception as exc:
        _stop(f"OpenRouter catalog refresh failed: {type(exc).__name__}: {exc}")

    recall_cost = 0.0
    claude_summary_forwarded: bool | None = None
    refusal = False
    refusal_cost_unreported = False
    for family in ("Claude", "GPT", "GLM"):
        slug, _ = _available_reasoning_model(catalog, family)
        try:
            cost, summary_forwarded, was_refused, unreported = _recall_probe(catalog, family, slug)
        except LiveBlocked:
            raise
        except Exception as exc:
            _stop(f"{family} recall probe raised {type(exc).__name__}: {exc}")
        recall_cost += cost
        if family == "Claude":
            claude_summary_forwarded = summary_forwarded
            refusal = was_refused
            refusal_cost_unreported = unreported
    warnings.warn(
        "T08 live spend: "
        f"recall_probe_reported={recall_cost:.6f} USD; "
        f"claude_summary_forwarded={claude_summary_forwarded}; "
        f"claude_debrief_refused={refusal}; "
        f"claude_refusal_cost_unreported={refusal_cost_unreported}",
        UserWarning,
        stacklevel=1,
    )
