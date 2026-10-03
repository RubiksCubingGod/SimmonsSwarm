# SimmonsSwarm build plan

Status: draft for review, 2026-10-03. Builds what `docs/SPEC.md` specifies. Section numbers below (§) refer to SPEC.md.

## Shape

One serial milestone, five parallel tracks, one gate.

```
M1 Walking skeleton (serial: freezes the interfaces, the breaker is in from the start)
 ├── M2 Config, draws, visibility        Track A
 ├── M3 OpenRouter and spend             Track B
 ├── M4 Runner at scale                  Track C
 ├── M5 Metrics and grid                 Track D
 ├── M6 Show and viewer                  Track E
 └── Content: prompts, cover tasks, experiments (starts on day one, needs no code)
          all of the above ──► M7 First real results: the done gate
```

M1 is built by one person or one agent, because it fixes the interfaces everyone else codes against. While it is in progress, the content track writes prompts and cover tasks, and Track D can hand-write the metrics fixture from §12.4 and §13. After M1, tracks A to E run in parallel. M7 needs all of them.

No time estimates. Each milestone ends when its verification passes.

## Working agreements

- **Code** goes through a short-lived branch and a pull request. The hermetic suite must be green. Someone other than the author runs the milestone's verification: a teammate, or a fresh agent that has not seen the work.
- **Content and committed runs** go straight to `main`: new prompt files, experiments, cover tasks, finished run directories. Pull with rebase first. Never force-push.
- **Frozen interfaces.** M1 freezes `simmons/types.py` (§15) and the record schemas (§12). Additive changes (a new optional field) need no discussion: make them and add a line to the "Interfaces" section of CLAUDE.md. Breaking changes need a message to the team first, a schema major-version bump, and regenerated fixtures.
- **Fixture runs are generated, not hand-edited.** `uv run python tests/fixtures/make_runs.py` rebuilds `tests/fixtures/runs/` from the fake provider. Whoever changes the record format regenerates and commits them in the same PR.
- **Verification commands use `uv` and `jq`.** Every command below is meant to be pasted as written. `<run-id>` is the id the previous command printed.

## Tracks and file ownership

One owner per file at a time. A file outside your track is changed through its owner or in a PR they review.

| Track | Owns |
|---|---|
| M1 (then hands off) | Everything below, as stubs. Afterwards `simmons/types.py` and `providers/base.py` are frozen (changes per the working agreements), and `simmons/cli.py` is an append-only list of commands. |
| A: config, draws, visibility | `config.py`, `inputs.py`, `plan.py`, `visibility.py`, `render.py`, `roles.py`, `payloads/`, `scorers/`, `templates/`, `commands/{validate,plan,schema,new}.py` |
| B: OpenRouter and spend | `providers/openrouter.py`, `spend.py` (prices, the breaker, the estimate), `model-aliases.json`, `commands/{doctor,budget}.py`, `.env` loading |
| C: runner | `runner.py`, `episode.py`, `records.py`, `events.py`, `providers/fake.py`, `commands/{run,resume,ls}.py` |
| D: metrics and grid | `metrics.py`, `grid.py`, `commands/{summarize,grid}.py` |
| E: show and viewer | `viewer/`, `commands/{show,view}.py` |
| Content (sender side, guard side) | `prompts/`, `cover-tasks/`, `experiments/` (option sets come from the option-set conversation) |
| Everyone | Tests for your own files under `tests/`. CLAUDE.md: each milestone appends its own sections; pull right before editing it, and do not rewrite another track's section. |

Seams that keep tracks apart:
- **One file per CLI command.** Adding a command is a new file plus one line in the list in `cli.py`.
- **The fake provider stands in for OpenRouter.** Tracks A, C, D and E never need a key.
- **Fixture runs stand in for the runner.** Tracks D and E build against `tests/fixtures/runs/` from M1.
- **The breaker is one object.** `spend.Breaker` (Track B) exposes `reserve(request) -> Reservation`, `settle(reservation, cost)`, `release(reservation)` and `tripped`. The runner (Track C) calls it around every provider call. Neither track edits the other's side.
- With four people, one split that works: A; B and C; D and E; content plus M7. An agent can take any single track from its SPEC sections.

---

## M1. Walking skeleton on the fake provider

**Goal.** One command runs a complete experiment end to end on the fake provider, writes every record, and computes the headline numbers. Every later milestone deepens a piece of this without changing its shape.

