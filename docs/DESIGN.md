# al — from `LLM.txt` to a codebase-specific agentic layer

## The idea in one paragraph

Tactical Agentic Coding describes the agentic layer as something built around a codebase rather
than inside it: templates, plans, HOPs and ADWs, kept in a separate directory. Its workflows are
written for *this* codebase; one that would fit any repository does little for any of them.
Building that layer by hand for every repository does not scale, and letting an LLM
free-write it gives you prose nobody checks. `al` puts a small compiler in the middle. The LLM
writes only two things — a descriptive `LLM.txt` and a short `#lang al` program — and the compiler
plus a deterministic generator do the rest. The compiler is the closed loop for the layer itself:
request (author `agentic.rkt`) → validate (compile) → resolve (fix the clause the error points at).

```
application repo ──al scout──▶ LLM.txt (draft) ──LLM + scout-codebase──▶ LLM.txt
                                                                           │
                              AL-DSL.llms.txt (generated language ref) ────┤ LLM + author-layer
                                                                           ▼
                                                                     agentic.rkt   (#lang al)
                                                                           │ racket  (errors → repair-layer)
                                                                           ▼
                                                              agentic-layer.json  (IR, JSON Schema)
                                                                           │ al gen  (deterministic)
                                                                           ▼
   <app>.al/  commands/ hops/ experts/ adws/ env/ forge/ systemd/ tasks.md specs/ agents/ artefacts/ trees/
```

## What the lessons prescribe, and where it lands

| Lesson concept | In `al` |
|---|---|
| Core Four; 12 leverage points; templates and ADWs as the highest two | The DSL declares exactly the through-agent levers: commands (templates), hops, experts, ADWs, validation cmds, conditional docs |
| Template your engineering; meta-prompts produce plans (`/chore`, `/bug`, `/feature`) | `(command NAME #:template plan-chore …)` instantiates a kit template; codebase-specific parts (`Relevant Files`, validation commands, package manager, style rules) come from `(codebase …)` and `#:focus` |
| HOP = a prompt that takes a prompt/plan as argument (`/implement <plan>`) | `(hop implement #:template run-plan #:takes plan)`; meta HOPs (`meta-command`, `meta-adw`, `meta-expert`) produce new templates / ADWs / experts |
| PITER: Prompt input, Trigger, Environment, Review | Four option families, each with alternatives; every ADW picks one or more of each |
| ADW = deterministic code + agents, composed plan → build → test → review → document | `(adw … (steps (run …) (check …) (review) (ship) (compose other-adw)))`, interpreted by one generic uv runtime |
| One agent, one prompt, one purpose | Every `(run …)` step is a fresh headless agent with one command |
| Each step does only its own job, and the runtime checks it | `adw_modules/discipline.py` compares HEAD, the refs, the main checkout and the layer's top level before and after every step that may not commit; a difference fails the step as a step-discipline violation and is undone where that is safe (`step.discipline` event) |
| Always add feedback loops (request → validate → resolve) | `(check NAME #:cmd test #:resolve resolve-test #:retries 3)` |
| Review asks whether the result is what was requested, and wants evidence for the answer; severities blocker / tech_debt / skippable | `review` command returns that JSON; artefacts (logs, junit, images, video keyframes) are collected, stored and handed to the reviewer |
| ZTE: SDLC + ship; shipping happens only when every stage passed; begin with chores | `(review auto …)`: agent verdict AND deterministic gates (required checks, allowed classes, max diff, protected paths) → merge/deploy, else fall back to a human PR |
| Work collects in stages that people accept as a whole | `(delivery sprint\|gitflow\|…)` / `(cycle …)` / `al promote` + `al decide`: the stages work collects in, branches only people may change, a presentation pack and recorded acceptance before merge, version, tag, deploy (see "How work is delivered", "Promotion between stages") |
| Isolation via worktrees + ports; layer separate from app | `environment worktree` creates `trees/<adw-id>` under the layer; the app's working tree is never written |
| The sandbox is built once per recipe and checked before work starts | image `al-<layer>:<recipe hash>`, built only when the recipe changed; a preflight before the first step; `al sandbox LAYER build\|update\|doctor\|shell\|status\|prune\|push\|pull` (see "Sandbox image") |
| `tasks.md` two-way task list polled by a cron trigger | `(prompt-input tasklist)` + `(trigger cron)` / `(trigger file-watch)` |
| Agent experts: expertise file + self-improve | `(expert NAME #:domain … #:paths …)` |
| Model sets base/heavy; KPIs | named model tiers per agent (`base`, `heavy`, any others in `#:models`), picked with `#:model-set` on commands/hops/steps and overridable per run; `(budget limits …)`; `track-kpis` template |
| Don't couple to one tool | `agent` family: claude-code, codex, custom CLI, mock |

## Why a DSL, and why Racket

Three properties matter when an LLM is the author:

1. **Small closed vocabulary.** The whole language fits in one generated reference file
   (`docs/AL-DSL.llms.txt`, ~500 lines). "Anything not listed does not exist."
2. **Errors that teach.** Every error is located at the offending clause and lists the valid
   alternatives (`` `tests` is not a declared codebase cmd; declared: test ``), so a repair turn
   needs no other context.
3. **Semantic rules, not just shape.** A JSON Schema can say a field is a string; it cannot say
   "an ADW that ships without a human must have a check step before its review step". The compiler
   can, and those rules are where an LLM-written layer usually goes wrong.

**Compiler warnings.** Some declarations compile but do nothing, or not what was meant. They are not
errors (the layer still works), so the compiler reports them on stderr as `warning: …` lines and
leaves the IR unchanged; `al compile`, `al gen` and `al check` pass them on (`racket/al/private/warnings.rkt`):

- an environment no workflow's `#:environment` names (home-manager excepted: it is installed, not run in);
- `#:credentials` on a podman option with `#:agent-inside #f` (the credentials only reach an agent inside);
- `#:publish` or `#:services` on a podman option with `#:scope command` (each command gets a fresh
  container, so a server one command starts is gone before anything can reach it);
- a workflow whose stack has a nix dev shell *and* a podman option, when a nix package is provided by
  neither the container's base image, `#:packages` nor `#:setup` (commands run in the container, so
  the dev shell's tools are not there). Names are compared loosely: `python312` → `python`,
  `nodejs_20` → `nodejs`, with `node`/`nodejs` and `python`/`python3` as aliases.

Racket gives this cheaply: s-expressions are trivial for an LLM to emit, `#lang` gives
expansion-time compilation with source locations, and the standard `#lang` pattern fits exactly:
a module reader, compilation at expansion time, and a `module+ main` that prints JSON. `racket/al`
follows it.

## The meta-DSL

"Our own meta DSL" is `racket/al/private/meta.rkt`: three forms that declare the vocabulary as data.

```racket
(define-family prompt-input #:key "promptInputs" #:letter "P" #:default cli "PITER · Prompt input …")
(define-kind prompt-input tasklist
  "A plain-text task list (tasks.md) …"
  [path string "tasks.md" "Task file relative to the layer."]
  [open-marker string "[ ]" "Marker of a task that is ready to be picked up."] …)
(define-record command "…" [template (enum prime … custom) #:required "Kit template to instantiate."] …)
```

From `private/vocab.rkt` three things are derived, so they cannot drift: the forms the compiler
accepts, the JSON Schema of the IR (`schemas/agentic-layer-v1.schema.json`), and the LLM reference.
Adding a PITER alternative is one `define-kind` plus its runtime support. Kinds can be marked
`planned` (GitLab, Notion, chat): they compile, the generator refuses them, the reference says so.

## PITER alternatives

| | Kinds |
|---|---|
| **P**rompt input | `cli` · `issue` (GitHub/Forgejo) · `tasklist` (tasks.md) · `spec-file` (drop a spec, skip planning) · `voice` (speech → text) · `webhook-payload` · planned: `notion`, `chat` |
| **T**rigger | `manual` · `cron` (poller, optional systemd unit) · `webhook` (HMAC-verified HTTP) · `forge-actions` (GitHub/Forgejo Actions workflow) · `file-watch` · `voice` (push-to-talk loop) |
| **E**nvironment | `host` (read-only) · `worktree` · `nix-devshell` (flake) · `home-manager` (module) · `podman` (rootless container) — stacked per ADW; a nix-devshell or podman option may reuse the repository's own flake dev shell, Containerfile or dev container (`#:flake`, `#:containerfile`, `#:devcontainer`), and the worktree option runs `#:prepare` cmds once per run |
| **R**eview | `human` (PR) · `agent` (verdict + proof, human merges) · `auto` (zero-touch: verdict + gates → merge/deploy) |

Supporting families: `agent` (claude-code, codex, custom, mock), `forge` (none, github, forgejo),
`storage` (local, s3-compatible), `artefact` (log, junit, coverage, image, video, audio, trace,
html, file), `voice` (Kokoro TTS + faster-whisper), `notify` (none, email, command), `layer` placement.

A declared `voice` also gives the dashboard a voice helper: a separate local service (`adws/voice/serve.py`,
loopback only, a token only the dashboard knows, models fetched once by `al voice LAYER setup` and checked
against `models.json`) recognises and speaks, and the dashboard proxies to it so `dashboard.py` stays
standard-library; the helper's agent runs with no tools and can only read runs through a fixed registry and
propose a card. A proposal starts only after the person answers yes and clicks Approve, both enforced by the
server (same browser session, unchanged digest, within two minutes).

Defaults when a family is omitted: cli prompt, manual trigger, worktree, human review, Claude Code,
no forge, local storage, no notifications. The smallest layer is 12 lines (`racket/al/examples/minimal.rkt`).

## Automatic review and shipping (the "R" that removes the human)

```
check steps (deterministic, with resolve loop) ─▶ collect artefacts ─▶ storage (local | s3)
        │                                              │ video → ffmpeg keyframes
        ▼                                              ▼
   exit codes                         review command × N reviewers ─▶ verdict JSON
        └───────────────┬──────────────────────────────┘
                        ▼
      gate: all checks green · required checks green on the tree that ships · no blocker · class allowed
            · diff ≤ max-diff-lines · no protected path touched
                 │ all true                       │ any false
                 ▼                                ▼
        ship: merge | deploy                 human PR, reasons listed
```

The verdict is an opinion; the gate is arithmetic. Zero-touch needs both. Blockers trigger a
patch plan → implement → re-check → re-review loop up to `#:max-retries`.

Shipping never touches the application's working tree: with a forge it is PR + merge through the
API; with a bare remote it is a push from the worktree; purely locally the result is placed on
`al/ship/<default-branch>` for a one-line fast-forward.

**Where a check runs.** `(check NAME #:cmd C #:in ENV)` runs C in the run's worktree inside
environment option ENV instead of the workflow's own stack (the default): a podman option gets a
container of its own (`adw_modules/checkenv.py`: named `al-<layer>-<run>-<ENV>`, labelled
`al.check-env=ENV`, one `podman run --rm` per command, so nothing of it runs while the host runs git;
it shares the run's worktree, variables and git sandbox and relaxes nothing), a nix-devshell option
runs it under `nix develop` on the host. The compiler accepts only those two kinds. The same
environment is used by the review's re-checks and by the gate's re-run of the same cmd, and the
preflight checks each such podman environment with the commands of the checks that run in it. A
zero-touch workflow whose check runs `#:in` a nix-devshell option compiles with a warning: that check
runs outside the run's podman sandbox.

**Skipped tests.** A check that exits 0 while the tests that matter were skipped proves nothing.
Every check attempt counts its skipped tests and their reasons (`adw_modules/skips.py`) from the
JUnit reports its `#:collect` junit artefacts wrote during that attempt, else from its log: the final
summary and `-rs` lines of pytest (also `-q`), `go test -v` and `-json`, `cargo test` and mocha's
spec reporter; several runners in one log add up. The count is recorded on the check
(`state.checks[NAME].skips`), in `check.end`, in the run summary (`test=0 (3 skipped: no podman ×2,
needs network ×1)`), on the dashboard and as a `skips` entry in the reviewers' manifest, whose prompt
treats a requirement proven only by a skipped test as unproven. Two rules turn skips into a failure:
`#:max-skipped N` (more than N skipped tests) and `#:forbid-skip-reason "TEXT"` (a skipped test whose
reason contains TEXT, case-insensitive). A broken rule appends an `al: check NAME fails: …` line to
the log and turns exit 0 into exit 1, so the resolve agent, the gate and a person all see why. The
rules fail closed: with a rule set, output al cannot read, `go test` without `-v`, or skips that show
no reason (pytest without `-rs`, mocha, a reason-less `#[ignore]`) fail the check with a line naming
the flag to add. They apply in the check step, its resolve loop, the review's re-checks and the
gate's re-run of the same cmd, so a gate cannot pass on tests that did not run.

### Affected checks and reused results

A workflow that lists five suites ran all five for every change, again in every review round and
again at the gate. Two things make the checks cost what the change needs; neither is a way to skip
a check that matters. Both are decided in one module (`adw_modules/affected.py`); `steps.py` is the
only caller, and the review and the gate (`review.py`, `ship.py`, both protected and unchanged) reach
it through `steps.run_check_with_resolve` and `steps.run_logged`.

**What a check covers.** `(cmd NAME "…" #:covers ("kit/dashboard" "*.md") #:always #f #:reuse #t)`.
`#:covers` lists directories, files or globs relative to the repository root; `#:always #t` (the
default) means the cmd runs for every change, so a layer that declares nothing behaves as before.
Affected checks are asked for in three places: `#:when-affected #t` on one check step,
`(check NAME #:cmds affected …)`, which the compiler expands, like `(compose …)`, into one ordinary
check step per cmd that declares `#:covers` (named after the cmd, `whenAffected` true, the other
keywords as written, so the IR and the schema know no new step kind), and `#:require affected` on a
`review auto`, for the gate.

**Selection** is a pure function of the workflow's checks (the cmds of its check steps and its
required checks) and the changed paths, and it fails safe. First match wins:

