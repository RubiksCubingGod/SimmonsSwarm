# Validation Report

Result Validator (`agent:validator`), 2026-10-03 21:25 to 21:36 PDT. Fresh clone of `origin/main` at `add2139b69d60c0f7682d9daa2f871193b2fac9d` (T17), in `$TMPDIR/simmons-audit`. No `.env`. Every command ran with `OPENROUTER_API_KEY`, `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` unset; the login shell exports the first two, so a validator that skips the unset is not hermetic. No live provider call was made. SS-20 had not landed when the audit ended (`HEAD..origin/main` empty at 21:35).

## Verdict
GREEN

Re-validation 2026-10-03 23:07 PDT, narrow, on a fresh clone of `origin/main` at `11a930c` (SS-20 e55df73, SS-21 11a930c), keys unset: R1 to R4 are fixed, each re-checked by its reproduction (R4's new test also mutation-checked); E-37 passes; E-02 is green. The first pass (add2139, REPAIR REQUIRED) is preserved below; each repair carries its re-validation result. Open, not repairs: E-02's 10 s wall, which the Orchestrator times on a quiet machine after the fleet closes; Atin's `sender_debrief` and Claude-refusal decision; the people's criteria (pending people).

## Completion accounting
- Scoped tickets: 17 code (SS-1 to SS-17) + T02f-b follow-up (PR#14) + SS-20 (chore) + T22, T23 (people)
- Merged and complete: 17 + T02f-b. All Lattice `done`. Every merge SHA matches run-state's landing matrix.
- Reviewed head is the merged head: 11 PRs merged with tree identical to the reviewed head; 7 squashed without a rebase, and for each `git merge-tree --write-tree <merge>^ <head>` equals the merge tree. The 6 rebases after review (T02a, T02e, T07, T08, T12, T17) carry mechanical-rebase receipts or an Orchestrator attestation (T02e: one test assertion).
- Accepted residuals: 0 recorded by Atin. Orchestrator waivers in force: T08 live lines (5-episode run `interrupted`, not `complete`; recall probe to v1.1).
- Unresolved: SS-20 (in progress), T22 and T23 (backlog, people).

## Criterion results
Out of scope (v1.1 only): E-16, E-17, E-18, E-30 to E-35, E-40 to E-43, and the v1.1 parts of E-06, E-09, E-20, E-25, E-26, E-28.

| Criterion | Owning ticket | Merged evidence | Runtime/assembled evidence (this audit) | Result |
|---|---|---|---|---|
| E-01 | T01 | PR#1 443ccf5 | `simmons --version` → `0.1.0` | PASS |
| E-02 | T01, all; SS-20 | PR#19 e55df73 | Re-validation at 11a930c: green with no keys, 551 passed, 2 deselected (live). user 5.30 s, sys 1.72 s, wall 9.15 s at load 59.8 to 69.4 (fleet busy). First pass at add2139: user 9.1 to 12.0 s. The 10 s wall is not judged here. | PASS (hermetic, green); wall pending the Orchestrator's quiet-machine timing |
| E-03 | T02f | PR#12 ee76f37 | Smoke: exit 0, `complete`, all 11 files, no `run.lock`, 20 episodes, covert 14 / plain_allowed 6, CATCH 7, HIT 5, MISS 8 | PASS |
| E-04 | T02b, T02d, T02f | golden plan test | Golden plan test passes. Two smoke runs in two clean clones differ only in `run_id`, `started_at`, `finished_at` (my field-level diff). No suite test asserts this half any more (candidate C4). | PASS |
| E-05 | T02f | PR#12 | After 9 fake and scratch runs, `runs/` does not exist; `runs-local/` gitignored. Edge case: R4. | PASS, with R4 |
| E-06 (v1.0) | T02e, T02f, T08 | PR#3, #12, #18 | `smoke-priced`: exit 3, `stopped_budget`, spent 0.04992. `max_cost_usd: 25`: validate exit 2 (`max_cost_usd`, "at most RUN_CEILING_USD (20.0)"), run exit 2. `--max-cost 21` over the default: exit 3, refused. Config 5 with `--max-cost 8`: refused. `RUN_CEILING_USD = 20.0` at `simmons/spend.py:23`, no env override. | PASS |
| E-07 | T03 | PR#11 cab74f4 | 101/101 files in `tests/fixtures/bad/` exit 2 and name a JSON path | PASS |
| E-08 | T02a, T03 | PR#4, #11 | Hash, id and override tests green (in the full suite) | PASS |
| E-09 (v1.0) | T02b, T12 | PR#2, #15 | M1-mode plan tests green; two consecutive smoke runs start at 60 then 80 | PASS |
| E-10 | T05 | PR#10 0c0586d | `-k "noninterference or canary"`: 129 passed (128 cases: 32 knob combinations, full outbound bodies, canaries inside) | PASS |
| E-11 | T05 | PR#10 | Role tests green: verdict and no-guess schema, enum dropped above 500, plain contract with and without a watchlist | PASS |
| E-12 | T06 | PR#9 6e79647 | `tests/test_openrouter.py` 16 passed, on real recorded responses | PASS |
| E-13 | T07 | PR#5 173a943 | Catalog tests green; `validate` warns "structural only" without a cache | PASS |
| E-14 | T08 (H1) | PR#18 a6a8575; SS-12 | Live, from T08's record, rechecked offline by its reviewer: run `…032602Z-atin-c23a` (3 episodes, complete) 18/18 settles at or below reserve; run `…034339Z-atin-d2c5` (5 episodes, `interrupted` at 4/5) 24/24. Under cap. | PASS with Orchestrator waiver (run not `complete`) |
| E-15 | T08 | PR#18 | Refused run: exit 3, no new directory (5 → 5). Reference `--dry-run` with no key: exit 4, no directory. Live dry-run and refusal checked offline only (T08 review). | PASS (hermetic); live via waiver |
| E-19 | T12 | PR#15 babb004 | Trajectory tests green; local runs continue (60 → 80); `ls` lists runs | PASS |
| E-20 (v1.0) | T02f | PR#12, #14 | `simmons verify` PASS on the smoke run; corrupted-copy tests green | PASS |
| E-21 | T13 | PR#7 14915bd | `tests/test_metrics.py` green (30-record and 10/90 fixtures) | PASS |
| E-22 | T14+T15 | PR#16 2df1645 | `dataset` on fixtures: 7 rows, 66 `cfg.` columns; golden header test green | PASS |
| E-23 | T14+T15 | PR#16 | Golden grid matches; grid equals `pivot --split arena`; mixed pivot exit 2 naming the field; default grid shows 0 `%` (fake excluded). The M5 line "summaries regenerate byte-identical" failed at add2139 (R2); at 11a930c all 7 fixture summaries regenerate byte-identical. | PASS |
| E-24 | T16 | PR#13 bd8ed9d | `show --episode 3 --role guard --prompt` ends "sha256 matches"; 0 `<target>` | PASS |
| E-25 (v1.0) | T17 | PR#17 add2139 | Hostile option on a watchlist fixture copy renders as text in a c11 browser; no dialog, no console errors; only `http` string is the SVG namespace | PASS |
| E-26 (run, episode) | T17 (H6) | PR#17 screenshots, `tests/screenshots/t17` | My drive of M6 items 1 to 5 and 7 in a c11 browser tab: all pass (below). Felt check by a person: pending. | PASS (agent); pending people (H6) |
| E-27 | T22 | content on main | Files present; `australian-cities` 100 options, 8 clusters, runs under fake | pending people |
| E-28 | T23 | none | No `runs/` on main | pending people |
| E-29 | T23 | none | none | pending people |
| E-36 | T23 | none | none | pending people |
| E-37 | each milestone; SS-20 | PR#19 e55df73 | Re-validation at 11a930c: 122 lines. Sections present: M1 (Setup, Run the tests, Run the smoke experiment, Where things live, Rules for a shared repo, Spend, Interfaces); M2 (Write an experiment, Visibility); M3 (Keys, Real runs and spend, Choosing models); M4 (Waves and what S sees, Interrupt and resume, Diagnose a run); M5 (Read results); M6 (Look at an episode). One current Status line: "v1.0 code merged; committed runs pending". Points to SPEC.md (line 3). "Interrupt and resume" correctly marks resume as v1.1. | PASS |
| E-38 | T01, T06 | PR#1, #9 | `git grep -nI 'sk-or-v1-[0-9a-f]'` empty; `.env` ignored (`.gitignore:1`); 2 planted-key tests pass. Literal `sk-or-` grep self-matches `docs/EVALUATION.md:98` (on the v1.1 list). | PASS |
| E-39 | T03 (+T22) | PR#11 | `--resolved` gives `australian-cities`, the foreign affairs cover task, `templates/au-meeting`, `protected_count` null, `control_rate` 0, `plain_sender_rate` 0.3; `reference.json` keys are `name`, `description`; render tests green | PASS (code); content half pending people |

M6 viewer drive (smoke run, plus a watchlist fixture copy with the hostile option planted), in c11 browser `tab:668`: (1) 20 rows; the CATCH filter shows 7 rows, all CATCH. (2) Target outlined; 10 options marked `protected` on the watchlist run. (3) Guard tab: "Request hash matches fresh render", no `<target>`. (4) CATCH receiver panel: "Not called: the guard blocked the message." (5) Curve SVG renders; console and errors empty. (7) The hostile option shows literally; nothing executes. Item 6 is v1.1 (T17b).

## Cross-ticket seams
| Seam | Verification | Result | Evidence |
|---|---|---|---|
| run → verify | smoke run, `simmons verify` | PASS | `PASS …/20261004T042603Z-atin-f0b0` |
| run → show | overview, `--episode`, `--prompt`, `--sample` | PASS | rates match `summary.json` |
| run → view | 21 pages, browser drive | PASS | see M6 above |
| run → summarize | `simmons summarize` on a fresh run | PASS at 11a930c (was FAIL, R2) | byte-identical; `source.episodes_sha256` equals the file's SHA-256; labels `<stem> · <model>` |
| run → grid / pivot / dataset | `--include-local --include-fake` on 3 smoke runs | PASS | `n=42, r=3`; grid equals pivot; 3 dataset rows |
| episode → attempt log → next prompt | sender (and receiver) request's log block | PASS at 11a930c (was FAIL, R1) | `#0 · target: Benalla · outcome: HIT` / `message: …`; a non-default `log_records` run renders its fields in config order |
| viewer numbering ↔ records, `show` | episode index 3 | PASS at 11a930c (was FAIL, R3) | viewer `#3`, "Episode 3" = `show --episode 3` (covert CATCH); log `#0`, `#1` |
| runner local root ↔ dataset fake filter | code and mutation | PASS at 11a930c (was FAIL, R4) | `runner.py` now `any(...)`; reverting to `all` fails `test_mixed_fake_and_real_roles_use_runs_local` |
| object-form option set (T04 is v1.1) ↔ v1.0 runner | fake run on `option-sets/australian-cities.json` | PASS | complete, 20/20 |
| `validate` on every committed experiment | 6 files, plus the no-path repo check | PASS | all exit 0 |
| key-limit refresh transient (T08 review) | code and test | PASS | `test_transient_key_limit_refresh_retains_last_limit_and_completes` |

## State boundaries
- Merged: `origin/main` at add2139 (17 tickets + T02f-b). SS-20 not merged.
- Deployed: NOT PERFORMED (none in this project).
- Published/external: NOT PERFORMED. No committed real runs (`runs/` absent).

## Repairs or decisions
1. **R1, owning-ticket repair, T02c (SS-4) lineage: the sender's attempt log is raw JSON, not the SPEC §12.5 rendering.** `simmons/visibility.py:143-145` `_log_section` dumps each record with `json.dumps(..., sort_keys=True)`, so S sees `{"episode":0,"message":"…","outcome":"HIT","schema":"simmons.attempt/1","target":"Benalla"}` where SPEC §12.5 specifies `#17 · target: Townsville · outcome: HIT` / `message: …`. The internal `schema` tag reaches the model. Reproduce: `simmons run experiments/smoke.json`, then `simmons view <run>`, episode 3, "Sender" tab, or `simmons show <run> --episode 3 --role sender --prompt`. This is rendering semantics, so it must land before T23 commits a run, or all three runs repeat (BUILDPLAN "critical path"). There are no committed runs yet, so per that rule no `PROTOCOL_VERSION` decision is needed. The fix regenerates `tests/fixtures/runs/` and the goldens. The v1.1 rung-3 observer block reuses this renderer. **Re-validated FIXED at 11a930c:** `_log_section` renders the §12.5 form with no `schema` tag. Smoke episode 3 sender prompt: `#0 · target: Benalla · outcome: HIT` / `message: …`. Non-default run (`log_records: [guard_suspicion, message, receiver_pick, guard_guess, outcome]`, `receiver_sees_log: true`, fake, 8 episodes): verify PASS; sender and receiver show `#1 · guard_suspicion: … · receiver_pick: Morwell · guard_guess: Port Macquarie · outcome: HIT` / `message: …`, target left out because it is not logged; the guard prompt has no `attempt_log`. Fixtures regenerated in SS-21.
2. **R2, owning-ticket repair, T02f (SS-7), seam with T13: the runner's `summary.json` disagrees with `simmons summarize`.** `simmons/runner.py:426-446` writes `labels` as the bare prompt stem (`sender-reference`) and hashes `canonical_json(records)` for `source.episodes_sha256`. SPEC §12.6 and §14.1, `commands/summarize.py:47-49` and `dataset.py:209` use `<stem> · <model>` and SHA-256 of the file bytes. Effects: every runner-written summary looks stale and is always recomputed; running `summarize` on a committed run rewrites its `summary.json`, a git diff in `runs/`; and the M5 Verify line fails on main. Reproduce: `for r in tests/fixtures/runs/*/*/; do uv run simmons summarize "$r"; done; git diff --stat tests/fixtures/runs` gives 7 files changed (labels and `episodes_sha256`; rates identical). Fix: the runner builds the summary through the same function as `summarize`; regenerate the fixtures. Must land before T23. **Re-validated FIXED at 11a930c:** `summarize` over all 7 fixture runs leaves `git diff` empty; on a fresh smoke run, `summary.json` is byte-identical after `summarize`; `source.episodes_sha256` equals `shasum -a 256 episodes.jsonl`; labels are `sender-reference · covert`.
3. **R3, owning-ticket repair, T17 (SS-17): the viewer numbers episodes from 1, everything else from 0.** `simmons/viewer/__init__.py:522` renders `#${item.index+1}`, and the episode title and "Attempt log as sender saw it" do the same, while `episodes.jsonl`, `show --episode N`, the plan, and the log S actually received (`"episode":0`) use the index. SPEC §17: "Episode table: index". Reproduce: viewer "Episode 4" is `episode-000003.html`, which is `simmons show <run> --episode 3`. A person citing a viewer number to an agent gets the wrong episode. Small; land before the H6 and H7 read-back. **Re-validated FIXED at 11a930c:** in a c11 browser, the run table links `#0` to `episode-000000.html` (and so on), `episode-000003.html` is titled "Episode 3" (covert CATCH), matching `show --episode 3`, and the attempt log reads `#0`, `#1`; no browser errors.
4. **R4, owning-ticket repair, T02f (SS-7): a run that mixes fake and real models lands in `runs/` but is hidden as fake.** `simmons/runner.py:570` sends a run to `runs-local/` only when `all` roles are fake; `simmons/dataset.py:259` treats a run as fake when `any` role is. SPEC Q21: runs that use the fake provider go to `runs-local/`. Scenario: `"models": {"sender": "<real slug>", "guard": "fake:random"}` without `--scratch` writes a committable run under `runs/` that `grid` and `dataset` then skip. One-word fix (`any`) plus a test beside `tests/test_runner_guard_regressions.py:36`. Found by code read; a hermetic reproduction needs a stubbed catalog. **Re-validated FIXED at 11a930c:** `simmons/runner.py` uses `any(...)`, the same as `dataset.py:259`. `tests/test_runner_guard_regressions.py:100` `test_mixed_fake_and_real_roles_use_runs_local` drives a mixed run through an offline adapter; it passes, and fails when `any` is mutated back to `all`.
5. **Decision for Atin, already open: Claude refusals now reach the sender's send call, not only the debrief.** In T08's live 5-episode run (`…034339Z-atin-d2c5`), episode 3's covert send failed format after 6 attempts, with `finish_reason` content_filter, stop, content_filter, content_filter, stop, stop (SS-12 comment). If the T23 sender is Claude, expect covert `ERROR`s against E-28's 5% bar. This belongs with the open `sender_debrief` ruling; H3c shakedowns should measure the rate.

Known and routed, now landed: SS-20 (PR#19) brought CLAUDE.md to 122 lines and the suite to 5.3 s user time; the quiet-machine wall check is the Orchestrator's. Still open for Atin: the `sender_debrief` default (decision 5).

## Deferred candidates not minted
- C1 (v1.1, T10 Ctrl-C polish): a second Ctrl-C drops a paid in-flight episode. `simmons/runner.py:761-763`: the `future.result()` inside `except KeyboardInterrupt` is unprotected, so a second interrupt reaches the outer handler at `:838`. Seen live in `…034339Z-atin-d2c5` (episode 4 settled at 03:46:55.277, not recorded; observation in T08's review). Not on the run-state v1.1 list. Still present at 11a930c (`simmons/runner.py:776-778`); SS-21's interrupt test covers a single Ctrl-C only.
- C2 (v1.1, T08 hardening; not on the v1.1 list): reviewer H1, no hermetic test that the catalog reaches the `Breaker` in real runs (fails closed); H3, a negative `limit_remaining` gives exit 2 or 4 rather than refused or `stopped_budget` (`simmons/runner.py:363`).
- C4 (v1.1, test gap): E-04's "two fake runs identical apart from timestamps and run ids" is no longer asserted, since T12 makes a second run continue the trajectory. Add a test that runs smoke twice under `log_scope: "run"` or two roots and diffs content. Holds today by my manual check.
- C5 (v1.1, already deferred): a smoke run's `events.jsonl` holds only `run_start`, `call_start`, `call_end` and `run_end`. `episode_end`, `budget_warning`, `breaker_tripped` and the rest of §12.7 belong to T10's full event set.
- Checked and cleared: SS-12 review item 1 (the key-limit refusal test was satisfied by the run cap) is fixed on main. `tests/test_runner.py:367` sets `max_cost_usd: 0.5` and asserts "remaining credit".