**Scope.**
- `pyproject.toml` (package `simmons`, console script `simmons`, Python ≥ 3.11, deps from §2 Q20, pytest config with a `live` marker), `uv.lock`, `.gitignore`, `.env.example`.
- A GitHub Actions workflow that runs `uv run pytest -q` on every push and PR. It is small, and it protects a repo four people push to.
- `simmons/types.py` with every interface in §15: `ModelSpec`, `TextPart`, `FilePart`, `ToolSpec`, `RoleRequest`, `Usage`, `RoleResponse`, the provider errors, `ProviderStatus`, `Target`, `Pick`, `Score`, `PlanContext`, `JudgeCaller`, the five view classes with their fields (§15.3), and the `Provider`, `Scorer`, `Payload` protocols.
- `config.py`: every field of §6.1 with its type and default. Validation enforces types plus the rules the M1 path needs, including the `max_cost_usd` ceiling.
- `inputs.py`: array-form option sets, prompts, cover tasks, templates; snapshot into `inputs/`.
- `plan.py`: the §8 algorithm for `option_draw: "fixed"`, `option_order: "fixed"`, `protected_selection: "random"`, `protected_draw: "sample"`, and the exact kind mix. Other modes raise "lands in M2".
- `visibility.py`: the full table and all five view classes, with every knob wired.
- `render.py`: strict `string.Template` rendering from views.
- `roles.py`: the three tools of §11, output validation, normalization, format resampling.
- `providers/fake.py`: `fake:covert` and `fake:random` (§15.2), with `params.price_in` and `params.price_out`.
- `spend.py`: `RUN_CEILING_USD`, the `Breaker` with reserve and settle (§19.1). Prices come from the fake params for now.
- `payloads/option.py`, `scorers/exact_match.py`.
- `episode.py`, `runner.py` (waves run one at a time with one worker; every call goes through the breaker), `records.py`, minimal `events.py`.
- A minimal summary: counts by kind and outcome, hit rate, catch rate, false-alarm rate, sanity catch rate.
- CLI: `simmons --version`, `simmons run` with `--fake`, `--scratch`, `--episodes`, `--seed`, `--max-cost`, `--json`.
- Content stubs: `templates/default/*.md` with the final tags and placeholder wording; placeholder `prompts/sender/reference.md`, `prompts/guard/reference.md`, `prompts/plain/reference.md`; the zero-byte `prompts/sender/empty.md`; `cover-tasks/note-to-teammate.md`.
- Experiments: `smoke.json` (20 episodes, every role on `fake:covert`, option set `tests/fixtures/option-sets/smoke-100.json` so it never depends on the presets), `smoke-priced.json` (the same with fake prices and a $0.05 cap, so the breaker trips; once M3's estimator lands, tune its prices so the estimate stays under the cap while worst-case reservations pass it, or it will be refused instead of tripping), `smoke-slow.json` (200 episodes, `latency_ms: 20`, for M4), and first drafts of `sanity.json`, `mirror.json` and `reference.json` (M7's three runs). If the preset option sets have not landed yet, these point at fixture sets in `tests/fixtures/option-sets/` (`nouns-100.json`, `numbers-100.json`), so `validate` and `plan` work from M2 on; the content track repoints them to the presets before M7. M1 never writes into `option-sets/`, which the option-set conversation owns.
- Tests: the hermetic autouse fixture (§21), the end-to-end smoke test, the golden plan for `smoke.json`, the breaker tests that need no OpenRouter, and unit tests for each module's M1 path. The fake receiver's `decode_error_rate` (§15.2) is what makes MISS appear; if seed 7 does not yield every outcome in `smoke.json`, change the smoke seed, not the assertion.
- `tests/fixtures/make_runs.py` and the fixture runs it writes: three sender prompts against two guard prompts in one arena, plus one run in a second arena, mixing `fake:covert` and `fake:random`. Every fixture run has 20 episodes.

**Definition of done.** Every check below passes, and CLAUDE.md has its M1 sections.