1. the changed paths cannot be computed → every check runs;
2. the workflow is a layer change, or a changed path lies under an in-repo layer directory → every check runs;
3. a changed path no check covers → every check runs;
4. a changed path two or more `#:always #f` checks declare → every check runs;
5. otherwise the `#:always` checks run, and the checks whose `#:covers` matches a changed path.

Each "every check runs" names the path that caused it, in the record of every check. A check that
is not run is recorded as **not affected**, with the reason and the paths compared (at most 50, plus
the real count): step status `not-affected`, `state.checks[NAME].outcome`, an entry in
`state.check_results`, a `check.decision` event and a log `checks/NAME.not-affected.log`. Its
`state.checks` record carries `"exit": 0` only because the protected gate counts a check step without
a record as failed; every reader shows `outcome`, never the exit code, so nothing displays it as
passed.

**The changed paths** are `git diff-tree -r --name-only -z --no-renames REFERENCE TREE`: a rename
counts as its old and its new path, a deleted file counts. `REFERENCE` is the run's base (the merge
base of `HEAD` and the branch the run delivers into); in a review round it is the tree the check
last passed on, so a patch that leaves a suite's paths alone does not run it again. The round
re-runs the check steps that ran (`review._checks_that_ran`); whenever it does, the steps recorded
as not affected are decided again for the new tree (`steps._redecide_not_affected`), so a patch
that touches what such a step covers runs it in the round.

**The tree hash.** Every result is recorded with the hash of the worktree's content: `git add -A`
into a temporary copy of the index (the real index is never written), the worktree option's
`#:copy-files` unstaged again as `ship.commit_pending` does, then `git write-tree`. It equals
`HEAD^{tree}` once everything is committed, which is how a pass in a check step is found again at
the gate. Sandboxed runs compute it through `gitsafe` (container frozen, metadata verified, pinned
locations, hardening options). A result also carries the hash of the check's definition (command
line, environment, skip rules, sandbox image). A command that left the worktree different from how
it found it has a result without a tree, which nothing reuses; when git fails there is no hash,
every check runs and nothing is reused.

**Reuse.** When a cmd is asked for again on a tree with exactly the content it already passed on
in this run, with the same definition, it is not run: the result is recorded as `reused from STEP,
same tree HASH`. Only a real pass is a source, never a failure, a "not affected" or another reuse.
`#:reuse #f` on a cmd turns this off for a check that is not deterministic. Results are not shared
between runs.

**The gate's guarantee.** For each required cmd the gate first compares the worktree's hash with
`HEAD^{tree}`: only when they are equal (what is in the worktree is exactly what ships) can a pass
be reused, and only then, and only with `#:require affected`, can a cmd be judged not affected,
always against the run's base and never against a later pass. In every other case the gate runs
the cmd, as before. There is no proof by transitivity: a pass on an older tree plus "nothing it
covers changed since" does not satisfy the gate. So by default every required check has a passing
result for the exact tree that ships; `#:require affected` trades that for speed, and the compiler
warns when it is combined with `#:ship merge` or `deploy`, because a wrong `#:covers` then ships
unproven code. A gate decision's text is the content of `checks/gate-NAME.log`.

**What a person sees.** The run summary shows `NAME=not affected (WHY; N paths compared)` or
`NAME=reused from STEP, same tree HASH` in `checks:` and a `gate:` line; "What the checks proved"
(a table per check: where, result, evidence, then the paths compared) is appended to `PR.md` or
posted as one comment on the run's pull request after shipping or a handoff
(`affected.publish`; the pull request body itself is built in the protected `ship.py`, whose
"Checks" list still prints `exit 0` for a check that was not run); the dashboard shows the same
table. `al verify` and `al sandbox doctor` print `coverage:` lines before their last line: the checks that declare no
`#:covers`, and the `(paths #:source …)` entries no check covers.

### Depths, batches and misses

Affected checks choose which suites run; a depth chooses how much of a suite runs. A codebase cmd
keeps `#:run` as its full form and may add `#:targeted "pytest -q {tests}"` (`{tests}`, `{files}`,
`{packages}`) and `#:smoke "…"`. The layer says how deep runs go with one `(testing DEPTH …)`
clause; without it every check runs its full form, exactly as before, and `al explain`, `al verify`
and the dashboard say so in a note. The policy is decided in `adw_modules/testing.py`, the selection
in `selection.py`, the coverage maps in `coverage_map.py` and the batch in `batch.py`; `steps.py`
(`plan_for`, `run_check`, `run_logged`) is the only seam, as for affected checks.

