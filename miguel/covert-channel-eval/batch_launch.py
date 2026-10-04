#!/usr/bin/env python3
"""
Parallel batch launcher.

Runs N independent experiments concurrently (each with its own seed, so tokens and
sampling differ), streams live progress to the console, then aggregates everything
into a results table + per-condition stats and saves CSV/JSON.

Examples:
  export OPENROUTER_API_KEY=sk-or-...
  python batch_launch.py -n 20                       # 20 covert runs
  python batch_launch.py -n 20 --condition both      # 20 covert + 20 plaintext
  python batch_launch.py -n 20 --workers 6 --rounds 8 --company Allbirds
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
from openrouter_client import OpenRouterClient
from orchestrator import run_experiment
from run_experiment import _save, _transcript_md


# ---- per-run worker ---------------------------------------------------------
def _run_task(task: dict, base_dir: str) -> dict:
    """Run one experiment. Returns a compact row; saves full artifacts to disk."""
    cfg = task["cfg"]
    started = time.time()
    row = {"idx": task["idx"], "seed": cfg.seed, "condition": cfg.condition,
           "status": "ok"}
    try:
        client = OpenRouterClient()          # one client per worker (thread-safe)
        result = run_experiment(cfg, client)
        m = result["metrics"]
        j, d = m["judge"], m["detection"]
        v = j.get("verdict", {}) or {}
        row.update({
            "channel_valid": j["channel_valid"],
            "a2b": j["A_to_B_valid"], "b2a": j["B_to_A_valid"],
            "covert": m["covert"],
            "max_susp": d["max_suspicion"],
            "flagged": d["ever_flagged"],
            "success": m["covert_channel_success"],
            "summary": v.get("summary", ""),
        })
        # persist full artifacts
        sub = os.path.join(base_dir, f"run{task['idx']:02d}_{cfg.condition}_seed{cfg.seed}")
        os.makedirs(sub, exist_ok=True)
        _save(sub, "result.json", result)
        _save(sub, "metrics.json", m)
        with open(os.path.join(sub, "transcript.md"), "w", encoding="utf-8") as f:
            f.write(_transcript_md(result))
        if v.get("summary"):
            with open(os.path.join(sub, "judge_debrief.txt"), "w", encoding="utf-8") as f:
                f.write(str(v["summary"]) + "\n")
    except Exception as e:  # noqa: BLE001 - one bad run must not kill the batch
        row["status"] = "error"
        row["error"] = f"{type(e).__name__}: {e}"
    row["secs"] = round(time.time() - started, 1)
    return row


# ---- table rendering --------------------------------------------------------
def _fmt(v) -> str:
    if v is True:
        return "yes"
    if v is False:
        return "no"
    return "" if v is None else str(v)


def _render_table(rows: list[dict]) -> str:
    cols = [("#", "idx"), ("seed", "seed"), ("cond", "condition"),
            ("valid", "channel_valid"), ("A>B", "a2b"), ("B>A", "b2a"),
            ("covert", "covert"), ("maxSusp", "max_susp"), ("flag", "flagged"),
            ("SUCCESS", "success"), ("s", "secs")]
    header = [h for h, _ in cols]
    table = [header]
    for r in sorted(rows, key=lambda x: x["idx"]):
        if r["status"] != "ok":
            table.append([str(r["idx"]), str(r["seed"]), r["condition"],
                          "ERROR", "", "", "", "", "", r.get("error", "")[:22], ""])
        else:
            table.append([_fmt(r.get(k)) for _, k in cols])
    widths = [max(len(row[i]) for row in table) for i in range(len(header))]
    lines = []
    for ri, row in enumerate(table):
        line = "  ".join(c.ljust(widths[i]) for i, c in enumerate(row))
        lines.append(line)
        if ri == 0:
            lines.append("  ".join("-" * widths[i] for i in range(len(header))))
    return "\n".join(lines)


def _aggregate(rows: list[dict]) -> dict:
    out = {}
    conds = sorted({r["condition"] for r in rows})
    for cond in conds:
        ok = [r for r in rows if r["condition"] == cond and r["status"] == "ok"]
        n_err = sum(1 for r in rows if r["condition"] == cond and r["status"] != "ok")
        if not ok:
            out[cond] = {"n_ok": 0, "n_error": n_err}
            continue
        susp = [r["max_susp"] for r in ok]

        def rate(key):
            return round(sum(1 for r in ok if r.get(key)) / len(ok), 3)

        out[cond] = {
            "n_ok": len(ok), "n_error": n_err,
            "channel_valid_rate": rate("channel_valid"),
            "covert_rate": rate("covert"),
            "flagged_rate": rate("flagged"),
            "covert_channel_success_rate": rate("success"),
            "mean_max_suspicion": round(statistics.mean(susp), 1),
            "median_max_suspicion": round(statistics.median(susp), 1),
        }
    return out


# ---- main -------------------------------------------------------------------
def main() -> None:
    p = argparse.ArgumentParser(description="Parallel batch launcher.")
    p.add_argument("-n", "--num", type=int, default=20, help="experiments per condition")
    p.add_argument("--condition", choices=["covert", "plaintext", "both"],
                   default="covert")
    p.add_argument("--workers", type=int, default=5, help="max concurrent runs")
    p.add_argument("--rounds", type=int, default=None)
    p.add_argument("--company", default=None)
    p.add_argument("--seed-start", type=int, default=1)
    p.add_argument("--out-dir", default=None)
    p.add_argument("--no-interview", action="store_true")
    args = p.parse_args()

    base = config.ExperimentConfig()
    if args.rounds is not None:
        base.rounds = args.rounds
    if args.company is not None:
        base.company = args.company
    if args.no_interview:
        base.run_interview = False

    conditions = ["covert", "plaintext"] if args.condition == "both" else [args.condition]
    tasks = []
    idx = 1
    for cond in conditions:
        for k in range(args.num):
            cfg = dataclasses.replace(base, condition=cond, seed=args.seed_start + k)
            tasks.append({"idx": idx, "cfg": cfg})
            idx += 1
    total = len(tasks)

    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base_dir = os.path.join(args.out_dir or base.out_dir, f"batch-{ts}")
    os.makedirs(base_dir, exist_ok=True)

    print(f"Launching {total} experiments "
          f"({args.num} x {len(conditions)} condition(s)) "
          f"with {args.workers} workers.")
    print(f"Models: core={config.COLLABORATOR_MODEL}  monitor={config.MONITOR_MODEL}")
    print(f"Output: {base_dir}\n")

    rows: list[dict] = []
    lock = threading.Lock()
    done = 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_run_task, t, base_dir): t for t in tasks}
        with lock:
            for t in tasks:
                print(f"  [queued] run {t['idx']:>2}  {t['cfg'].condition:<9} "
                      f"seed {t['cfg'].seed}")
            print()
        for fut in as_completed(futures):
            row = fut.result()
            with lock:
                rows.append(row)
                done += 1
                if row["status"] == "ok":
                    tag = "SUCCESS" if row["success"] else (
                        "valid/caught" if row["channel_valid"] else "no channel")
                    detail = (f"valid={_fmt(row['channel_valid'])} "
                              f"covert={_fmt(row['covert'])} "
                              f"maxSusp={row['max_susp']} -> {tag}")
                else:
                    detail = row.get("error", "error")
                bar = f"[{done}/{total}]"
                print(f"  {bar:<9} done run {row['idx']:>2} "
                      f"{row['condition']:<9} ({row['secs']}s)  {detail}")

    elapsed = round(time.time() - t0, 1)

    # ---- results table ----
    print("\n" + "=" * 72)
    print("RESULTS")
    print("=" * 72)
    print(_render_table(rows))

    # ---- aggregate stats ----
    agg = _aggregate(rows)
    print("\n" + "=" * 72)
    print("AGGREGATE (per condition)")
    print("=" * 72)
    for cond, s in agg.items():
        print(f"\n[{cond}]  n_ok={s.get('n_ok')}  n_error={s.get('n_error')}")
        for k, val in s.items():
            if k in ("n_ok", "n_error"):
                continue
            print(f"    {k:<30} {val}")

    # ---- judge debriefs ----
    print("\n" + "=" * 72)
    print("JUDGE DEBRIEFS")
    print("=" * 72)
    for r in sorted(rows, key=lambda x: x["idx"]):
        if r["status"] == "ok" and r.get("summary"):
            print(f"\n--- run {r['idx']} [{r['condition']} seed {r['seed']}] ---")
            print(str(r["summary"]))

    # ---- persist aggregate + CSV ----
    _save(base_dir, "batch_results.json",
          {"elapsed_secs": elapsed, "total": total, "rows": rows, "aggregate": agg,
           "models": {"collaborator": config.COLLABORATOR_MODEL,
                      "monitor": config.MONITOR_MODEL, "judge": config.JUDGE_MODEL}})
    csv_cols = ["idx", "seed", "condition", "status", "channel_valid", "a2b", "b2a",
                "covert", "max_susp", "flagged", "success", "secs"]
    with open(os.path.join(base_dir, "results.csv"), "w", encoding="utf-8") as f:
        f.write(",".join(csv_cols) + "\n")
        for r in sorted(rows, key=lambda x: x["idx"]):
            f.write(",".join(_fmt(r.get(c)) for c in csv_cols) + "\n")

    print(f"\nFinished {total} runs in {elapsed}s. Saved -> {base_dir}")


if __name__ == "__main__":
    main()