**Verify.**
```bash
uv sync
uv run simmons --version                                   # 0.1.0
uv run pytest -q                                           # green, in under 10 s
uv run simmons run experiments/smoke.json --json > "$TMPDIR/smoke.json"; echo "exit $?"   # exit 0
RUN=$(jq -r .run_dir "$TMPDIR/smoke.json"); echo "$RUN"    # under runs-local/smoke/
jq -r .status "$RUN/run.json"                              # complete
ls "$RUN"                  # attempt-log.jsonl config.json episodes.jsonl events.jsonl inputs plan.jsonl run.json summary.json
wc -l < "$RUN/episodes.jsonl"                              # 20
jq -r .kind "$RUN/episodes.jsonl" | sort | uniq -c         # control 4, covert 14, plain_allowed 1, plain_protected 1
jq -r .outcome "$RUN/episodes.jsonl" | sort | uniq -c      # CATCH, HIT and MISS each at least once
git status --short runs/                                   # empty: fake runs never land in runs/
uv run simmons run experiments/smoke-priced.json --json > "$TMPDIR/priced.json"; echo "exit $?"   # exit 3
jq -r .status "$TMPDIR/priced.json"                        # stopped_budget
jq -r .run_dir "$TMPDIR/priced.json" | xargs -I{} jq .spent_usd {}/run.json   # below 0.05
```

**CLAUDE.md after M1:** Setup, Run the tests, Run the smoke experiment, Where things live, Rules for a shared repo (§5), Interfaces, and the spend rule: every provider call goes through `spend.Breaker`; the $20 ceiling is a code constant.

---

## M2. Config, draws and visibility, complete (Track A)

**Goal.** Every field and rule in §6 works, every draw mode in §8 works, and the visibility rules are proven by test.

**Scope.**
- Every validation rule in §6.1, each with a failing fixture in `tests/fixtures/bad/` and its expected JSON path. Unknown keys with "did you mean". `_` keys ignored.
- Object-form option sets and clusters (§7.1). Draw modes: `sample`, `shuffled`, `cluster`, `protected_draw: "fixed"`.
- The hashes and ids of §6.3.
- Tool schema variants: `verdict` mode, no `guess`, the enum dropped above 500 options.
- The canary test over all 32 knob combinations (§10.2, item 8). The visibility table pinned to the page's table.
- Commands: `validate` (with repo checks when given no paths), `plan`, `schema`, `new`.
- A first full draft of the default templates' wording, reviewed by the content track.

**Definition of done.** Every check below passes; every rule in §6.1 has a test.

**Verify.**
```bash
uv run pytest -q tests/test_config.py tests/test_inputs.py tests/test_plan.py tests/test_visibility.py tests/test_render.py tests/test_roles.py
uv run pytest -q -k canary                                 # 32 combinations, all green
uv run simmons validate; echo "exit $?"                    # exit 0
for f in tests/fixtures/bad/*.json; do uv run simmons validate "$f" --json | jq -r '.experiments[0].errors[0].path'; done   # one JSON path per file, none empty
uv run simmons schema | jq '.properties | keys | length'   # the number of fields in §6.1
uv run simmons plan experiments/reference.json --head 3
uv run simmons new my-test --from smoke && uv run simmons validate experiments/my-test.json && command rm -f experiments/my-test.json
```

**CLAUDE.md after M2:** Write an experiment (`new`, `validate`, `schema`), and the visibility rule in one paragraph: views are the only way a role reads episode state; adapters get a `RoleRequest` and nothing else.

---

## M3. OpenRouter and spend (Track B)

**Goal.** Real models through OpenRouter, with exact cost and the breaker running on real prices.

**Scope.**
- `providers/openrouter.py` as §15.1 specifies: forced tool choice, `require_parameters`, `params.provider` and `params.reasoning` passthrough, `usage.cost`, served-by host, `X-Title`.
- Error mapping: 429, 5xx and timeouts are transient; 402 is `BudgetError`; any other 4xx is permanent, with the provider's message.
- Model list cache from `GET /api/v1/models`: slug, tool support, prices. Validation rejects unknown slugs, models without tool support, and unpriced models. `model-aliases.json` with the page's `claude-sonnet-5-5`.
- The breaker on real prices. The second cap from `GET /api/v1/key` (`limit_remaining`), read before the run and between waves (§19.3).
- The cost estimate and the pre-run refusal (§19.2), `run --dry-run` (§9.1 step 4), and the estimate that `validate` prints.
- `simmons budget --audit`: compare each call's recorded cost with OpenRouter's per-generation cost.
- `simmons budget`, `simmons doctor` (and `--live`), `.env` loading.
- Built before the key arrives against recorded OpenRouter responses in `tests/fixtures/openrouter/`, with the client's HTTP transport stubbed, so the suite stays hermetic.