**The depth of a run** (`testing.run_depth`), first match wins: a person's "full before it ships"
(`agents/<id>/testing-request.json`, written by `al testing --full-before-ship` or the dashboard; it
reaches the gate), `al run --testing LEVEL`, the local setting `agents/settings/testing.json`
(light = smoke, standard = targeted, heavy = full; `al testing --set` or the dashboard), the layer's
`#:per-run` (check steps and the gate) or `#:review-rounds` (re-checks in a review round). A step
that names its depth (a batch, a promotion's checks: `#:on-stage`) wins over all of these.

**One check's plan** (`testing.plan_check`): a cmd without a narrower form runs full; then the run
escalates to full, with the reason recorded, for a core path (`#:escalate ((paths …))`), more changed
lines or files than allowed, a lockfile, the agentic layer, a work class listed in
`(class …)`, or (per cmd) a changed path under `#:covers` that no strategy maps to a test
(`unmapped`); a cmd whose miss rate is above `#:max-miss-percent` runs full and says so. Otherwise
selection runs the strategies in order and unites them: `changed-tests` (always first), then
`#:select` (`path-map`, `imports` for Python, JavaScript/TypeScript, Go packages and Rust crates,
`coverage`, `command` through `#:select-cmd`), then `learned` (what earlier misses taught). More
than `#:max-targeted` selected runs full; nothing selected runs the smoke form, else full, never
nothing. The filled command is part of the check's definition hash, so a targeted pass is never
reused where full is asked for.

**Honest accounting.** Every result that carries a depth (a layer with the clause, or a run that
asked for one) records `{depth, asked, why, selected, units, strategies, ran, total, seconds,
full_seconds}`; `ran` comes from the test runner's own summary, `total` and `full_seconds` from the
last passing full run (`agents/testing/suites.json`). The run summary reads `unit=passed (targeted:
ran 3 of 43 tests (changed-tests 1, imports 1): 2 s instead of about 1 min)` and a `testing:` line;
"What the checks proved" has a Depth column; the promotion pack shows the depth per check. Nothing
that was not run is called passed.

**Two guarantees.** Per run: the selected tests pass for the tree that ships (at depth D). Per
batch: the full suites pass for the head of the branch the runs land on. The kit workflow
`verify-batch` (every layer has `adws/verify_batch.py`; `al batch`; the dashboard's "Run the full
suites now") is a synthetic run like a promotion: `open` (pin the head; the commits and runs since
the last green batch), `checks` (every check cmd at `#:on-stage` depth), `record`, `bisect` (red:
only the failing tests, binary search over the first-parent commits since the last green batch),
`fix` (red: `open-fix` starts a fix run with the failing tests, the culprit and its plan; `revert`
prepares `al/revert/<batch>`, created and never moved), `close`. It starts by itself after a landing
when `#:batch ((every N) (after-hours H))` says it is due, when a cycle closes, and a promotion's
checks on a stage head count as that stage's batch. The compiler refuses a zero-touch workflow that
can land on the final stage below full depth without a batch rhythm.

**Misses.** When bisect names a run whose check of that cmd passed below full depth, that is a
miss: `agents/testing/misses.jsonl` (the batch, the run, the cmd, its strategies, the failing tests,
the changed files), and `agents/testing/learned.json` maps those files to those tests, so the next
change to them selects them. `testing.miss_rates` (per cmd and strategy over the newest 50 runs
batches verified) feeds `al kpis`, the dashboard and the full fallback. A failure bisect cannot pin
to a commit (a flaky test) records no miss. Files under `agents/testing/`: `suites.json`,
`batches.jsonl`, `misses.jsonl`, `learned.json`, `coverage/CMD.json` (keyed by commit: a file
changed since that commit is unknown to the map).

The protected gate counts exit codes only, so a targeted pass is exit 0 there; the depth is in the
ledger, the summary, the report and `al explain`'s per-run guarantee. The pull request body built by
the protected `ship.py` does not yet show the depth.

## How work is delivered

A layer may declare **stages**: `(stage NAME #:branch "…" #:from STAGE #:into STAGE #:accepts (…)
#:agents-may-merge #t|#f #:entry merge|squash|rebase|fast-forward #:checks (…) #:deploy-cmd CMD)`. Following
`#:into` (default `#:from`) from every stage leads to one final stage, whose branch is `#:default-branch`. A
`(cycle sprint|release …)` makes one stage repeat: its branch pattern holds `{cycle}` (or `{version}`), and the
open cycle is state of the layer (`agents/cycle.json`), changed by `al cycle LAYER status|start|close`.

**Presets** are declared in the meta layer with `define-preset`: data (the parameters, typed like any field)
plus a function from the checked parameters to `(stage …)`/`(cycle …)` clauses. The compiler splices the
expansion where `(delivery PRESET …)` stands (a declared stage of the same name replaces the preset's in
place), the reference prints each preset's expansion with its defaults, and `al explain` shows it from the
IR (`delivery.expansion`). The compiler's delivery rules are about coherence: one final stage, no cycle in
the graph, valid branch patterns, and above all no zero-touch workflow whose classes route to a stage
with `#:agents-may-merge #f`.

**A layer without stages compiles to the IR it always had**: the `stages`, `cycle` and `delivery` keys and
every ADW's `deliverTo` are left out (the schema lists them as optional), and a run is exactly what it was.

At run time `adw_modules/delivery.py` resolves the run's target (stage, concrete branch, entry method) and
the runner hands every step a **run-scoped layer** whose `default_branch` is that branch. Every existing
reader of `default_branch` (the worktree base, ship's integrate/merge/PR base, the gate's diff base,
fan-out) then follows the target without a change to those modules; `codebase_default_branch` keeps the
codebase's value. A `deliver` step runs before `ship`: it re-checks the class routing, the entry method and
(with a forge) branch protection, merges a stage head that moved, and on a conflict runs one patch round
against it (`deliver-patch-1`, `deliver-implement-1`, the checks again, the review again). A failure there is
handed to a person like any stopped run, with `delivery:<reason>` as the reason. The target is recorded in
`state.json` (`delivery`), so a resumed run keeps it.

The `deliver` step runs only for a ship step that runs: a ship step skipped by `#:when` records no `deliver`
step. The compiler rejects `#:entry fast-forward` with any forge, because through a forge a pull request is
merged by merge, squash or rebase.

**Follow-ups for a person (protected modules).**

- **`ship.py`, entry methods without a forge.** (1) Add `ENTRY_METHODS = ("merge", "squash", "rebase",
  "fast-forward")` at module level; `delivery.entry_supported` reads it and then accepts those methods with
  no other change. (2) In `_integrate`, read `entry = (getattr(layer, "delivery", {}) or {}).get("entry",
  "merge")`: for `rebase`, replace `_merge(worktree, base, sandbox)` with `git rebase <base>` (abort on
  conflict, like `_merge`); for `fast-forward`, do not merge, require `git merge-base --is-ancestor <base>
  HEAD` and otherwise raise `StepFailed("ship", "fast-forward impossible: <branch> moved")`; for `squash`,
  merge as today. (3) In `_merge_locally` and `_merge_via_remote`, for `squash` first create one commit with
  `git commit-tree HEAD^{tree} -p <base> -m <title>` and update or push that commit instead of HEAD
  (sandboxed: `gitsafe.import_objects` on it before `gitsafe.update_ref`). (4) In `ship()`, call
  `client.merge_pr(pr["number"], method=entry)` when `entry != "fast-forward"`.
- **`review.py`, a public patch round.** Offer `patch_round(layer, state, step, request, prefix="",
  env_stack=None) -> dict` (today's `_patch_round` with the request text and the step-name prefix as
  parameters: step names `{prefix}patch-1` / `{prefix}implement-1`), returning `{"patch_plan",
  "implement_output", "checks_rerun"}`, plus public `checks_that_ran(state, adw)` and `model_set(layer,
  command)`. `delivery._conflict_round` would then call `review.patch_round(…, prefix="deliver-")` and drop
  its own `_model_set` and `_checks_that_ran`.

## Promotion between stages

`(promotion FROM -> TO …)` declares how a stage moves into the next: who approves (`human`, `agent`, `auto`
and how many), what must hold (`checks`, `acceptance`), the method (`merge`, `squash`, `fast-forward`,
`cherry-pick`), the version change and tag, the pack's sections, `(deploy ENV)` afterwards and the stages
it is merged back into. The compiler emits `promotions` (one per non-final stage, declared or with the
defaults, `origin` saying which, `hotfix` and the resolved base `adw`) only for a layer with stages, so every
other IR is unchanged.

`al promote LAYER FROM TO` (the layer's `adws/promote.py run`, `adw_modules/promotion.py`) starts a
**promotion run**: an ordinary run of a synthetic ADW (`promote-<from>-<to>`, recorded in
`state.definition` like a single-command run) with the environment, agent and review option of its base
ADW and steps of kind `promote`: `open` (pin the stage head; the changes are the runs delivered into FROM —
`delivery.base`/`head` recorded by every delivery — or whose pull request into it a person merged, and not
yet in TO), `checks`, `pack` (the presentation pack under `artefacts/<id>/pack/`, every file an artefact of
the run), `request` (a pull request, or `promotions/<id>/approval.md`), `approval`, `version`, `merge`,
`tag`, `deploy`, `back-merge`, `close`. So the run has the run log, events, heartbeat, slots, summary and
dashboard page of every run. The `approval` step raises `StepWaiting` until the declared approval exists:
the run ends `waiting` (exit 75) and `al promote` again (or the dashboard's Continue) resumes it; one
promotion per FROM → TO is open (`agents/promotions/<from>-<to>.json`).

**Decisions are files of the layer**: `promotions/<id>/acceptance.jsonl`, one JSON object per decision
(`at`, `by`, `kind` human|agent|forge, `scope`, `decision`, `then`, `comment`, `source`), append-only and
rendered as `sign-off.md`; `promotion.json` records the closed promotion. `promotions/` is committed. Per
person and scope the latest decision counts; a refusal wins over an acceptance. A refused change is
reverted out of the stage in the run's worktree (`git apply --3way -R` of its diff; an agent round of the
base ADW's `#:patch`/`#:implement` when that does not apply), published to FROM by compare-and-swap, and
optionally sent back for a patch run (`launch.start`). Merges, reverts, version commits and back-merges are
plain git in the run's worktree; application refs move only by `update-ref` with the old value (or a push);
a checked-out target gets `al/promote/<branch>`. Agent approvers run `review.run_review` on a run-scoped copy
of the IR whose review option is `agent` with N reviewers.

**Freezes** are stage properties: `#:frozen` periods (UTC days) and `#:frozen-while-promoting`.
`delivery.before_ship` raises `StepWaiting("deliver", 75, why)` and emits `delivery.frozen`.

**Follow-ups for a person (protected modules).** `ship.py` could offer a public `merge_into(layer, state,
worktree, branch, commit, sandbox=None) -> (bool, str)` (today's `_merge_locally` for any branch, checked-out
→ a staging ref and a note); `promotion._publish` already calls it when it exists, and carries its own copy
of the rule (`al/promote/`) until then. `review.py` could offer `run_reviewers(layer, state, option, round,
env_stack)`, so agent approvers need no run-scoped IR copy (next to the `patch_round` follow-up above).

## Integrating a handed-over run

A workflow whose auto review merges hands a run to a person when the merge is not possible: `ship.py`
falls back with `merge-conflict:…` or `ship-failed:…`, and the run ends `handed-over`. `al integrate
LAYER RUN` (the layer's `adws/integrate.py run`, `adw_modules/integrate.py`) starts an **integration
run**, built like a promotion run: an ordinary run of a synthetic ADW named `integrate`, recorded in
`state.definition`, with the agent, review option, environment and `deliverTo` of the *original run's
workflow*. Its steps are `prepare` and `resolve` (kind `integrate`, dispatched from
`steps._execute_step`), that workflow's own `check` steps with `whenAffected` set, its `review` step, an
`approve` step (kind `integrate`) when the layer asks for a person, and its `ship` step. There is no
second engine and no change to a protected module:

- **prepare** runs in the run's worktree, which the ordinary worktree environment cut from the target
  branch's head: `git -c merge.conflictStyle=diff3 merge --no-commit --squash <tip>` through
  `delivery.tree_git` (sandboxed: through `gitsafe`). It records the conflicted files, which are
  generated, and the commits and runs that touched each one between the merge base and the target head.
- **resolve** renders the kit prompt `commands/resolve-conflicts.md` and runs it with `agents.run_prompt`
  on the heavy tier. The IR does not declare this command: the runtime holds its record
  (`integrate.COMMAND`), as `decide.py` holds the decider's, and `al gen` writes the prompt into every
  layer; a command of that name the layer declares wins. Afterwards the runtime checks what the agent
  did: `HEAD` has not moved, every conflicted file is reported as resolved or regenerated, no file the
  integration changed holds a marker line, and no test of either side is gone. A conflict the agent
  reports as `undecidable` fails the step with the question.
- **review** is `review.run_review` unchanged. It hands `state.plan` to the reviewers, so `prepare` and
  `resolve` set `state.plan` to an *integration brief* (`specs/integrate-<id>.md`): the change was
  reviewed before, why it could not merge, the original request and plan, the conflicts and their
  resolutions, and the two diffs to read (against the target head: what lands; against the run's tip:
  what the integration changed in the reviewed work).
- **ship** is `ship.ship` unchanged. It titles the commit from the run's prompt, whose first line is
  `Integrate run <ORIG> (<its request>) onto <target>`, so the commit names the original run. If the
  target moved again meanwhile, the gate's own `merge-conflict` fallback applies and nothing lands.

**Records.** The integration run's `state.json` has `integration` (`run`, `branch`, `tip`, `target`,
`target_head`, `base`, `workflow`, `by`, `clean`, `conflicts`, `report`, `seen_before`, `brief`,
`approval`, `result`, `why`). The original run gets `agents/<orig>/integration.json` (`run`, `status`
queued | running | waiting | landed | failed, `why`, `commit`, `target`, `by`, `at`, earlier ones under
`previous`), and, when the work landed, `agents/<orig>/settlement.json` in the dashboard's format with
`as: landed`, `by: <integration run>`, `via: integrate`: the run is settled without a person. The
dashboard's `attention()` reads both, so the original is under Landed with a link, under Running while
it is being integrated, and stays under Needs you with the reason when the integration did not land.

**Eligibility is rules, not a model.** `integrate.eligible()` refuses with a classified reason
(`integrate.REASONS`) before any worktree exists: the workflow is switched off, the run is unknown, not
handed over, settled or landed, its review rejected it, its reasons name neither a conflict nor a moved
target, its workflow changes the layer or has no ship step, its branch is gone or a person changed it,
it touches a protected path, or it is being integrated already. `integrate.preview()` adds a dry merge
that writes nothing into the application (`git merge-tree --write-tree` with `GIT_OBJECT_DIRECTORY`
set to a temporary directory and the repository's objects as its alternate). The stand-in decider
therefore needs no agent call to start an integration: `integrate.after_run()` (called by the runner
beside `decide.after_run`) starts it, detached, when the run ended handed over and is eligible, the
decider's mode is `auto` and the settings say `"decider": true`.

**Modes: a rejected run goes round once more.** "Give to an agent" (`al assign`, below) continues a
run whose review rejected it. Such a run has no worktree any more (`envs.py` removes it when a run
ends handed over), so it continues the way an integration does, in a mode that `integration_adw(…,
mode=, extra=)` records in the synthetic ADW's `integration` and that adds up to two steps of kind
`integrate`:

- mode `conflict` is everything above, unchanged (its `integration` has no `mode` key);
- mode `blockers`: `prepare`, `resolve`, **`address`**, the checks, the review, `ship`. `address` is the
  review's patch round done once, outside `review.py`: the review step's own `patch` and `implement`
  commands (`agents.run_agent`, steps `address-patch` and `address-implement`), with the blockers in
  the format of `review._patch_round`, a person's approvals and their note as the request. Integrating
  first and then addressing the blockers is the step order; when the target has not moved, `prepare`
  applies cleanly and `resolve` calls no agent;
- mode `proof`: `prepare`, `resolve`, (`address`, only when the proof must be produced: the agent runs
  the plan's proof commands and a changed file outside the artefact globs fails the step), **`proof`**
  (the files found for the original run become the artefacts `found-image`, `found-video` and
  `found-file` through `artefacts.add_files`; otherwise the layer's artefacts are collected again), the
  checks as the workflow declares them (not forced to `whenAffected`: they must run to produce the
  proof), the review, `ship`.

In both modes the review runs **one** round with no further patch round: `integration.review_retries`
is 0, and `steps._execute_step` hands `review.run_review` a per-run copy of the layer whose review
option has that `maxRetries` (`integrate.with_review_retries`, the way `delivery.scope` copies a
layer). `review.py`, the gate and `ship.py` are not changed, so a rejected review never ships: the
attempt ends handed over, `integration.json` on the original says `failed` with `its review rejected
it again: …`, and the next attempt takes that run as its `source` (its branch, tip and blockers; the
records stay on the original). `eligible(mode=, source=, allow_protected=)` asks for a rejected review
in these modes (`not-rejected` otherwise), for a review step that can patch in mode `blockers`
(`no-patch-round`), and lets through a changed protected path a person approved (the gate still judges
it). The brief says which mode it is, lists the blockers, the approvals and the note, and what the
run did about them.

**Give to an agent: one function decides the plan.** `adw_modules/assign.py` `plan(layer, run)` maps
why a run stopped to what an agent does about it; `al assign` (the layer's generated `adws/assign.py`,
written by `al gen` for every layer and required by `al check`, like `adws/integrate.py`), the
dashboard's "Give to an agent" and the stand-in (`assign.after_run`, which the runner calls where it
called `integrate.after_run`) all use it. Three existing mechanisms carry every row: the integration
run in its three modes; `--resume` of the run itself for a failed check (on the heavy tier), a login
failure, a time limit and a died process (a failed run keeps its worktree); and `launch.start(…,
replaces=)` for a run that failed before changing anything. A "needs a person" item (a blocker or a
failed step that matches `assign.PERSON_MARKERS`, a `protected-path:` or `layer-change` gate reason, a
`REQUIRES HUMAN` request) is never given to an agent: the plan's kind is `person`, and
`assign.approve` records the person's approval of exactly that item (its id is a hash of its text).
Layer-change workflows, spending limits, integrity failures and promotions cannot be approved there.
The record is `agents/<run>/assign.json`: `attempts` (`n`, `at`, `by` cli | dashboard | decider, `who`,
`kind`, `note`, `run`, `reason`: the stop key when given, `outcome` started | landed | passed |
stopped, `after`: the stop key afterwards, `why`) and `approvals` (`id`, `kind`, `text`, `path`, `by`,
`at`, `via`). An attempt's outcome is settled lazily by the next reader, from the run it started. The
limits are constants of `assign.py`, not vocabulary: a person gives a run to an agent `MAX_CLICKS` (2)
times, `--override` once more; the stand-in `MAX_STAND_IN` (1) time, only for the kinds `integrate`,
`blockers` and `proof`, and only with `"decider": true` in `templates/integrate.json` and the decider's
mode `auto`. The bulk order (`assign.order`) starts resumes and restarts first, then the integration
runs, which the lock below runs one at a time.

**One at a time.** `integrate.run()` holds `agents/.integrate.lock` (an flock, so it dies with the
process) around `runner.main`. A second request waits, keeping its skeleton `state.json` alive; because
a worktree is created only inside the runner, it starts at the target head that already holds the
first one's result, and eligibility is checked again once it holds the lock.

**Why the settings are a file of the layer.** `templates/integrate.json` (`enabled`, `approval`,
`decider`, `generated`) is committed layer source that `al gen` never writes. A clause of the language
(`(integrate #:approval person #:generated (…))`) would be the natural home, but it changes
`racket/al/private/vocab.rkt`, which a person owns. Follow-up for a person: add the clause and let
`integrate.settings()` read it from the IR first. Two more, neither needed: `ship.py` could add a
trailer `Integrates: <ORIG>` to the commit when `state.integration` is set, and `review.py` could pass
the conflict report to the review command as a third argument instead of relying on the brief.

**Lessons.** `integrate.conflict_report()` counts, over the layer's integration runs, how often each
file conflicted and which pairs conflicted together (`al integrate LAYER --conflicts`); each brief and
summary names the files that conflicted before.

## What a layer can run: the registry

A layer can run more than its definition declares: `integrate` (above), one promotion run per declared
promotion, single-command runs of its commands and hops, the stand-in decider, and the layer-change
workflow of `al improve`. `kit/runtime/adw_modules/registry.py` is the one description of all of it.
`registry.build(ir, layer_dir)` returns the declared workflows, then the kit rows, then the commands and
hops, then the codebase commands that `script` steps run; every entry has the same keys (its kind, where
it comes from, its steps, the input it takes: a request, a handed-over run, two stages; whether and how
a person starts it; its setting and whether that is on; its script and whether it exists). The kit rows
come from one table, `KIT`: a new kit workflow is declared there and nowhere else. The readers build no
list of their own: `launch.catalog_names` and `launch.resolve_target` (so `al run` and `dispatch.py
--start` answer a kit name with the command that starts it), the decider's resume (`registry.resume_argv`:
a workflow's script, `dispatch.py --command` for a single command, `promote.py` or `integrate.py` for a
kit run), `al explain`, and the dashboard's Layers view, start form and run page. `adws/dispatch.py
--registry --json` prints it. The module is standard library only and imports no other runtime module,
because the dashboard and `tools/explain.py` load it by path; the three rules it therefore repeats by
value (the names and settings file of `integrate.py`, the names and steps of `promotion.py`, the script
lookup of `inputs.adw_script`) are kept equal by `kit/runtime/tests/test_registry.py`.
`tests/tools/test_registry_guard.py` generates the full example and fails when an executable script
under `adws/` or an `(adw …)` is not in the registry, or a workflow of the registry is not in what the
dashboard's two views return. The names a kit run records (`integrate`, `promote-FROM-TO`) are reserved:
`al gen` and `al compile` refuse a declared workflow of that name (a check of `tools/al`; the compiler
itself does not have the rule yet).

## Separation from the application

The layer is its own directory (default `<app>.al`, its own git repo). The app is referenced by
path and reached only through `git worktree add <layer>/trees/<id>`. Plans, logs, artefacts,
expertise and docs about runs live in the layer. The single optional exception is the CI workflow
file for the `forge-actions` trigger, which by nature must sit in the app repo; it contains no
logic, only a call into the layer, and it is generated into `forge/` for you to copy deliberately.

`(layer in-repo #:dir "agentic")` is the deliberate alternative: the layer is a directory of the
application repository and is committed with it, so the team shares one layer and reviews changes to
it like code. Run state stays out of git (the generated `.gitignore` covers worktrees, run records,
artefacts, plans, the task list and machine-specific files), so runs leave `git status` clean; worktrees
sit in the ignored `agentic/trees/`, and runs always use the main checkout's layer, never the copy
inside their worktree. Every ADW that is not a layer-change workflow treats `agentic/` as a protected
path, whatever its review option says: the runtime's gate adds `protected-path:` reasons, so an agent
cannot quietly rewrite its own rules.

`(host #:id ID …)` says what a machine that runs an in-repo layer must have. From that one fact set
the generator writes three provisioning styles under `env/hosts/<id>/`: a Home Manager module, a
NixOS module and a POSIX shell script. The three share one list of wanted facts
(`tools/al_provision.py: wanted()`) and one source of unit text (`unit_spec()` in `tools/al`, also
behind `systemd_unit()`), so they cannot drift apart. They are committed and machine-neutral: a
host's paths and user name appear only as module options, set by a host-local wrapper that
`al deploy` writes under `~/.config/al/<name>/` on that host and nowhere else, or as `$HOME` in the
script. Privilege is used only after the plan has been printed with every privileged command, and
only through `sudo -n`. Sandbox images are matched by recipe hash, the runtime's recipe hash
(`adw_modules/sandbox.py: recipe_hash`: the Containerfile, the files it copies from `env/`, the base
image) carried as the label `al.recipe`. A tag or an image id says where an image came
from, not what it was built from. The hash makes "the image that was tested" and "the image this
layer asks for" the same question on every machine, whether the image was pulled, streamed over ssh
or built there.

## Changing a layer

A layer has two kinds of source: `agentic.rkt` and layer-local templates under `templates/`, which
replace a kit template of the same name (`al eject` makes one). Everything else is generated, and
`.al-manifest.json` records a sha256 per generated file, so `al gen` refuses to overwrite a hand edit
instead of losing it. `al improve LAYER "request"` changes a layer through al's own workflow
(`kit/self/agentic.rkt.in`, a `#lang al` program instantiated per layer into the ignored
`.al-improve/`): plan → implement → checks (compiles; regenerates without hand edits, twice, the
second time changing nothing; only the layer changed; `al check`; every ADW completes a dry run
against a pristine clone of the application; `al verify` when cmds or environments changed) →
commit → review → ship. It is an ordinary ADW with `#:changes layer`, which the compiler only accepts
with a human or agent review: there is no zero-touch mode for changes to how agents work.

## Codebases that already have an agent setup

Many repositories already carry agent material: rule files (`CLAUDE.md`, `AGENTS.md`, per-directory
variants, `.cursorrules`, …), prompt libraries (`.claude/commands/`), workflow scripts (`adws/`), CI
jobs that run an agent, Claude Code settings. `al scout` finds and classifies it (tracked files only,
nothing executed) into an `## Existing agent setup` table of the `LLM.txt` draft, and the meta
commands make the authoring LLM build on it instead of beside it.

`al adopt LAYER` brings it in deterministically and idempotently, never touching the application:

| Material | What the layer gets |
|---|---|
| rule files | a `(conditional-doc FILE "Always: …")` per file (per-directory files: "When working under DIR/"), so every prompt tells the agent to read it; the files stay where they are. Agents run with `--safe-mode`, which does not load `CLAUDE.md` by itself, so this is what makes the rules reach them. Turning rules into `#:focus` lines is judgement, left to the authoring LLM, which gets `adopted/RULES.md`: every rule with its source line |
| prompts | a `(command NAME #:template custom #:file "templates/custom/NAME.md")` per prompt, the text copied with a header naming its origin; `$ARGUMENTS` and `$1…$9` work as before; a name taken by a kit template or a layer command gets a `team-` prefix; `{{…}}` is kept literally through `{{open_braces}}`; what does not carry over (`!`cmd``, `@file`, `$10`, `model:` / `allowed-tools:` front matter) is reported |
| workflow scripts | a `(cmd legacy-NAME "uv run adws/…")` and a PROPOSED one-step ADW `(steps (script run #:cmd legacy-NAME #:args ("$prompt")) (review) (ship))` with human review. A script cannot be converted, and is not pretended to be; a native equivalent is a change for `al improve` and a person |
| CI agents, settings, MCP, skills, subagents, an existing al layer | reported, not adopted: a person decides |

The declarations go to `adopted/codebase.rkt` and `adopted/layer.rkt`, which `agentic.rkt` includes with
`(include "…")` inside `(codebase …)` and at the top level; `al adopt` adds those two lines once. An
include rather than a marked region in `agentic.rkt`: `#lang al` resolves it at expansion time and
reads the file as syntax, so every compiler error about an adopted clause points into the included
file at that clause; the author's file is touched once and never rewritten; and the adopted files are
al's own, regenerated whole and protected like generated files (`adopted/.al-adopt.json` records a
sha256 per file; a hand edit makes the next `al adopt` refuse). The report (`adopted/REPORT.md`)
lists what a person must decide, with both texts where a rule mechanically contradicts the layer
(e.g. "never commit" against the kit's commit prompt). `al check` verifies the adopted files and the
rule files they point at still exist, and says when an adopted original has changed.

A `(script NAME #:cmd C #:args (…))` step runs a declared codebase cmd in the run's environment, with
the arguments substituted and shell-quoted and the run context in `ADW_*` variables. Its output is the
step's output, a non-zero exit fails the run (a shipping workflow hands its work to a person), and it
is not a check: no resolve loop, and the gate never counts it as verification.

## Environments the repository already describes

A sandbox that drifts from the repository's own setup fails checks for reasons that are not the
change's fault, so al reuses what the repository declares instead of retyping it.

- **Detection** (`tools/al_envscan.py`, called by `al scout`): reads tracked files only, at most 256 KiB
  each, and executes, imports and evaluates nothing (flakes by pattern, not `nix eval`): flake dev
  shells, `shell.nix`, devenv, Containerfile stages and `EXPOSE`, dev containers (JSON with comments),
  compose services, version files, `pyproject.toml` / `package.json` / `go.mod`, lockfiles, browser test
  dependencies, CI workflows (`apt-get install`, `setup-*` versions, job services) and `.env` examples
  (names only: the rest of the line is never stored). It reports conflicts between sources and
  proposes a declaration whose every line ends in `; from FILE[:LINE]`. Text and `--json` come from the
  same data.
- **Reuse** (the generator): the repository's files are read with `git show <default branch>:<path>`,
  never from the working tree and never executed. `#:flake` writes `env/flake.nix`, a wrapper whose
  `app` input is `git+file://<app>?ref=<branch>` and whose shell takes the repository's shell through
  `inputsFrom`; it names this machine's checkout, so it is a local (git-ignored) file. `#:containerfile`
  and `#:devcontainer` write a **thin** `env/Containerfile` (`FROM` the repository's image, then the run
  user's home, packages, `#:setup`, a browser, the agent CLI when it runs inside, `ENTRYPOINT []`).
  `env/source.json` tells the runtime what to build and publish; `env/SOURCE.md` and `al gen`'s `note:`
  lines say what was not honoured and why.
- **Runtime** (`adw_modules/envsource.py`, without changing `envs.py`): after the worktree exists it
  builds the repository's Containerfile from the run's worktree as `localhost/al-<layer>-base:latest`,
  labelled `al.env-fingerprint`, under a file lock; a matching label builds nothing. Before the
  preflight, `sandbox.ensure_image` then builds the thin layer as `al-<layer>:<recipe hash>`, whose hash
  includes the repository's source (Containerfile and context on the default branch, or the dev
  container's image), and re-points `:latest`, which `envs.py` uses unchanged. A flake's `app` input is relocked at
  each run start. A dev container's `forwardPorts` are merged into the option's `publish` in memory, so
  the existing per-run port leases apply.
- **Prepare** (`adw_modules/prepare.py`): `(environment worktree #:prepare (cmd …))` runs once per run,
  before the first step, through `stack.wrap` (the container and its caches, or the Nix shell), each
  command in a `checks` slot. It is recorded as the step `prepare` of kind `setup`; a failure ends the
  run at once with the output; a resumed run does not prepare again. Without `#:prepare`, a dev
  container's `postCreateCommand` is the prepare step. The step name `prepare` is reserved.
- **Platforms** (`tools/al_platform.py`, `al doctor --platform`): OS, architecture, podman (machine,
  rootless), Nix (flakes), a host browser, the environment kinds that can run, and whether zero-touch can
  (it needs podman, by the compiler's rule).

Not reused, on purpose: starting services from a compose file (they are proposed as `(service
container …)`), a dev container made of compose services, devenv and `shell.nix` (translated to
`#:packages`, not wrapped), and editing any repository file.

## Supporting the LLM: our own commands

`meta/commands/` holds the prompts that drive an LLM through the pipeline: `scout-codebase`
(complete the deterministic draft; every command must cite evidence), `author-layer`
(LLM.txt + reference → `agentic.rkt`, compile, repair), `repair-layer`, `review-layer`.
Inside each generated layer the meta HOPs (`meta-command`, `meta-adw`, `meta-expert`) let agents
extend the layer through the same compiler-checked path: what gets corrected is the layer, so the
next run is right too, rather than one run's output.

Fabric patterns are a library of methods: `#:fabric (review_code)` on a command vendors the pattern
into the layer and the command applies it, while the command's own Report contract stays in charge
of the output format.

## Layout of this repository

```
racket/al/         #lang al: meta.rkt (meta-DSL), vocab.rkt, compiler.rkt, schema.rkt, reference.rkt, tests/
schemas/           agentic-layer-v1.schema.json      (generated)
docs/              AL-DSL.llms.txt (generated), DESIGN.md
tools/al           init | scout | adopt | compile | gen | check | explain | eject | improve | dashboard | verify | cycle | promote | decide | integrate | voice
tools/al_delivery.py  read-only detection of the branching style in use (`al scout`'s `## Delivery`)
kit/templates/     command, HOP and expert templates   kit/schemas/  JSON output contracts
kit/runtime/       adw_modules (generic IR interpreter; promotion.py behind a layer's adws/promote.py, integrate.py behind adws/integrate.py), triggers, voice (incl. serve.py, the dashboard's speech service), dispatch
meta/              LLM.txt template + the meta commands
tests/             acceptance script over the testbeds
testbeds/ layers/  cloned public repos and their generated layers (git-ignored)
```

## Scaling out: worktrees, containers, limits

Every run gets its own git worktree (`trees/<adw-id>`), branch and leased port block; that is the
default environment and what makes N agents on one repository safe. On top of that:

- `(concurrency slots #:max-runs 4 #:max-agents 2 #:max-checks 2)` — layer-wide limits enforced by
  file-lock slot pools, shared by every trigger and by manual runs. Locks die with their process,
  so a crashed run cannot leak a slot. Port blocks are leased under the same kind of lock.
- `(environment podman #:scope run …)` — one container per run; every step execs into it, so
  installed dependencies persist for the run. `#:caches ("uv" "cargo")` are named volumes shared
  across runs. Only the worktree and the app's `.git` are mounted (git needs it); the application's
  working tree is not.
- `#:agent-inside #t` — the agent CLI itself runs in the container, with the layer mounted
  read-only except where a run writes, and `#:credentials (claude)` making just that agent's auth
  available. `bypassPermissions` is only accepted in this configuration.
- A zero-touch ADW (auto review that merges or deploys) must have a podman option in its stack.
- Worktree hygiene: `#:sparse` for monorepos, `#:gc-days` for kept worktrees of old runs, and
  branches that hold nothing unshipped are deleted with their worktree. The work of a workflow without
  a ship step is committed to its branch and copied into its artefacts before teardown (the runtime's
  `keep.py`), so that branch is kept; when keeping fails, the worktree is kept instead.

- `(fan-out NAME #:adw CHILD #:count N #:pick best|first|all)` — race one task across N independent
  runs of a child ADW. Each candidate is a full run with its own worktree, container, port lease and
  slots; the winner (fewest resolve attempts, then smallest diff) is merged into the parent run, which
  goes on to check, review and ship it. Losers' branches are removed and their ids recorded.

### Runs on other hosts

`(placement auto #:hosts (home build-1 build-2) …)` lets a run execute on another machine. `home`
is the machine that holds the layer; every other name is a declared `(host …)`. Host addresses are
not in the layer (`AL_HOST_<ID>`, read like every private value), and neither is the git source
(`#:source-env`, default `AL_GIT_SOURCE`: one URL per machine, as seen from there). The design moves
the existing run path instead of writing a second one:

- **The scheduler** (`adw_modules/placement.py`) runs in the home process that would have run the
  workflow. It asks every listed host for its load with one call each, at once (`adws/host.py load`
  through `adw_modules/hostlink.py`: the person's own ssh, `BatchMode`, nothing stored), writes the
  answers to `agents/hosts.json`, picks a host by `#:prefer` and reserves it in the same file under
  one lock, so runs that start together spread. A workflow the compiler pinned (`pinnedHome`: a cmd
  with `#:needs-home`, a waiting script, a layer change, no worktree) stays on `home`. A host is
  skipped, with its reason in the `placement.decision` event, when it does not answer, runs an older
  layer, has no login for the workflow's agent, is reserved for other classes, or when the layer on
  the home machine is not what the git source holds.
- **The clone.** On the host, `adws/host.py start` (`adw_modules/selfrun.py`) creates
  `$XDG_STATE_HOME/al/<name>/runs/<id>/`, clones the base branch tip from the git source into
  `repo/` (a real `.git` directory, shallow when the source allows it, through a local mirror cache
  when there is one; no remote is configured and the URL is written to no file there), takes the
  layer from that clone (an in-repo layer) or from the layer's own source at the exact revision, and
  starts that layer's own `adws/adw_<name>.py` with `AL_SELF_CONTAINED=1`. From there the unchanged
  environment stack, sandbox, steps and review run as at home: same worktree layout, same read-only
  `.git` mount (now the clone's), same quarantine, same hardening flags. All runs of a host share
  one slot directory (`AL_SLOTS_DIR`), so the limits hold per host. The sandbox image is pulled by
  its recipe tag from the `(registry …)` and its label checked; it is built there only as a fallback.
- **The push.** A `(ship)` step is not run on a host. The run commits what is pending, imports the
  commit into its clone the usual way and pushes `<sha>:refs/heads/<run branch>` to the git source:
  no `+`, no `--force`, and refused for any branch that is not the run's own (`<class>/<id>-<slug>`),
  for the default branch and for every stage branch. The run ends `delivered`. After every step that
  moved HEAD the branch is pushed too, with a copy of the state (`pushed-state.json`): where a lost
  run starts again. A run that fails pushes its work the same way and makes no handoff.
- **The take-over.** The home process (`adw_modules/remoterun.py`) then builds the normal environment
  stack at home, fetches the pushed branch into a temporary bare repository, refuses it unless it is
  exactly the commit the run reported, feeds the new objects through `git index-pack --strict` into
  the run's quarantine (a sandboxed stack) or the application's store, moves the fresh worktree to
  that commit and runs what is left of the workflow, its `(ship)` step, through the same code as a
  resumed local run. So the gate re-runs the required checks on the final tree at home, and only the
  home machine moves the default branch. `ship.py`, `review.py`, `gitsafe.py` and `envs.py` are
  unchanged. A run that failed on its host fails at home right after setup, and the usual handoff
  hands its pushed work to a person.
- **The record is pulled, never pushed** (`adw_modules/recordsync.py`). Every two seconds the home
  process sends its cursor and the host answers with what the cursor has not seen: append-only files
  (`events.jsonl`, `run.log`, `raw.jsonl`, check logs) as byte ranges, written at home only when the
  home file has exactly the size the range starts at; other files whole, replaced atomically. A
  dropped connection is caught up from the cursor, so nothing is duplicated. Every path is checked
  before anything is written (only this run's `agents/<id>/`, `artefacts/<id>/` and its plan files;
  no link is followed), and `state.json` is accepted only as the state of this run and workflow, with
  the host's paths rewritten to the home machine's. While the run is on the host the home process
  writes nothing to `events.jsonl` and `run.log`: they are the host's bytes; its own start events
  travel in the seed and open the host's file.
- **Never sent:** the login copy (`agents/<id>/credentials/`), the object quarantine (`objects/`),
  any address or URL. The host's own values (the git source, an agent token) come from its env file;
  of those only the agent token variables of the workflow's stack reach the run's environment.
- **Lost.** A host that gives no usable answer for `#:lost-after` seconds leaves the run `lost`, with
  what was last received. `--restart` starts it on another host: the new clone fetches the run branch
  too, the worktree starts at the pushed commit, and the steps recorded in the pushed state are
  skipped. `--follow` re-attaches a home process to a run whose proxy died.

Not done here: per-host port leases (two runs starting in the same instant on one host could lease
the same block; keep `#:max-runs-per-host 1` for a sandbox that publishes ports), spreading fan-out
candidates over hosts (they run where their parent runs), and stopping a remote run from home.
## Toolchains: one declaration, three consumers

Before toolchains, a layer said what its environments contain three ways: nixpkgs names for the dev
shell and the Home Manager module, Debian names and raw `RUN` lines for the container. They drifted,
and the container was not reproducible (unpinned apt, a `curl | bash` installer for the agent CLI).
`(toolchain nix …)` is the one declaration:

| Declared | Generated (`env/`) | Consumed by |
|---|---|---|
| `#:packages`, `#:features`, `#:env`, `#:shell-hook` | `toolchain-<id>.nix` (imports the kit's `features.nix` and `agent-cli.json`) | everything below |
| `(environment nix-devshell #:toolchain ID)` | `flake.nix` `devShells.default` | `nix develop path:env` |
| `(environment podman #:toolchain ID …)` | `flake.nix` `packages.image-<id>` (`dockerTools.streamLayeredImage`) and a `Containerfile` with the `#:setup` lines on top | `al sandbox LAYER build`, `al verify`, runs |
| `(environment home-manager #:toolchain ID)` | `home-manager.nix` | the user's Home Manager, against their own nixpkgs |

The compiler checks the references (an environment gives `#:toolchain` or `#:packages`, never both; no
`#:nixpkgs` or `#:base-image` beside a toolchain; no `#:nested-engine #t` on a podman option with a
toolchain, because the nested engine is installed with Debian packages and a `COPY` into
`env/Containerfile`, which the Nix-built image cannot take), and that `agent-cli` is only asked for where an
agent runs inside an environment using the toolchain and is one al knows a CLI for. It derives
`agentClis`, the agent kinds whose CLI the bundle installs. A layer without a toolchain compiles and
generates byte for byte what it did before (`toolchains` and an empty `toolchain` are left out of the IR).

**Pinning.** Each toolchain is one flake input, `nixpkgs_<id>`, locked in `env/flake.lock`, which is
committed (the layer's `.gitignore` excepts it). `al gen` writes the lock when there is none and keeps
it otherwise; `al sandbox LAYER update` re-pins and prints the revision and date per input. Unfree packages are
allowed by name only, from `agent-cli.json` (`claude-code`). An agent CLI comes from nixpkgs when the
pinned nixpkgs has it at the recorded minimum version, else from the vendor's release, fetched by the
recorded SRI hash.

**Two ways to build** (`kit/runtime/adw_modules/images.py`, standard library only):

- *nix*: `nix` is on the host's PATH; it builds the stream script and al pipes it into `podman load`.
- *container*: no Nix on the host. Nix runs in `docker.io/nixos/nix@sha256:…` (pinned by digest),
  with `--option sandbox false` and its store in the volume `al-nix-store-<first 12 of the digest>`,
  so a new builder never runs on an old store. The builder sees only `env/`, mounted read-only except
  for `lock` and `update`, and its volume: never a worktree or a credential.

Either way the Nix image is tagged `localhost/al-<layer>-toolchain-<id>:latest`, and `podman build` of
`env/Containerfile` puts the `#:setup` lines on top. The result is the layer's one sandbox image,
`al-<layer>:<recipe hash>` (see "Sandbox image"), with the labels of every sandbox image (`al.layer`,
`al.layer-dir`, `al.recipe`) and `al.toolchain.key`: a sha256 over the flake, the lock, the toolchain
file, the kit files, the setup lines and the builder digest. For a recipe option with a toolchain the
recipe hash covers that key, so a changed lock or declaration is a new recipe, built at the next run,
and an unchanged one is never built; `al-<layer>:latest` is re-pointed at it. `sandbox.build_image`
builds it through `images.ensure` (which takes the recipe's ref and labels) and then does what it does
for every image: the build events, `:latest`, the prune. The doctor's fix hints name the toolchain
(`#:packages`, feature `browser`, feature `agent-cli`) instead of the podman option.

**Image layout guarantees.** The image has a normal filesystem layout, so the runtime's hardening flags
(`--cap-drop=ALL`, `no-new-privileges`, `--userns=keep-id`, optionally `--read-only`) work unchanged:
`/bin/sh`, `/usr/bin/env`, bash and the core utilities, a CA bundle (`SSL_CERT_FILE`), a regular writable
`/etc/passwd` and `/etc/group`, and `/tmp`, `/tmp/home` (`HOME`) and `/work` with mode 1777. In the image
only, `chromium` is a wrapper adding `--no-sandbox --disable-dev-shm-usage`: the container is the
sandbox, and chromium's own cannot start with every capability dropped.

**Follow-ups for a person (protected modules).** None is required: every run goes through the preflight,
which ensures the recipe image (`sandbox.ensure_image`) before a container starts, and `al verify` and
`al sandbox LAYER doctor|shell` do the same, so `envs.py` finds `al-<layer>:latest` and never builds. For
symmetry a person may make `EnvStack._ensure_podman_image` call `sandbox.ensure_image(self.layer)`
(imported inside the function, `sandbox` imports `envs`) instead of its own `podman build`; today that
fallback build would fail on a toolchain layer whose Nix base image was never loaded, with podman's
message about the missing base.

## What the sandbox guarantees

A run with a podman option is a *sandboxed run*. The container sees the worktree, the app's `.git`
**read-only**, its own worktree admin directory and a per-run object quarantine — nothing else of
the host. Consequences, each covered by a test against real podman:

- Code in the container cannot write the app's hooks or config, move its branches, or add objects
  to its object store. The worktree is detached; only the host creates the run's branch.
- Every git command the host runs against that worktree first freezes the container and verifies
  the git metadata the container could have rewritten, then runs with pinned paths and hooks off.
  An agent that runs on the host is wrapped in the same guard for the length of its step.
- Shipping fixes one commit, imports exactly its objects through `git index-pack --strict`, gates
  on that commit and ships that commit. Local ref updates are fast-forward-only with an old-value check.

`al verify` is the counterpart for truth: the compiler proves the layer is coherent, `verify` runs
every declared codebase command in its declared environment on a clean worktree and proves the
descriptor is true. It verifies every distinct environment stack of the workflows once (or one
workflow's, with `--workflow NAME`) and labels each result with its stack.

### Sandbox image

The image of a layer's podman sandbox is `al-<layer>:<recipe hash>` (`adw_modules/sandbox.py`). The
hash (12 hex digits of a sha256) covers the generated `env/Containerfile`, every file it `COPY`/`ADD`s
from `env/` (content and executable bit; a source outside `env/` is refused) and the podman option's
`#:base-image` string. Other files in `env/` (the nix flake) do not count, and a moved upstream tag of
the base image is not a recipe change (`al sandbox LAYER build --no-cache` takes it). So a changed
package or `#:setup` line is built at the next run with no manual step, and an unchanged recipe never
is. The generator writes one Containerfile, from the first podman option; every podman option of the
layer shares that image and differs only in how a container is started.

- **Build.** At most once per recipe, also when several runs start together (a file lock per hash),
  with the output streamed live (to the terminal, or to the run's `run.log`), labelled `al.layer`,
  `al.layer-dir` and `al.recipe`. `al-<layer>:latest` is re-pointed at the current hash. A run
  records the image it uses in `state.json` (`image`); a build is recorded as `env.build.start` /
  `env.build.end`.
- **Retention.** Old images stay while anything uses them: a container of the layer directory, or a
  live run whose `state.json` names them. After every build the old images beyond the 2 newest unused
  ones, and those older than 14 days, are untagged (`podman rmi`, never forced); `al sandbox LAYER
  prune` removes every unused old one.
- **Preflight.** Before a run's first step the runtime ensures the image and runs the fast checks
  of `al sandbox LAYER doctor` in the run's own container (started exactly as its steps will use it): the
  first executable of every codebase cmd the workflow runs (check and script steps, and the review's
  `#:require-checks` when it ships), the agent CLI and a credential when the agent runs inside (variable names and
  file paths only; values are never printed), git in the worktree, an init process, every cache
  volume writable, enough free disk, and — when the workflow collects image, video or trace artefacts
  — a headless browser, and the sidecars' ready commands. Any miss fails the run at once, before any
  step, with a table naming each missing thing and the declaration to change (`env.preflight`).
  A podman option's `preflight` list (default `["services", "browser"]`) turns those two optional
  checks off for that environment. A workflow that runs nothing in the container (no codebase cmd, and its
  agent outside it, or a dry run's stand-in agent) skips the preflight and builds nothing.

### An engine for the sandbox

`(environment podman … #:nested-engine #t)` gives the run's container a rootless podman of its own,
for tests that build or start containers (`adw_modules/nested_engine.py`). The image gets
`podman fuse-overlayfs` and the two configuration files below (`al gen` writes them to
`env/al-engine/`, the Containerfile copies them to `/etc/containers/`; the recipe hash covers them).
Layers without the flag generate byte-identical files.

**The flag delta.** Compared with an ordinary run container exactly one device and one volume are added:

| Flag | Why |
|---|---|
| added: `--device /dev/fuse` | fuse-overlayfs mounts the inner engine's image layers. The kernel's overlay cannot stack on the container's own overlay root, and the vfs driver copies every layer in full. |
| added: `-v al-engine-<layer>-<run>:/al-engine:U` | the inner engine's own storage, one volume per run, owned by the run's user, removed with the run. |
| kept: `--cap-drop=ALL` | the inner engine runs with a single uid mapping, so it never runs `newuidmap` or another setuid binary and needs no capability. |
| kept: `--security-opt=no-new-privileges` | single uid mapping: no `newuidmap`, no setuid exec. |
| kept: `--userns=keep-id` | the inner engine's user is the run's user, which owns the worktree and the engine volume. |
| kept: default seccomp profile | it allows the unprivileged `unshare(CLONE_NEWUSER)` rootless podman needs. |
| kept: default AppArmor profile | a host whose profile forbids user namespaces gets an `engine:userns` miss instead of a weaker profile. |
| kept: masked `/proc` paths | inner `pidns="host"`: crun bind-mounts the outer `/proc`, nothing is unmasked. |
| kept: `--memory`, `--cpus`, `--pids-limit` | inner `cgroups="disabled"`: everything the engine starts stays in the run container's cgroup. |

There is no `--privileged`, no `--cap-add`, no `unmask`, no `/dev/net/tun` and no host socket.

**The inner configuration** (`containers.conf`, then `storage.conf`), each line and why:

- `netns="host"`: inner containers use the run container's network namespace, which is already
  private to the run and follows its `#:network`; no new network, no `/dev/net/tun`.
- `pidns="host"`: crun bind-mounts the outer `/proc` instead of mounting a new procfs, which the
  masked `/proc` paths would forbid.
- `userns="host"`, `ipcns="host"`, `utsns="host"`, `cgroupns="host"`: no new namespaces that need
  anything the run container may not do, and none would add isolation from the run itself.
- `cgroups="disabled"`: inner containers create no cgroup, so `#:memory`, `#:cpus` and
  `#:pids-limit` cover every process they start.
- `log_driver="k8s-file"`, `[engine] cgroup_manager="cgroupfs"`, `events_logger="file"`,
  `runtime="crun"`: no systemd or journald inside a container; crun runs rootless without a helper.
- `driver="overlay"` with `mount_program="/usr/bin/fuse-overlayfs"`, `rootless_storage_path=
  "/al-engine/storage"` (the run's volume) and `ignore_chown_errors="true"` (one uid inside).

**One uid inside.** The keep-id user has no `/etc/subuid` range in the container, so the inner engine
maps a single uid and never uses `newuidmap` (a setuid binary). Inner images whose processes switch
to another user (postgres, for one) do not run, and files another user owns in an inner image become
the engine user's. A subuid range would need `newuidmap`, so `no-new-privileges` off and the `SETUID`
/ `SETGID` capabilities; that is deliberately not done. Docker-API compatibility (`DOCKER_HOST` for
testcontainers) is not provided either.

**Volume lifecycle.** The preflight (and `al sandbox doctor`) creates `al-engine-<layer>-<run>`,
labelled `al.layer`, `al.layer-dir` and `al.run`; a resumed run reuses it. It is removed at the end
of the run, whatever the outcome. A crashed run's volume is removed by the next run of the layer, or
by `al sandbox LAYER prune`, once its run is not alive.

**Preflight rows.** With the flag the doctor script adds `engine` (podman in the image),
`engine:fuse` (`/dev/fuse` in the container), `engine:storage` (`/al-engine` writable, and podman's
graph root under it), `engine:isolation` (no `/run/podman/podman.sock`, `/run/user/*/podman/podman.sock`
or `/var/run/docker.sock`, and no `CONTAINER_HOST` / `DOCKER_HOST`; it names the path or the
variable, never a value) and `engine:userns` (`podman unshare true`), and the host rows
`engine:host-fuse` and `engine:host-userns` (`user.max_user_namespaces`;
`kernel.apparmor_restrict_unprivileged_userns=1` is reported as a detail). A miss stops the run before
its first step, with a fix per row; al never turns AppArmor or seccomp off for a run.

**Pending a person.** The flags are passed by the protected `kit/runtime/adw_modules/envs.py`, which
an agent does not edit. The change is one line in `EnvStack._podman_base`:

```python
def _podman_base(self) -> list[str]:
    from . import nested_engine
    option = self.podman_option or {}
    namespace = ["--pod", self._pod] if self._pod else self._network_flags()
    return [*namespace, "--security-opt", "label=disable", *hardening_flags(option),
            *nested_engine.podman_flags(option, self.layer, self.state), *self._labels(),
            "--memory", str(option["memory"]), "--cpus", str(option["cpus"]),
            "-w", str(self.state.worktree), *self._podman_mounts()]
```

For a podman option with `#:nested-engine #t` it passes exactly `--device /dev/fuse` and
`-v al-engine-<layer>-<run>:/al-engine:U`; for every other option nothing. Until a person applies
it, every nested-engine run is refused at preflight: the `engine:fuse` row misses with a fix naming
this change. Afterwards the person regenerates the al layer's own copy (`al gen`). If a host turns
out to need another flag, it is not added anywhere else: it goes to a person with the failing probe.

## Isolation per run

Every run is isolated on six axes, and none of them depends on runs being polite to each other:

| Axis | Mechanism |
|---|---|
| Files | Own git worktree; with podman only that worktree is mounted. Other runs' worktrees, state, artefacts and object quarantines are not mounted at all, and neither is the application's working tree. |
| Git | The app's `.git` is read-only in the container; new objects go to a per-run quarantine; only the host moves refs, after freezing the container and verifying its git metadata. |
| Network and ports | Each run has its own network namespace, so the app inside binds its usual ports and runs cannot collide. `#:publish (8000)` exposes a container port on `127.0.0.1` at a host port leased to that run (`ADW_PUBLISHED_8000`); nothing else is reachable from the host. Leases are tied to a live process. |
| Services | `(service container #:id db #:image … #:ready-cmd …)` listed in `#:services` gives each run its own sidecar instances in a per-run pod (shared localhost, no mounts). Parallel runs never share a database. |
| Privileges and resources | `--cap-drop=ALL`, `no-new-privileges`, a pids limit, memory and CPU limits, optional read-only root, per-container HOME and /tmp. Layer-wide slot pools cap runs, agent calls and checks. |
| Machine | With `(placement …)` a run may execute on another host, in its own clone of the git source under that host's state directory. It shares nothing with the home machine but that source: it can push only its own branch there, never forced, and the home machine imports that branch strictly and decides the merge itself. Slot pools are per host. |

Stale containers and pods of dead runs are removed when the next run starts, selected by label so
one layer never touches another's.

If anything fails *after* the gate decided to ship — the default branch moved, a push was rejected,
the forge refused the merge — the run falls back to the human path: a PR (or `PR.md` without a
forge) with the reason recorded, the commits kept on the run's branch, and no deploy. A git
integrity failure is never turned into a PR.

## Model tiers and spending limits

An agent declares named model tiers: `base` and `heavy` always exist (`#:model-base` / `#:model-heavy`
are shorthand for them, with defaults), `#:models ((fast "…") (heavy "…" #:effort high))` adds or sets
others, and both forms compile to one list in the IR (`agents[].models: [{tier, model, effort}]`).
Commands, hops and run steps name a tier with `#:model-set`; the compiler rejects, at the ADW's
clause, any tier a workflow can reach that the agent running it does not declare (run steps on their
own or the ADW's agent, resolve/patch/implement on the ADW's agent, the review command on the review
option's `#:agent`). At run time the tier of each agent call is, first match winning: `--model-set`
(or `AL_MODEL_SET`), the run step's own `#:model-set`, the request's tier (a task tag or an issue
keyword naming any declared tier), the command's; `--model TIER=ID` (or `AL_MODELS`) then remaps that
tier's model id for the run. Every call is recorded with its tier, model and reason in `state.json`
(`agent_calls`) and in its `agent.*` events. Effort goes to the CLI only where it has a way to take it:
Claude Code `--effort`, codex `-c model_reasoning_effort=…`.

`(budget limits #:run-usd #:step-usd #:day-usd)` and `--max-cost` / `AL_MAX_COST` (which replaces the
run limit for one run) are checked around every agent call, with the cost each transcript reports
(codex and custom agents report none and count as $0, which the summary states). Before a call:
today's spending in the layer (local date, from every run's `state.json` cost, or its events for
older runs) must be below the day limit. After a call: a call over the step limit, or a run over its
limit, raises a budget stop (`budget:<limit>: spent $X of $Y`), which is never retried and goes
through the ordinary handoff path, so the work is committed and a person is told. Claude Code also
gets `--max-budget-usd` with what is left under the tightest limit, so a single call stops early; a
call the CLI ends at that cap is reported as the limit that set it. A run does not start at all once
the day limit is reached (exit code 3), and the cron trigger claims nothing while it is.

Subscription usage. When the agent CLI runs on a subscription login, the dollars above are an estimate
at API prices, and what stops work is a usage window (for Claude Code the 5-hour and the weekly one).
The runtime (`usage.py`) reads only two events of each call's stream-json transcript, after the call:
`system`/`init` (`apiKeySource`: `none` = subscription; `ANTHROPIC_API_KEY`, `apiKeyHelper`,
`/login managed key` = API key; anything else = not said) and `rate_limit_event` (`rate_limit_info`
with `status`, `resetsAt`, `rateLimitType` and `unifiedWindows`, one entry per window with
`utilization`, `resetsAt`, `status`). No credential file, network endpoint or private CLI state is
read. Each call records its billing mode (`agent_calls[].billing`, `agent.end`); a run with
subscription calls has `cost.billing` (subscription / api-key / mixed) and `cost.api_equivalent`,
and its summary labels the dollars "API-equivalent" (the dollar limits keep counting them). Every
report becomes `limit.status` events, `state.usage`, and the latest report per window in the
layer-level file `agents/subscription-usage.json` (written atomically under an flock on
`agents/subscription-usage.lock`):

```json
{"version": 1, "billing": "subscription", "api_key_source": "none", "billing_at": "…", "billing_run": "1a2b3c4d",
 "windows": {"seven_day": {"window": "seven_day", "status": "allowed", "used": 0.57,
                           "resets_at": "2026-11-19T14:00:00+00:00", "at": "2026-11-15T18:57:41.294Z",
                           "run": "1a2b3c4d", "step": "build"}}}
```

A call that failed and whose transcript has a `rejected` report is a limit stop
(`limit:<window>: the <label> subscription limit is reached (the CLI refused the call); resets …`):
like a budget stop it is never retried and goes through the ordinary handoff. `(budget limits
#:limit-stop-percent N)` adds a threshold: before an agent call, when the file says that N% or more of
a window is used and that window's reset has not passed, no call starts. With `#:on-limit stop` (the
default) the run is handed over, and a new run does not start at all (exit code 3; the cron trigger
claims nothing). With `#:on-limit wait` the run sleeps until the reset and continues, unless the reset
is further away than `#:limit-wait-minutes` (default 300) or unknown, in which case it is handed over;
a waiting run keeps its `runs` slot and holds no `agents` slot. The threshold is off while the last
call ran on an API key. What is not known: usage is only as of the last agent call of this layer (the
CLI offers no way to ask without a call, so use by other tools on the same account shows at the next
call); the event carries no time of its own; when the CLI gives no share, only status and reset time
are recorded; a refusal that leaves no `rejected` report stays an ordinary agent error; codex and
custom agents report nothing.

## Proof on the pull request

With a Forgejo forge the run attaches its image artefacts, and the keyframes of any video, to a "Proof"
comment on the pull request (at most eight pictures; a video is linked). A human reviewer therefore sees
what the checks captured without leaving the forge, and a zero-touch merge leaves the same evidence on
the record. GitHub has no attachment API through `gh`; there the route is an S3-compatible store with a
public base URL. The web app demo in `tests/webdemo` exercises both review styles end to end.

## When a run cannot finish

A run of a shipping workflow that stops part-way (a check that will not go green, a failed agent
step, a failed review) does not just end as "failed". It commits what it has, publishes its branch
through the same safe paths as shipping, and opens a pull request marked `[needs human]` (a draft
where the forge supports it; `PR.md` without a forge). The body states why it stopped, with the tail
of the failing log, and collects every `REQUIRES HUMAN` request from the plan and the agents'
reports, including the code block each one introduces. The run still counts as failed, so triggers
and the task list see a failure; the difference is that a person is pointed at the work and told
what it needs. A tampered sandbox is never handed over, and nothing is merged or deployed.

**The stand-in's scope.** A stand-in may take such decisions for a person, and only inside a scope
the layer declares: `(stand-in off|suggest|auto #:may (KIND …) #:never (KIND …) #:max-actions-per-run N
#:max-per-hour N #:escalate-to (dashboard notify))`. The kinds are the decisions a person is asked
for: `resume`, `restart`, `answer`, `integrate`, `settle`, `land`, `promote`, `retry-job`. A kind in
`#:may` it takes alone (mode auto), every other kind it only proposes, a kind in `#:never` it neither
takes nor proposes; without a declaration the scope is `resume`, as before. It sits beside the review
and the gate, never in their place: `land` runs the run's ship step again, so the same gate decides
on the current target, and `promote` records one approval as `stand-in` and continues the promotion,
whose approval rule decides. Limits that no setting lifts are checked in code after the model
answered and again before the action (`adw_modules/standin.py`, `hard_limit`): it never acts on a run
a person touched since it stopped, never overrides a rejected review, never lands work with a
protected-path change or a failing required check, never approves its own work, and a call that
changed a file is discarded. Every decision, the stand-in's and a person's, is one line in
`agents/<id>/decisions.jsonl` and in `agents/stand-in/log.jsonl` (who, what, why, what it read, what
happened next). The dashboard is its control room and goes through the layer's own
`adws/standin.py`, as starts go through `adws/dispatch.py`.

## Telling a person

Runs happen while nobody is watching, so a layer can declare notifiers: `(notify email)` sends a
short e-mail over SMTP (standard library only), `(notify command #:run "…")` runs a command of the
person's choice, and `(notify none)`, the default, sends nothing. Several may be declared.

**Events.** Each notifier lists the events it reports (`#:events`, default `(needs-human failed)`),
and a run reports exactly one:

| Event | The run |
|---|---|
| `needs-human` | failed and was handed over (see above), or was stopped by a spending or usage limit |
| `failed` | failed otherwise (nothing was handed over, or the handover itself failed) |
| `shipped` | passed and its change was merged or deployed |
| `passed` | passed otherwise; this includes a pull request or `PR.md` that waits for a person's review |

**When.** Right after the handover (only `needs-human`) and at the end of the run. `state.json`
records every attempt (`notified`: notifier id → events) before it is made, so there is at most one
attempt per run, event and notifier; a resumed run (`--resume`) that stops again the same way does
not send again. A fan-out candidate never notifies: its parent reports. A `--dry-run` sends nothing
unless `--notify` is given. A notifier can never fail a run or change its result: each attempt is
bounded by the notifier's `#:timeout` (1–60 seconds, default 10), a failure is recorded as a
`notify.failed` event with a reason from a fixed list, and the run goes on. There is no retry.

**The message.** The subject is `[al] <layer>: <event in words> — <title>`, with `needs a person`,
`failed`, `finished` or `shipped`, and the title of the run's pull request (at most 72 characters).
The body is plain text; a line appears only when it has a value:

```
Workflow: change
Run: 1a2b3c4d

The run stopped at step `test` and was handed to a person. Why: check test exited 2 after 2 attempt(s); log agents/1a2b3c4d/checks/test.2.log

Where to look:
- branch: chore/1a2b3c4d-add-a-pay-band
- pull request: https://forge.example.org/acme/webshop/pulls/7
- description: agents/1a2b3c4d/PR.md
- dashboard: http://127.0.0.1:8765/
```

The reason is the first line of the handover reason, the limit's reason or the failed step's
detail, with machine paths removed and at most 300 characters. Nothing else is in a message: no
prompt text beyond the title, no log tail, no environment, no path of the machine and no host name
(the SMTP greeting says `localhost`, the `Message-ID` uses the sender's domain, the date is UTC).
The dashboard line is the value of `AL_DASHBOARD_URL` as given, when it is set.

**No private data in the repository.** A layer file is committed, so it holds only the NAMES of
environment variables: `#:to-env`, `#:from-env` and `#:smtp-url-env` default to `AL_NOTIFY_TO`,
`AL_NOTIFY_FROM` and `AL_NOTIFY_SMTP_URL`, and the compiler rejects a value that looks like an
address, a URL or a host name (its error never repeats the value). The values come from, highest
precedence first:

1. the process environment (a non-empty value);
2. `local.env` in the layer directory: `KEY=VALUE` lines, ignored by git through the generated
   `.gitignore` of every placement; `al check` fails when git tracks it;
3. `$XDG_CONFIG_HOME/al/<layer name>.env` (default `~/.config/al/<layer name>.env`).

```
AL_NOTIFY_TO=ops@example.org
AL_NOTIFY_FROM=al@example.org
AL_NOTIFY_SMTP_URL=smtp+starttls://USER:PASSWORD@mail.example.org:587
```

Values read from the two files are never put into the process environment, so agents, checks and
containers do not inherit them, and they are never written to state, events, logs, PR text or
anything the dashboard reads. Events, `run.log` and `al notify-test` show the notifier's id, its
kind, the event and a reason from a fixed list (`variable AL_NOTIFY_TO is not set`, `timed out
after 10 s`, `login refused (SMTP 535)`, …); an exception's text is never printed.

**SMTP.** Three URL forms: `smtp://host[:port]` (plain, port 25), `smtp+starttls://[user:pass@]host[:port]`
(port 587) and `smtps://[user:pass@]host[:port]` (port 465); user and password are percent-decoded.
Plain `smtp://` is accepted only to a loopback host (`localhost`, `::1`, `127.x.x.x`, compared as
text), e.g. a mail bridge on the same machine; to any other host it is refused with the reason,
because the message and any password would travel unencrypted, unless the URL ends in
`?insecure=plaintext`. Certificates are verified with the system's defaults, except for a loopback
host (where plain would be accepted anyway), so a local bridge with a self-signed certificate works.
STARTTLS is never downgraded: a server that does not offer it is a failure.

**The `command` kind** is the generic way out (a chat webhook, a desktop notification, a pager). The
command runs with `sh -c` in the layer directory and gets the message on standard input, the
subject on the first line. Its environment has the variables of `local.env`, plus `AL_NOTIFY_EVENT`
and `AL_NOTIFY_SUBJECT`. Exit code 0 means accepted; its output is discarded; after `#:timeout` its
process group is killed.

**Checking the setup.** `al notify-test LAYER [--event E]` sends one clearly marked test message
through every declared notifier, whether or not it lists the event, and prints per notifier
`accepted` or `FAILED: <reason>`. No run is started and nothing is recorded.

## Run events

Besides `state.json` (the current picture, rewritten as it changes) and `run.log` (progress lines for
people, each prefixed with its UTC time), every run appends what happened, as it happens, to
`agents/<id>/events.jsonl`. One module owns the format (`kit/runtime/adw_modules/events.py`); the
places that print a progress line record the matching event in the same call, so the two cannot
drift. One JSON object per line, appended with a single `write` on an `O_APPEND` file; the next `seq`
is taken under a `flock` on the file, so two writers (the run and the stand-in's loop, or two
processes) never share one. A failed write never fails a run. Readers ignore types and fields they do not know, and a directory without the file
is an older run (the dashboard then infers timings from file times, as before).

Every event has `ts` (ISO-8601 UTC, milliseconds), `seq` (1, 2, … within the file; a resumed run
continues the count) and `type`. `owner` is the ADW step running at the time (a resolve agent belongs
to its check, a re-check and the reviewers to the review step).

| `type` | Fields |
|---|---|
| `run.start` | `adw`, `adw_id`, `klass`, `dry_run`, `resumed`, `pid`, `host`, `fanout_parent` (`{run, step, index}` or null), `overrides` (`{tier, models, max_cost, from}` or null), `limits` (`{run, step, day, run_from, limit_percent, on_limit, wait_minutes}` or null) |
| `run.end` | `status` (passed / handed-over / failed / waiting; `delivered` for the remote part of a run on another host), `exit` (0 for passed, handed-over and delivered, 75 for waiting) |
| `slot.wait` | `pool` (runs / agents / checks), `limit`, `who` |
| `slot.acquire` | `pool`, `slot`, `waited` (seconds), `who` |
| `slot.release` | `pool`, `slot`, `held` (seconds), `who` |
| `env.container.start` / `env.container.stop` | `name` |
| `env.build.start` | `image` (`al-<layer>:<hash>`), `recipe` (the hash) |
| `env.build.end` | `image`, `recipe`, `status` (passed / failed), `seconds`, `size` (bytes, or null), `error` (`exit N`, `timed out after …`, or null) |
| `env.preflight` | `env` (the podman option checked: the run's own, or one its checks run `#:in`), `status` (passed / failed), `seconds`, `checks` (`[{name, status (ok / miss / skip), detail}]`) |
| `step.start` | `step`, `kind`, `attempt` |
| `step.end` | `step`, `kind`, `status` (passed / rejected / failed / skipped / waiting / not-affected; `rejected` only for a review step whose final verdict failed, `not-affected` only for a check step whose cmd was not run), `detail`, `seconds` |
| `agent.start` | `step` (the agent's directory: `build`, `test-resolve-1`, `review-2-1`, `patch-1`, …), `owner`, `agent` (option id), `agent_kind` (claude-code / codex / custom / mock), `model`, `model_set` (= `tier`), `tier`, `tier_from` (override / request / step / command), `model_from` (agent / override), `effort`; emitted before the call waits for an `agents` slot (a `slot.wait` / `slot.acquire` with `who` = `adw <id> step <dir>` follows) |
| `agent.end` | as `agent.start`, plus `status` (passed / failed), `seconds`, and when the transcript says so `cost` (USD), `turns`, `duration_ms`; `billing` (subscription / api-key, when the transcript's init event says so; else empty); `error` on failure |
| `check.start` | `step` (check step, or `gate` / `deploy`), `owner`, `phase` (check / recheck / gate / deploy), `cmd` (codebase cmd name), `attempt`, `log` (layer-relative) |
| `check.end` | as `check.start`, plus `exit`, `seconds`; for a result that carries its depth (a layer with `(testing …)`) `depth` (smoke / targeted / full), `selected`, `total` (the full suite's size, or null) and `strategies` (`{strategy: count}`); when the output had a test report `skipped` (count), `skip_reasons` (`[[reason, count]]`, at most 5, most frequent first) and `skip_source` (`pytest`, `go`, `go (no -v)`, `cargo`, `mocha`, `junit:<file>`, joined with `+`); `in` (the environment option) for a check that runs `#:in` one. A gate re-run of a check's cmd carries the same fields |
| `check.decision` | a check that was not run, in place of a `check.start` / `check.end` pair: `step` (check step, or `gate`), `owner`, `phase` (check / recheck / gate), `cmd`, `outcome` (not-affected / reused), `why`, `paths` (the changed paths compared, at most 50), `paths_total`, `tree` (the worktree's tree hash), `from_step` and `from_phase` (where the reused pass ran; empty otherwise), `log` (the decision's text; for a reused check step the log of the run that passed) |
| `script.start` / `script.end` | `step`, `owner`, `cmd` (codebase cmd name), `log` (layer-relative); `script.end` adds `exit`, `seconds` |
| `script.event` | `step`, `owner` (the script step), `line` (the line number in the script's own `events.jsonl`), `foreign` (that line's JSON object, unchanged) |
| `budget.stop` | `step` (the agent call that crossed the limit, or the one refused), `limit` (run / step / day), `spent`, `of`, `reason` (`budget:<limit>: spent $X of $Y`) |
| `limit.status` | `step` (the agent call's directory), `owner`, `window` (the CLI's name: `five_hour`, `seven_day`, …), `status` (allowed / allowed_warning / rejected), `used` (0–1, or null when the CLI gave no share), `resets_at` (ISO-8601 UTC or null), `at` (time of the nearest transcript line that has one, or null), `billing`, `raw` (the CLI's `rate_limit_info`, unchanged); one per window and call, more only when a value changed during the call |
| `limit.stop` | `step`, `window`, `cause` (reached = the CLI refused or cut off the call / threshold), `status`, `used`, `percent` (the threshold, or null), `resets_at`, `reason` (`limit:<window>: …; resets …`) |
| `limit.wait` | `step` (the call that waits), `window`, `used`, `percent`, `resets_at`, `seconds` |
| `review.round` | `step`, `round`, `success`, `reviewers`, `issues` (`{blocker, tech_debt, skippable}` counts), `summary` |
| `batch.open` | a batch run's `open` step: `branch`, `stage`, `head`, `since` (the last green head or the merge base, or empty), `commits` (count), `delivered` (run ids) |
| `batch.bisect` | `culprits` (`{cmd: run id, "commit SHA (no run)" or empty}`) |
| `batch.result` | `id`, `branch`, `stage`, `head`, `status` (green / red), `depth`, `culprit`, `fix_run`, `delivered` |
| `gate.decision` | `action` (pr / merge / deploy / none), `ship`, `reasons` |
| `ship.result` | `action`, `shipped`, `target`, `pr_url`, `reasons` |
| `handoff` | `status` (opened / skipped / failed), `reason`, `stopped_at`, `target`, `pr_url` |
| `notify.sent` | `notifier` (the option id), `kind` (email / command), `event` (needs-human / failed / passed / shipped), `seconds`; never a recipient, a server or a variable's value |
| `notify.failed` | as `notify.sent`, plus `reason` (one of a fixed list, e.g. `variable AL_NOTIFY_TO is not set`, `timed out after 10 s`, `login refused (SMTP 535)`, `exit 3`) |
| `fanout.candidate` | `step`, `id`, `index`, `status` (running / passed / failed / stopped), and when finished `exit`, `seconds`, `reason` |
| `delivery.frozen` | `stage`, `branch`, `reason` (the period or the open promotion, and how to resume) |
| `promotion.opened` | `id`, `from`, `to`, `from_branch`, `to_branch`, `pinned`, `changes` (ids), `dry_run` |
| `promotion.decision` | `by`, `kind` (human / agent / forge), `scope`, `decision`, `then`, `comment`, `source`; also appended by `al decide` while the run waits |
| `promotion.excluded` | `change`, `by`, `comment`, `then`, `revert` (the revert commit), `patch_run` |
| `promotion.waiting` / `promotion.approved` | `why`, `needed`, `have`, `undecided` / `approved_by`, `refused` |
| `promotion.merged` | `method`, `result`, `to_branch`, `staged` |
| `promotion.tagged` / `promotion.deployed` | `tag`, `commit` / `environment`, `exit` |
| `promotion.back-merge` | `stage`, `branch`, `conflict`, `resolved`, `published` |
| `promotion.closed` | `id`, `result`, `tag`, `changes` (id → status) |
| `integrate.queued` | `behind` (the integration run that holds the layer's lock) |
| `integrate.lock` / `integrate.unlock` | `holder` (this run), `waited` / `held` (seconds): one integration of a layer at a time |
| `integrate.prepared` | `run` (the run it integrates), `target`, `target_head`, `base`, `conflicts` (files), `generated` (files) |
| `integrate.resolved` | `resolved`, `regenerated`, `undecidable` (files, from the agent's report); after an `address` step: `addressed` (blockers), `mode` |
| `integrate.result` | `run`, `result` (landed / staged / pull-request / failed / waiting), `why`, `commit`, `target` |
| `placement.decision` | `host` (where the run executes; `home` is the machine that holds the layer), `reasons`, `skipped` (`[{host, reason}]`: no answer, an older layer, no login for the agent, reserved for other classes, the layer not at the git source). Only in a layer with `(placement …)` |
| `remote.start` | `host`, `adw`, `adw_id`, `base_branch`, `restart` (true when a lost run starts again). Written by the host as the first event of the run's remote part |
| `remote.push` | `sha`, `branch` (always the run's own), `step`, `kind` (checkpoint: a step committed / delivered: in place of the ship step / stopped: the work of a failed run) |
| `remote.sync` | `host`, `status` (what the run's remote part ended as: delivered / failed). One per change of that status, not one per pull |
| `remote.lost` | `host`, `reason`, `pushed` (the last pushed commit, or empty) |
| `remote.takeover` | `host`, `sha`, `branch`: the delivered commit was fetched from the git source and the home worktree stands on it |
| `assign.attempt` | on the run that was given to an agent: `n`, `by` (cli / dashboard / decider), `kind`, `run` (the run it started or resumed), `reason` (the stop key), `note`, `override` |
| `assign.outcome` | `n`, `kind`, `run`, `outcome` (landed / passed / stopped), `after` (the stop key afterwards), `why`; once per attempt |
| `assign.approval` | `item`, `kind`, `text`, `by`, `via`: a person approved exactly this "needs a person" item |
| `step.discipline` | after a `run` step that may not commit: `step`, `ok`, `head_before`, `head_after`, `branch_before`, `branch_after`, `refs` (`created`, `moved`, `deleted`), `main_checkout` (new status lines), `layer_entries` (new top-level names), `violations` (kinds: `committed`, `head-moved`, `ref-created`, `ref-moved`, `ref-deleted`, `main-checkout-changed`, `layer-entry`), `texts`, `restored`, `not_restored`; after a step that may commit (the `commit` template or `#:may-commit #t`): `step`, `ok`, `left_out_restored` (the uncommitted paths under the layer directory or a protected path it restored). The same facts are in the step's record in `state.json` (`discipline`, or `left_out` on a commit step) |
| `standin.decision` | a decision of the stand-in that did not act, or a person's answer: `entry` (its id in `decisions.jsonl`), `who` (stand-in / person), `via` (run / loop / asked / cli / mail / webhook / dashboard), `kind`, `action`, `answer`, `status` (suggested / refused / escalated / failed / skipped), `reason`, `summary` |
| `standin.action` | an action that was taken: the same fields with `status` acted, `auto` (true when the stand-in acted alone) and `outcome` (what happened next) |
| `decision.skipped` | a party left the run's current stop alone: `who` (`run`: its own decide step lost the stop to the stand-in's loop / `stand-in`: the loop found the run's process alive or the stop claimed), `reason`, `stop_seq` (the `seq` of the stop's `handoff` event); once per stop and party |

Two more things make a run observable from outside. A check's log file grows while the check runs
(its output goes straight to the file; when it ends the content is normalised exactly as before).
And `state.json` holds `pid`, `host` and a `heartbeat` refreshed every few seconds by a small daemon
thread of the run, so a run whose heartbeat is old and whose process is gone has died (killed, out
of memory, machine rebooted) rather than still working; the dashboard shows it as **died**, and `al
check` lists such runs. A fan-out candidate's state names its parent (`fanout_parent`), and each step
record names the agent (`agent`, `agent_kind`, `model`) that ran it.

One process owns a run while it lives. The runner holds an exclusive `flock` on
`agents/<id>/run.lock` (which names its `pid` and `host`) from the moment its state is known until
it has written its summary, and releases it before it starts the resume its decider chose. A second
`--resume` of a live run exits 3 (`refused: run ID is held by process PID on HOST`) before anything is
saved or an environment is built, so only the owner ever replaces the run's container. Every party
that resumes checks the lock first: the dispatch's `--resume` (the dashboard), `al answer`, the
stand-in's loop and its apply, give to an agent, the decider's spawn and the dashboard's start again
and resume proposals. The lock goes with its process, so a dead owner's run is taken over. While the
run's process lives, even in its own decide step after it marked itself failed, the stand-in's loop
leaves it alone. The run's decide step and the loop also share one claim per stop in
`agents/<id>/decide.claim` (`seq` of the stop's `handoff` event, `by`, `pid`, `at`, `skipped`): whoever
claims first decides that stop, and the other records `decision.skipped`.

A `script` step can declare exit codes that mean "waiting for something outside" (`#:waiting-exit
(75)`): the step and the run end `waiting` (exit 75), the worktree is kept, no hand-off is written
and no failure is notified, and `--resume` runs the step again with `ADW_RESUME=1`. A script that
keeps its own run journal (`#:journal ".work/adw/{adw_id}"`, relative to the worktree) is followed
while it runs: a daemon thread (`adw_modules/journal.py`) reads the folder's `events.jsonl` from the
last offset, only complete lines, and emits each JSON object as a `script.event` (anything else is
counted as malformed); after the script it sweeps once more. Each sub-folder's `prompt.md` and
`output*` files (regular files only, no symlinks, at most 1 MB) are copied to
`agents/<id>/scripts/<step>/journal/<inner>/` and recorded in `state.scripts[<step>].documents`. A
folder whose real path leaves the worktree is refused and nothing is read; a missing folder is not
an error. The mirrored lines are data: the dashboard shows them as text.
