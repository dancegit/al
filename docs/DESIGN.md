# al — from `LLM.txt` to a codebase-specific agentic layer

## The idea in one paragraph

Tactical Agentic Coding says the agentic layer is "a ring around your codebase": templates, plans,
HOPs and ADWs, kept in a separate directory, specific to *this* codebase ("an effective ADW is not
generic"). Building that ring by hand for every repository does not scale, and letting an LLM
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
| Always add feedback loops (request → validate → resolve) | `(check NAME #:cmd test #:resolve resolve-test #:retries 3)` |
| Review = "is what we built what we asked for? prove it", severities blocker / tech_debt / skippable | `review` command returns that JSON; artefacts (logs, junit, images, video keyframes) are collected, stored and handed to the reviewer |
| ZTE: SDLC + ship, "only if all phases pass", start with chores | `(review auto …)`: agent verdict AND deterministic gates (required checks, allowed classes, max diff, protected paths) → merge/deploy, else fall back to a human PR |
| Isolation via worktrees + ports; layer separate from app | `environment worktree` creates `trees/<adw-id>` under the layer; the app's working tree is never written |
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
| **E**nvironment | `host` (read-only) · `worktree` · `nix-devshell` (flake) · `home-manager` (module) · `podman` (rootless container) — stacked per ADW |
| **R**eview | `human` (PR) · `agent` (verdict + proof, human merges) · `auto` (zero-touch: verdict + gates → merge/deploy) |

Supporting families: `agent` (claude-code, codex, custom, mock), `forge` (none, github, forgejo),
`storage` (local, s3-compatible), `artefact` (log, junit, coverage, image, video, audio, trace,
html, file), `voice` (Kokoro TTS + faster-whisper), `notify` (none, email, command), `layer` placement.

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
      gate: all checks green · required checks re-run green · no blocker · class allowed
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

## Supporting the LLM: our own commands

`meta/commands/` holds the prompts that drive an LLM through the pipeline: `scout-codebase`
(complete the deterministic draft; every command must cite evidence), `author-layer`
(LLM.txt + reference → `agentic.rkt`, compile, repair), `repair-layer`, `review-layer`.
Inside each generated layer the meta HOPs (`meta-command`, `meta-adw`, `meta-expert`) let agents
extend the layer through the same compiler-checked path — "fix the system, not the output".

Fabric patterns are a library of methods: `#:fabric (review_code)` on a command vendors the pattern
into the layer and the command applies it, while the command's own Report contract stays in charge
of the output format.

## Layout of this repository

```
racket/al/         #lang al: meta.rkt (meta-DSL), vocab.rkt, compiler.rkt, schema.rkt, reference.rkt, tests/
schemas/           agentic-layer-v1.schema.json      (generated)
docs/              AL-DSL.llms.txt (generated), DESIGN.md
tools/al           init | scout | adopt | compile | gen | check | explain | eject | improve | dashboard | verify
kit/templates/     command, HOP and expert templates   kit/schemas/  JSON output contracts
kit/runtime/       adw_modules (generic IR interpreter), triggers, voice, dispatch
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
  branches that hold nothing unshipped are deleted with their worktree.

- `(fan-out NAME #:adw CHILD #:count N #:pick best|first|all)` — race one task across N independent
  runs of a child ADW. Each candidate is a full run with its own worktree, container, port lease and
  slots; the winner (fewest resolve attempts, then smallest diff) is merged into the parent run, which
  goes on to check, review and ship it. Losers' branches are removed and their ids recorded.

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
  of `al sandbox doctor` in the run's own container (started exactly as its steps will use it): the
  first executable of every codebase cmd the workflow runs (check and script steps, and the review's
  `#:require-checks` when it ships), the agent CLI and a credential when the agent runs inside (variable names and
  file paths only; values are never printed), git in the worktree, an init process, every cache
  volume writable, enough free disk, and — when the workflow collects image, video or trace artefacts
  — a headless browser, and the sidecars' ready commands. Any miss fails the run at once, before any
  step, with a table naming each missing thing and the declaration to change (`env.preflight`).
  A podman option's `preflight` list (default `["services", "browser"]`) turns those two optional
  checks off for that environment. A workflow that runs nothing in the container (no codebase cmd, and its
  agent outside it, or a dry run's stand-in agent) skips the preflight and builds nothing.

## Isolation per run

Every run is isolated on five axes, and none of them depends on runs being polite to each other:

| Axis | Mechanism |
|---|---|
| Files | Own git worktree; with podman only that worktree is mounted. Other runs' worktrees, state, artefacts and object quarantines are not mounted at all, and neither is the application's working tree. |
| Git | The app's `.git` is read-only in the container; new objects go to a per-run quarantine; only the host moves refs, after freezing the container and verifying its git metadata. |
| Network and ports | Each run has its own network namespace, so the app inside binds its usual ports and runs cannot collide. `#:publish (8000)` exposes a container port on `127.0.0.1` at a host port leased to that run (`ADW_PUBLISHED_8000`); nothing else is reachable from the host. Leases are tied to a live process. |
| Services | `(service container #:id db #:image … #:ready-cmd …)` listed in `#:services` gives each run its own sidecar instances in a per-run pod (shared localhost, no mounts). Parallel runs never share a database. |
| Privileges and resources | `--cap-drop=ALL`, `no-new-privileges`, a pids limit, memory and CPU limits, optional read-only root, per-container HOME and /tmp. Layer-wide slot pools cap runs, agent calls and checks. |

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
drift. One JSON object per line, appended with a single `write` on an `O_APPEND` file; a failed write
never fails a run. Readers ignore types and fields they do not know, and a directory without the file
is an older run (the dashboard then infers timings from file times, as before).

Every event has `ts` (ISO-8601 UTC, milliseconds), `seq` (1, 2, … within the file; a resumed run
continues the count) and `type`. `owner` is the ADW step running at the time (a resolve agent belongs
to its check, a re-check and the reviewers to the review step).

| `type` | Fields |
|---|---|
| `run.start` | `adw`, `adw_id`, `klass`, `dry_run`, `resumed`, `pid`, `host`, `fanout_parent` (`{run, step, index}` or null), `overrides` (`{tier, models, max_cost, from}` or null), `limits` (`{run, step, day, run_from, limit_percent, on_limit, wait_minutes}` or null) |
| `run.end` | `status` (passed / failed / waiting), `exit` (75 for waiting) |
| `slot.wait` | `pool` (runs / agents / checks), `limit`, `who` |
| `slot.acquire` | `pool`, `slot`, `waited` (seconds), `who` |
| `slot.release` | `pool`, `slot`, `held` (seconds), `who` |
| `env.container.start` / `env.container.stop` | `name` |
| `env.build.start` | `image` (`al-<layer>:<hash>`), `recipe` (the hash) |
| `env.build.end` | `image`, `recipe`, `status` (passed / failed), `seconds`, `size` (bytes, or null), `error` (`exit N`, `timed out after …`, or null) |
| `env.preflight` | `status` (passed / failed), `seconds`, `checks` (`[{name, status (ok / miss / skip), detail}]`) |
| `step.start` | `step`, `kind`, `attempt` |
| `step.end` | `step`, `kind`, `status` (passed / failed / skipped / waiting), `detail`, `seconds` |
| `agent.start` | `step` (the agent's directory: `build`, `test-resolve-1`, `review-2-1`, `patch-1`, …), `owner`, `agent` (option id), `agent_kind` (claude-code / codex / custom / mock), `model`, `model_set` (= `tier`), `tier`, `tier_from` (override / request / step / command), `model_from` (agent / override), `effort`; emitted before the call waits for an `agents` slot (a `slot.wait` / `slot.acquire` with `who` = `adw <id> step <dir>` follows) |
| `agent.end` | as `agent.start`, plus `status` (passed / failed), `seconds`, and when the transcript says so `cost` (USD), `turns`, `duration_ms`; `billing` (subscription / api-key, when the transcript's init event says so; else empty); `error` on failure |
| `check.start` | `step` (check step, or `gate` / `deploy`), `owner`, `phase` (check / recheck / gate / deploy), `cmd` (codebase cmd name), `attempt`, `log` (layer-relative) |
| `check.end` | as `check.start`, plus `exit`, `seconds` |
| `script.start` / `script.end` | `step`, `owner`, `cmd` (codebase cmd name), `log` (layer-relative); `script.end` adds `exit`, `seconds` |
| `script.event` | `step`, `owner` (the script step), `line` (the line number in the script's own `events.jsonl`), `foreign` (that line's JSON object, unchanged) |
| `budget.stop` | `step` (the agent call that crossed the limit, or the one refused), `limit` (run / step / day), `spent`, `of`, `reason` (`budget:<limit>: spent $X of $Y`) |
| `limit.status` | `step` (the agent call's directory), `owner`, `window` (the CLI's name: `five_hour`, `seven_day`, …), `status` (allowed / allowed_warning / rejected), `used` (0–1, or null when the CLI gave no share), `resets_at` (ISO-8601 UTC or null), `at` (time of the nearest transcript line that has one, or null), `billing`, `raw` (the CLI's `rate_limit_info`, unchanged); one per window and call, more only when a value changed during the call |
| `limit.stop` | `step`, `window`, `cause` (reached = the CLI refused or cut off the call / threshold), `status`, `used`, `percent` (the threshold, or null), `resets_at`, `reason` (`limit:<window>: …; resets …`) |
| `limit.wait` | `step` (the call that waits), `window`, `used`, `percent`, `resets_at`, `seconds` |
| `review.round` | `step`, `round`, `success`, `reviewers`, `issues` (`{blocker, tech_debt, skippable}` counts), `summary` |
| `gate.decision` | `action` (pr / merge / deploy / none), `ship`, `reasons` |
| `ship.result` | `action`, `shipped`, `target`, `pr_url`, `reasons` |
| `handoff` | `status` (opened / skipped / failed), `reason`, `stopped_at`, `target`, `pr_url` |
| `notify.sent` | `notifier` (the option id), `kind` (email / command), `event` (needs-human / failed / passed / shipped), `seconds`; never a recipient, a server or a variable's value |
| `notify.failed` | as `notify.sent`, plus `reason` (one of a fixed list, e.g. `variable AL_NOTIFY_TO is not set`, `timed out after 10 s`, `login refused (SMTP 535)`, `exit 3`) |
| `fanout.candidate` | `step`, `id`, `index`, `status` (running / passed / failed / stopped), and when finished `exit`, `seconds`, `reason` |

Two more things make a run observable from outside. A check's log file grows while the check runs
(its output goes straight to the file; when it ends the content is normalised exactly as before).
And `state.json` holds `pid`, `host` and a `heartbeat` refreshed every few seconds by a small daemon
thread of the run, so a run whose heartbeat is old and whose process is gone has died (killed, out
of memory, machine rebooted) rather than still working; the dashboard shows it as **died**, and `al
check` lists such runs. A fan-out candidate's state names its parent (`fanout_parent`), and each step
record names the agent (`agent`, `agent_kind`, `model`) that ran it.

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