**Definition of done.** Every check below passes with the team's OpenRouter key.

**Verify.**
```bash
uv run pytest -q tests/test_openrouter.py tests/test_spend.py      # hermetic
uv run simmons doctor                    # key set; key limit and usage shown; every model in experiments/ found, tool-capable, priced
uv run simmons doctor --live             # one tiny tool call per distinct model, all ok
uv run pytest -m live -q                 # 3-episode reference run under a $0.50 cap; tool calls parse; usage.cost recorded
uv run simmons run experiments/reference.json --episodes 5 --scratch --json > "$TMPDIR/ref5.json"
jq -r '.status, .summary.cost.usd' "$TMPDIR/ref5.json"             # complete, a small positive number
uv run simmons budget --audit "$(jq -r .run_dir "$TMPDIR/ref5.json")" --json | jq '.mismatches | length'   # 0: our ledger matches OpenRouter's, call by call
uv run simmons run experiments/reference.json --dry-run; echo "exit $?"                     # prints kind counts and an estimate; exit 0
ls runs-local/reference | wc -l > "$TMPDIR/n0"
uv run simmons run experiments/reference.json --max-cost 0.01 --scratch; echo "exit $?"     # exit 3, refused before any call
ls runs-local/reference | wc -l | diff - "$TMPDIR/n0"                                       # no output: a refusal writes no run directory
```

**CLAUDE.md after M3:** Keys (`.env`, `OPENROUTER_API_KEY`), Real runs and spend (estimate, the $20 breaker, `--scratch`, `simmons budget`), Choosing models (slugs, aliases, pinning hosts).

---

## M4. Runner at scale (Track C)

**Goal.** Runs are fast, interruptible and resumable, and an agent can diagnose one from its files.

**Scope.**
- Waves with frozen log snapshots (§9.2), the thread pool (`--concurrency`, default 10), the single-wave fast path when no log is in play.
- Ctrl-C handling and `--stop-after N`. `resume`, with `--retry-errors`. The breaker's spend carried across resumes.
- The full event set of §12.7, `calls.jsonl`, the per-wave progress line, every status value.
- `shadow_decode`, if Atin says yes to SPEC open question 1.
- `simmons ls`, with stale-run detection.

**Definition of done.** Every check below passes.

**Verify.**
```bash
uv run pytest -q tests/test_runner.py
uv run pytest -q -k "concurrency_invariant or resume_equals_uninterrupted or wave_log_snapshot"
uv run simmons run experiments/smoke-slow.json --stop-after 57 --json | jq -r '.status, .run_dir'   # interrupted, <run-dir>
uv run simmons resume <run-dir> --json | jq -r .status                    # complete
jq -s 'map(.index) | unique | length' <run-dir>/episodes.jsonl            # 200
jq -r .event <run-dir>/events.jsonl | sort | uniq -c                      # includes run_start, run_resume and run_end; no error events
uv run simmons ls --runs-dir runs-local
```

**CLAUDE.md after M4:** Waves and what S sees, Interrupt and resume, Diagnose a run (the order in §20).

---

## M5. Metrics and grid (Track D)

**Goal.** Every number in §13 and the grid of §14, computed from records alone.

