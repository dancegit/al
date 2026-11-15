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
| Model sets base/heavy; KPIs | `#:model-set` on commands/steps; `track-kpis` template |
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
html, file), `voice` (Kokoro TTS + faster-whisper), `layer` placement.

Defaults when a family is omitted: cli prompt, manual trigger, worktree, human review, Claude Code,
no forge, local storage. The smallest layer is 12 lines (`racket/al/examples/minimal.rkt`).

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
tools/al           init | scout | compile | gen | check
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
descriptor is true.

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

## Proof on the pull request

With a Forgejo forge the run attaches its image artefacts, and the keyframes of any video, to a "Proof"
comment on the pull request (at most eight pictures; a video is linked). A human reviewer therefore sees
what the checks captured without leaving the forge, and a zero-touch merge leaves the same evidence on
the record. GitHub has no attachment API through `gh`; there the route is an S3-compatible store with a
public base URL. The web app demo in `tests/webdemo` exercises both review styles end to end.