**Scope.**
- `metrics.py`: every metric in §13, Wilson intervals, the curve, AUC, hit rate at thresholds, the shadow fields when present.
- `simmons summarize`.
- `grid.py`: arenas, pooling, labels with collisions, filters, the terminal, `--md`, `--json` and `--html` outputs (the HTML page itself comes from Track E's template).
- A hand-built fixture of 30 episode records with hand-computed expected values. It can be written before M1 lands, from §12.4.

**Definition of done.** Every check below passes.

**Verify.**
```bash
uv run pytest -q tests/test_metrics.py tests/test_grid.py
for r in tests/fixtures/runs/*/*/; do uv run simmons summarize "$r"; done
git diff --exit-code tests/fixtures/runs                   # summaries regenerate byte-identical
uv run simmons grid --runs-dir tests/fixtures/runs --include-fake --md | diff - tests/fixtures/grid.golden.md   # no output
uv run simmons grid --runs-dir tests/fixtures/runs --include-fake --json | jq '.arenas | length'               # 2
uv run simmons grid --runs-dir tests/fixtures/runs | grep -c '%'                                               # 0: fake runs excluded by default
```

**CLAUDE.md after M5:** Read results: what each summary field means in one line each, how to read a grid cell, why the intervals matter.

---

## M6. Show and viewer (Track E)

**Goal.** A person or an agent can see any episode exactly as each role saw it.

**Scope.**
- `simmons show`: run overview, one episode, one role's re-rendered request with the `request_sha256` check.
- `simmons view`: the run page and episode detail of §17, including the "as seen by" tabs, and the grid page.
- One self-contained HTML file per page: inline CSS and JavaScript, embedded JSON, no external URL.

**Definition of done.** The commands pass, and a reviewing agent has driven the pages in a real browser.

**Verify.**
```bash
uv run pytest -q tests/test_viewer.py tests/test_show.py
uv run simmons show tests/fixtures/runs/<experiment>/<run-id> --episode 3 --role guard --prompt   # ends "sha256 matches"; no <target> tag in the text
uv run simmons view tests/fixtures/runs/<experiment>/<run-id> --open
uv run simmons grid --runs-dir tests/fixtures/runs --include-fake --html --open
```
Then a reviewing agent drives both pages in a browser (a c11 browser tab, or Chrome) and confirms:
1. The episode table has 20 rows, and filtering to CATCH shows only catches.
2. Episode detail outlines the target and shades the protected options.
3. The guard's "as seen by" tab shows a hash match and no target.
4. On a CATCH episode, the receiver tab says "not called".
5. The curve renders, and the console has no errors.
6. The grid page matches `simmons grid` for the same runs.

Screenshots go in the PR.

**CLAUDE.md after M6:** Look at an episode (`show` for agents, `view` for people).

---

## Content track (from day one)

Needs no code and can start immediately.

- **Sender side:** the reference P₁ (`prompts/sender/reference.md`), a generic, modest scheme-free instruction that sets the baseline row; the obvious-code prompt for the sanity run (`prompts/sender/obvious-code.md`); first candidate prompts (`<slug>-v1.md`).
- **Guard side:** the reference P₂, a plain careful monitor that sets the baseline column; first candidate guard prompts.
- **Both:** the plain sender's prompt; the final wording of the default templates, together with Track A; two or three more cover tasks; the experiments each person wants to run first.
- **Option sets:** delivered by the option-set conversation. M7 needs `numbers-1-100` (sanity) and `common-nouns` (mirror and reference), or whatever that conversation names them. `option_draw: "sample"` needs a few larger pools (300 to 500 options), per Atin's notes, and `protected_selection: "cluster"` needs the object form with clusters (SPEC §7.1). The option-set conversation's brief asks for ten sets of exactly 100, so Atin relays both asks to it.

---

## M7. First real results: the done gate (everyone)

**Goal.** The framework does its real job end to end, on real models, with results that combine across people. "Done" means this milestone, not green tests. It is Atin's three-run ladder from `atin/phosphor-mutations-selected.md`, run in order, each on real models through OpenRouter.

1. **Sanity** (`experiments/sanity.json`): option set `numbers-1-100`, a sender prompt with an obvious code (`prompts/sender/obvious-code.md`). It proves the pipeline, the visibility rules and the scorer. Expect a hit rate far above chance, plain senders with a protected target caught about every time, and false alarms near zero. If those do not hold, stop: the pipeline is wrong, not the prompts.
2. **Mirror** (`experiments/mirror.json`): the reference config with the empty P₁. The floor every later P₁ is read against.
3. **Reference vs reference** (`experiments/reference.json`): Atin's basic experiment (his `nouns-baseline-01`). The first real cell of the grid.

**Scope.**
- Real reference prompts, the obvious-code prompt, the plain prompt, final templates, and the `numbers-1-100` and `common-nouns` option sets in place.
- The three runs, committed.
- The reference prompts frozen: a test pins their SHA-256.
- CLAUDE.md complete, and a cold-agent check.

**Definition of done.** Every check below passes.

**Verify.**
```bash
uv run simmons validate; echo "exit $?"                     # exit 0
for e in sanity mirror reference; do uv run simmons run experiments/$e.json --dry-run; done   # each estimate under $20; if not, set log_window or fewer episodes, never raise the ceiling
uv run simmons run experiments/sanity.json                  # status complete
jq '.rates.hit_rate.value, .rates.sanity_catch_rate.value, .rates.false_alarm_rate.value' runs/sanity/*/summary.json
                                                            # at least 0.20 (twenty times chance); at least 0.90; at most 0.10
uv run simmons run experiments/mirror.json                  # status complete
uv run simmons run experiments/reference.json               # 200 episodes, status complete
jq .rates.sanity_catch_rate.value runs/reference/*/summary.json   # at least 0.90; lower means the reference guard is broken: fix it, then re-run sanity, mirror and reference before freezing
git pull --rebase && git add runs/sanity runs/mirror runs/reference && git commit -m "Add the sanity, mirror and reference runs" && git push
```
Then a second person, on their own clone:
```bash
git pull && uv run simmons grid      # reference guard column: the reference row and the empty row, about 140 covert episodes each, with intervals
uv run simmons view runs/reference/<run-id> --open   # walk one CATCH, one HIT and one MISS through every "as seen by" tab
uv run pytest -q                     # green, including the frozen-reference hash test
```
**Cold-agent check:** a fresh agent with no context gets one instruction: "Read CLAUDE.md. Run the smoke experiment, then tell me the hit rate and false-alarm rate of the latest reference run, with their intervals." It succeeds without help.

**CLAUDE.md after M7:** Done means (this milestone's checklist), Pitfalls (whatever the build taught), and the Status line set to "v1 working".

---

## Stub first, real later

| Piece | Stub in M1 | Real in |
|---|---|---|
| Model provider | `fake:covert`, `fake:random` | M3: OpenRouter |
| Prices | fake params | M3: OpenRouter model list |
| The breaker | complete, on fake prices | M3: real prices and the key's limit |
| Draw modes | fixed options, fixed order, random protected redrawn per episode | M2: sample, shuffled, cluster, fixed protected |
| Validation | types plus the M1 path's rules | M2: every rule |
| Visibility | full table, all views, all knobs | M2: canary proof over 32 combinations |
| Templates | final tags, placeholder wording | M2 draft, M7 final |
| Reference prompts | placeholders | M7: written, run, frozen |
| Runner | one wave at a time, one worker | M4: thread pool, interrupts, resume |
| Summary | counts and four headline rates | M5: every metric |
| Looking at results | `jq` on `episodes.jsonl` | M6: `show` and `view` |
| Option sets | `tests/fixtures/option-sets/smoke-100.json` | the option-set conversation's presets, before M7 |

## Later, after M7

Not in this plan. These are Atin's later fields, in his order, off by default; each has a seam in v1 (§22):
1. `guard_output: "distribution"`, `receiver_output: "distribution"`.
2. `sender_authors_prompt`: S writes P₁ once before the run.
3. `sender_author`, `guard_author`, `generations`: the arms race.
4. `guard_memory`.
5. `guard_actions` with `rewrite` and `rewrite_strength`, then `inject`.
6. `receiver_replies`, `messages_per_episode > 1`.
7. `target_scope: "run"`: low and slow.

Also later: open payloads (`text`, `file`) with `similarity` and `llm_judge` scorers; a catch-corpus export; template cache markers; cover-task sets drawn per episode; `log_from_run` if SPEC open question 3 goes that way; direct provider adapters only if OpenRouter cannot pass something through. The first experiment after M7 is the monitoring gap, which needs only `models`.

## How CLAUDE.md grows

CLAUDE.md is the one teaching document for every agent here, because AGENTS.md links to it. It stays short (target: under 150 lines) and points to SPEC.md for depth. Each milestone's definition of done includes its CLAUDE.md sections, listed under each milestone above. In order:

| After | Sections added |
|---|---|
| M1 | Setup · Run the tests · Run the smoke experiment · Where things live · Rules for a shared repo · Interfaces · Spend rule |
| M2 | Write an experiment · Visibility in one paragraph |
| M3 | Keys · Real runs and spend · Choosing models |
| M4 | Waves · Interrupt and resume · Diagnose a run |
| M5 | Read results |
| M6 | Look at an episode |
| M7 | Done means · Pitfalls · Status: v1 working |

The Status line changes at every milestone ("M3 done; M4 and M5 in progress"), so an agent arriving cold knows what exists. A new pitfall gets an entry the day it bites: the trap, why it fails, the fix.
