# al work dashboard

A live view of what the agents of one or more layers are doing (with two guarded writes: which
model each tier uses, and starting work: a workflow or a single command): which runs are in
flight and what each agent is doing right now, the workflow of every run as a graph that colours
in as steps finish, where the time went, what it cost, and every agent's transcript formatted for
people. One Python file (standard library only) serves one page (HTML, CSS and vanilla JS, no
CDN, no build step) and pushes updates over Server-Sent Events. Its **Repos** view also lists the git
repositories beside the layers with their al state and runs allow-listed `al` commands on one of them,
each confirmed first when it writes (see Safety, "Actions").

![Three runs started while the page is open; the third waits for a run slot](../../docs/demo/dashboard/live.gif)

[live.mp4](../../docs/demo/dashboard/live.mp4) (52 s): three runs of a demo layer, two at a time;
the Now view fills, the third run waits for a slot, the graph colours in, a transcript streams,
a test fails and its resolve agent fixes it. More: [run detail](../../docs/demo/dashboard/run-detail.png) ·
[transcript, filtered](../../docs/demo/dashboard/step-transcript.png) ·
[timeline and costs](../../docs/demo/dashboard/timeline-costs.png) ·
[artefacts](../../docs/demo/dashboard/artefacts.png) · [a layer's workflows](../../docs/demo/dashboard/layers.png).
The still images are real Claude Opus runs (the flask layer and the healthcare staffing demo).

## Run it

Every generated layer has its own (the default `(dashboard local)` in `agentic.rkt`; see
"Declared in the layer" below):

```bash
uv run adws/dashboard.py                                         # in a layer: this layer, its #:also layers
al dashboard ~/code                                              # any layers, several codebases at once
```

Or straight from the kit:

```bash
uv run kit/dashboard/dashboard.py --scan .                       # every layer below this directory
uv run kit/dashboard/dashboard.py --layer ~/code/myapp.al --open # one layer, open a browser
kit/dashboard/run-in-podman.sh ~/code/myapp.al                    # in its own container (below)
```

| Flag | Meaning |
|---|---|
| `--layer DIR` | watch this layer (a directory with `agentic-layer.json`); repeatable |
| `--scan DIR` | find layers below DIR, in-repo ones included (depth `--scan-depth`, default 4; `.git`, `node_modules`, `trees`, `testbeds`, … are skipped); rescanned every 10 s, so new layers appear |
| `--port N` | default 8765 |
| `--bind ADDR` | default 127.0.0.1; a non-loopback address is refused unless `--token` is given |
| `--token T` | every request needs it (`?token=T` once, then an HttpOnly cookie; or `Authorization: Bearer T`); also read from `AL_DASHBOARD_TOKEN` |
| `--open` | open the page in a browser |
| `--poll S` | seconds between file polls (default 0.75) |
| `--no-podman` | never call podman |
| `--podman-socket PATH` | opt-in: read container status from a podman API socket (for the container mode) |
| `--in-container` | the host's processes and locks are not visible: infer slot use from run state; starting work is off; no workspace and no actions |
| `--read-only` | refuse every write: no model settings, no starting work, no actions |
| `--no-start` | never start work from the page (the Models panel still writes) |
| `--allow-start-in-container` | with `--in-container`: allow starting work, only for an operator-built image that has `uv`, the application checkout and a writable layer (the image of `run-in-podman.sh` has none of them) |
| `--workspace DIR` | a folder of git repositories side by side: list them with their al state and act on them (repeatable). Default: the folder holding each `--layer`'s repository (its `codebase.root`'s parent); `--workspace` alone is enough to start |
| `--workspace-depth N` | how deep below a workspace folder repositories are looked for (default 1) |
| `--no-workspace` | list no repositories, only layers |
| `--no-actions` | list the repositories but run no action |
| `--jobs-dir DIR` | where actions are recorded (default `$XDG_STATE_HOME/al/dashboard-jobs`, else `~/.local/state/al/dashboard-jobs`) |
| `--al PATH` | the al tool the actions run (default: `tools/al` of the checkout this kit belongs to, else `al` on `PATH`) |
| `--author-agent claude-code\|mock`, `--author-mock FILE` | the agent `al author` uses in the create and adopt actions (`--author-mock` for tests: `--agent mock --mock FILE`) |

## What you see

| View | What it shows |
|---|---|
| **Now** (`1`) | every run in flight: layer, workflow, request, current step, the agent's latest action in one line ("editing tests/test_basic.py", "running pytest", "thinking"), elapsed time, cost so far, whether it waits for a slot, its container (name, state, CPU and memory when podman is available); a **Subscription usage** panel per layer: what the agent CLI last reported about the subscription's usage windows (5-hour, weekly, or whatever name the CLI uses): share used as a bar, status, reset time, when and by which run it was reported, and the layer's threshold when it declares one. It has three states: the windows, "API key: not applicable" (the last call ran on an API key), "no data yet"; where the CLI gave no share it says so. It is not a live meter: it changes when an agent call ends. Then slot pools per layer (runs / agents / checks, held / total, from the lock files) and leased ports; recently finished runs. Dollars of runs whose calls ran on a subscription are marked "API-equivalent" |
| **Runs** (`2`) | history with filters (layer, workflow, status, class, dates, text), sortable by time, cost and duration; count, pass rate, total and median cost and time |
| **Run** | the workflow as a graph from the layer's IR (agent / check / review / ship / fan-out nodes; `when` branches stacked in one column; resolve loops and the review → patch → re-check loop as back-edges; fan-out candidates as lanes linked to their own runs), coloured live with duration and cost per node; a timeline of every step, check attempt, resolve agent, reviewer, patch round and gate re-run, with agent / check / slot-wait totals, setup bars at the top for a sandbox image build (`building sandbox image <recipe> (<size>)`) and the preflight (`preflight: all checks passed` or `preflight: missing <checks>`) from `env.build.*` / `env.preflight` events (while a build runs the run's current action reads "building the sandbox image"), and marks for what the CLI reported about subscription usage, a limit stop and a wait for a reset (`limit.*` events); cost and tokens per agent ("API-equivalent" when the calls ran on a subscription); the outcome: gate decision and reasons, ship target or PR, **why the run stopped** (a run a subscription limit stopped carries a "limit stop" mark and a "Subscription limit" block with the window, the share used and the reset time) and what the agent asks a human to do, review verdicts per round and reviewer with issues by severity |
| **Step** | the exact prompt (sections collapsible), the transcript as one row per event: what the agent said, tool calls summarised (file read / edit with a diff / write, shell command, search pattern), results collapsed with size, errors marked, thinking as a marker, the final cost / turns / time. Filters (kind, errors only, tool), search with highlight, follow mode, raw JSON of any event, download of the raw `.jsonl`. Checks: every attempt's log with ANSI colours, its exit code and the resolve agent that followed |
| **Documents** | what the run has written for people to read, as soon as each file exists: the plan, the `.md` / `.txt` / `.json` / `.log` outputs its steps recorded (a review's `patch-<n>` plans among them), review patch plans, `PR.md` (the handoff or PR text) and other text files in `agents/<id>/`, each reviewer's verdict (`review-*/output.txt`) and text artefacts; with the step that produced each, its size and time. **View** renders Markdown in the page (with a raw toggle), pretty-prints JSON and shows text as text; **Download** saves the exact file, **Download all** a zip of them all. A step's panel links the documents it produced (the plan on `plan-feature`). A new document appears live |
| **Artefacts** | screenshots as thumbnails (click for full size), videos playable in the page with their keyframes, logs with their tail, all with the step that collected them |
| **Repos** (`4`) | the workspace: every git repository of the folder(s), searchable, with its state badge (`layer`: an `agentic.rkt` in it or in a sibling `NAME.al`; `setup`: rule files, prompts, workflow scripts or a hand-built suite, the same detection as `al scout`; `none`), branch, a mark for uncommitted changes and its last activity; **refresh** rescans. A selected repository (`#/repo/<key>`) shows al scout's "Existing agent setup" summary, its layer with a link to its workflows and runs, its jobs, and the **al** panel: one button per action with the exact command line(s) it runs, disabled with the reason when it does not apply (`needs the al tool; looked for …`, `--read-only`, `--no-actions`, the state), and a tools table: what `al doctor` sees on this machine (racket, raco, uv, git, the author agent's CLI: where each was found, or the sentence saying where al looked, and its version; kept 5 minutes). A writing action first shows what it runs and writes (for adopt the dry run's diff, line by line) and runs on **Confirm** |
| **Job** | one action (`#/job/<id>`), in the order a person reads it: the header (repository, action, status, exit code, duration, why it failed, `needs a person: N` when the author left items out, **Download full output**); once it ended, the **outcome** card: for a passed job what was created (the layer with the files added, changed and removed) or adopted, what is `UNVERIFIED` and the next action (the last `next: …` line the steps printed), for a failed or interrupted job the failing step, the reason in one sentence (a generic "exited N" is replaced by the step's last error), up to 12 last relevant lines, **Retry from STEP** (asks first; the steps from the first that did not pass run again in the same job, a retried `author` with `--accept-existing`) and **Download log**; the **steps row**, one chip per step with status, exit code and time, a failed step's reason, and under the running one what it is doing right now ("reading the CI files", "compiling, attempt 2", "writing agentic.rkt", or a plain step's last line); the **activity** of the selected step (the running one, else the failed one, else the agent step; a click picks another and the address keeps it as `?step=`): an agent step's journal with its messages as Markdown, one line per tool call (tool, target, result size, duration), errors in red, the final report and the totals, hooks, start-up, rate limits and other bookkeeping only with **show everything**; a plain step's own output as text (ANSI stripped); both the latest rows with **show earlier** and a search box, and the step's log (an agent step's **raw events**) and journal to download; the author's **Decisions** and **Left out and why** cards; the layer's files it added, changed or removed, and whether the repository changed; its documents (`LLM.txt` of scout, `preview.diff.txt` of an adopt preview, `verify.json`, `EXPLAIN.md`); and, collapsed, **Full output** (every step as it ran, ANSI colours; an agent step condensed), opened by itself for a job from before the `steps/` files |
| **Layers** (`3`) | a **Start work** panel (below), then each layer's workflows as static graphs with input / trigger / environment / review, runs, pass rate, average time and cost per workflow, recent runs, and the layer's plain-language explanation when an `EXPLAIN.md` exists |

The **Start work** panel (on the Layers view; a layer chooser when several are watched) takes a
request, any workflow of the layer (adopted ones included) or a single command or hop (one recorded
step in the layer's normal environment that never ships), picked from a list or typed as `/NAME …`
with completion (arrow keys, Enter, Esc; `/command:NAME` when a workflow has the same name). It says
in one line what the choice does and whether anything ships without a person, takes an optional
class, tier and model ids, and, when the layer has a forge, can log the request as an issue ("start
now", or "let the trigger start it" for a workflow a cron or webhook trigger starts from issues). A
workflow that merges or deploys without a person, and the same request again within five minutes,
open a confirmation dialog. Then the page opens the new run. When starting is off, the panel says why
(`--read-only`, `--no-start`, the container, or a layer generated before this feature: "run `al gen`").

A run whose `script` step exited with a code the step declares as waiting (`#:waiting-exit (75)`)
ended **waiting**: it is neither failed nor passed, its worktree is kept, and nobody is notified.
The run list, its status filter, the graph node and the timeline show it as `waiting` (⏸), and the
run's header shows the step it waits at and the command that resumes it
(`uv run adws/adw_<workflow>.py --resume --adw-id <id>`).

A review whose final verdict failed is shown as **rejected** (⊗, amber), with each round's verdicts
under the review step. Its run went on to the ship step, where the gate handed the work to a person,
so the run is **handed-over** (↪, amber), with the reasons (`review-rejected`, the gate's fallback
reasons) in the run's header. A run whose gate fell back is handed-over too. Records written before
these statuses existed (review `passed` with a failed verdict, run `passed`) are read the same way
when the files are read; the files are never rewritten. Pass rates count handed-over runs as
finished, not passed.

A `script` step that follows the script's own run journal (`#:journal`) shows that journal's lines
(mirrored as `script.event` events) as **inner steps** under the script step: time, kind, subject,
and the tokens and cost when the line reports them (`usage.input_tokens` / `output_tokens` / cache
fields or `tokens.in` / `out`, `cost_usd` / `cost`), with the step's totals. A `*.start` … `*.end`
pair with the same subject is one bar on the timeline, under the script step; any other line is a
point. Each inner step's `prompt.md` and `output*` files, copied into the run, are documents of
kind `inner` (named `<inner>/<file>`) on the Documents tab and in the step panel.

Every view is a URL (`#/run/flask/11bf99ac/step/build?q=static`), so a link can go into a PR or a
chat. Keys: `j`/`k` move through runs, nodes or transcript events, `Enter` opens, `/` searches,
`f` follows, `Esc` goes back, `?` lists them.

## What it reads, and how

Everything comes from files the runtime already writes. The dashboard itself writes
agents/settings/models.json (the Models panel) and, for the actions, its jobs directory; starting
work is done by the layer's own `adws/dispatch.py --start`, which writes the run's files; the
actions run `al`, which writes what that command writes. `--read-only` turns all of it off, and
`--no-start` and `--no-actions` each turn off their own.

| File | Used for |
|---|---|
| `agentic-layer.json` | the workflows (graphs; a `script` step is its own node type), slot limits, spending limits (`budget`: shown on the Now view with what today's runs cost, and against each run's cost), review options, declared commands |
| `agents/<id>/events.jsonl` | when present (runs of this runtime version and later): exact start and end of every agent call (with the agent, its kind and model), check attempt (exit code, re-check or not), gate and deploy command, slot wait and the container; the check running now; subscription usage reports, limit stops and waits (`limit.status`, `limit.stop`, `limit.wait`). See "Run events" in `docs/DESIGN.md`. Older runs have no such file and are read as below, from file times |
| `agents/<id>/state.json` | status, class, request, step records (status, start, end, detail, the agent, model and model tier used), every agent call's tier and why it was chosen (`agent_calls`), cost and spending limits (`cost`, `budget`: a run a limit stopped carries a distinct "budget stop" mark; `cost.billing` for "API-equivalent"), what the run recorded about subscription usage (`usage`: billing mode, the limit that stopped it, waits), script steps (`scripts`), checks, review rounds, ship and gate, fan-out candidates and a candidate's parent (`fanout_parent`), artefacts. Liveness: `pid`, `host` and `heartbeat` (refreshed every ~5 s). A run whose state says running but whose heartbeat is older than 20 s while its process is gone is shown as **died**, with when it was last seen (other host, or inside the container where pids are not visible: after 5 min without a heartbeat). Older files without newer keys are fine. A run without `state.json` is shown as `unknown` from its `run.log` |
| `agents/subscription-usage.json` | the Now view's "Subscription usage" panel: the billing mode and the latest report per usage window, written by the runtime after each agent call. Read-only, polled like the run files; a change is pushed to the page. Missing = "no data yet"; anything unreadable or of the wrong type is ignored, and its content is shown as text |
| `agents/settings/models.json` | the Now view's "Models" panel: the layer's local model setting (`{"models": {TIER: MODEL_ID}, "version": 1}`, never committed), the same file `al models LAYER --set TIER=ID` writes. With the IR's agent tiers it gives each tier's model and its source (`local setting`, `layer`, `built-in default`). Polled like the usage file; a change is pushed to the page. An unreadable file is shown as an error and not used. Also the one file the dashboard writes (see Safety) |
| `agents/<id>/run.log` | without events: container name and removal, slot waits (`[slots] … waiting for a free runs slot`), gate re-run exit codes. Newer runtimes start each line with its UTC time, which is stripped before matching |
| `agents/<id>/<step>/{prompt.md,raw.jsonl,output.txt,stderr.log}` | one directory per agent call (the transcript's init event also says whether the call ran on a subscription or an API key, so older runs are labelled too). `<check>-resolve-<n>` belongs to that check, `review-<n>`, `review-<round>-<n>`, `patch-<n>`, `implement-<n>` to the review step. Without events: start = `prompt.md` mtime, end = `output.txt` mtime |
| `agents/<id>/scripts/<name>.<n>.log` | the output of a `script` step, shown when its node is opened |
| `agents/<id>/checks/<name>.<attempt>.log`, `gate-<name>.log` | ANSI logs, followed live while the check runs (newer runtimes write a check's output to its log as it comes). Without events: attempts end at the log's mtime and start where whatever ran before them ended, since the runtime is sequential; a log only appears when its command finished |
| `agents/<id>/PR.md` | the "Why this stopped" and "What the agent asks" sections of a handoff; also a document of the run |
| `state.json` `plan`, `outputs`, `review.rounds[].patch.patch_plan`, `artefacts` | the run's **Documents**: recorded paths (absolute on the host or in a container, or layer-relative) mapped to the layer's `specs/`, `agents/<id>/`, `artefacts/<id>/`; with `PR.md`, other text files in `agents/<id>/` and `review-*/output.txt` |
| `agents/.slots/<pool>.<n>.lock`, `agents/.ports.json` | which slots are held (from `/proc/locks`, read-only) and by which run; leased ports and whether their owner is alive |
| `artefacts/<id>/manifest.json` | artefacts when `state.json` has none |
| `commands/<name>.md`, `hops/<name>.md` | the `argument-hint` of their front matter, shown next to `/name` in the Start work panel (regular files inside those directories only) |
| `adws/dispatch.py`, `adws/adw_modules/launch.py` | starting work: their presence enables the Start work panel; `dispatch.py --start --json` is run (see Safety) |
| `agents/starts.json` | written by the layer's launcher, not read by the page: the request keys of the last two days (`{key: {run_id, target, prompt_sha, at, issue}}`) behind the double-start guard; `agents/.starts.lock` serialises starts |
| `agents/<id>/request.md`, `agents/<id>/launch.log` | a started run's prompt (mode 0600) and the output of its detached process until the run's own `run.log` takes over; both are documents of the run |
| `state.json` `definition` | a single-command run's one-step workflow (`command:<name>`), which the compiled layer does not declare: the run's graph is drawn from it |
| workspace repositories | the Repos view, read-only: directories below each workspace folder (depth `--workspace-depth`; symlinked entries, hidden and pruned directories and a sibling `NAME.al` skipped; at most 500) that hold `.git`. Per repository `git ls-files -z` (at most 20 000 paths; a path whose real path leaves the repository is dropped, so a symlinked rule file pointing elsewhere is never read), given to `al scout`'s detection (`tools/al_adopt.py`, `tools/al_suite.py`, which read files and run nothing), `symbolic-ref`, `status --porcelain`, `log -1`. git runs with `core.fsmonitor=false`, `core.untrackedCache=false`, `core.hooksPath=/dev/null`, `--no-optional-locks` (the index is never rewritten), no prompt, no inherited `GIT_*` variables, and its process group is killed after 5 s. A repository is reread only when its signature changes (stats of `.git/HEAD`, `.git/index`, `.git/logs/HEAD`, its directory, the sibling `NAME.al/agentic.rkt` and its layer's IR); rescanned every 60 s and on **refresh** (at most once per 5 s). A layer of a workspace repository that has `agentic-layer.json` is watched like any other |
| `<jobs-dir>/<id>/job.json`, `output.log`, `docs/`, `steps/` | the Job view: an action's steps (argv, the command line as shown, `stream` (`agent` for the author step), exit code, times, `reason` of a failed step), its status and `reason`, the runner's `pid` / `host` / `heartbeat` (as a run's `state.json`), `path` (the folders of the tools found by tools/al_tools.py, then the dashboard's `PATH`: the steps' `PATH`), `preflight` (the `al doctor LAYER --for …` argv run before the first pending step), `retries` (`{"from", "at"}` per retry), changed files; its output; its documents. `<jobs-dir>/locks/` holds one lock file per repository. The second place the dashboard writes, outside every repository and layer. Two dashboards with different `--jobs-dir` do not see each other's locks. `steps/<name>.log` is one step's whole output, stdout and stderr as they arrived (the author step's raw Claude Code event stream), started afresh when a retry runs the step again, while `output.log` keeps every attempt. `steps/<name>.journal.jsonl` is an agent step's journal, written by the runner as the stream arrives (`AgentJournal`, which wraps the run Step view's transcript normaliser): one event per line with `t`, `kind` and `text`, the kinds `message`, `tool` (`id`, `tool`, `target`, `doing`), `tool.done` (`bytes`, `lines`, `error`, `ms`), `error`, `report` (`decisions`, `left_out`), `result` (`cost`, `turns`, `ms`, `tokens`) and `hidden` (`what`: hook, init, system, rate, task, thinking, meta), every path shortened (the layer and repository as shown, the home directory as `~`); only its one-line form goes into `output.log`. The page reads a step through `/api/job/journal` and `/api/job/log`, never a step file whole |

Transcripts (`raw.jsonl`) are read incrementally (only new bytes; a half-written last line waits
for its newline; a truncated or replaced file restarts with a `reset`). Claude Code stream-json
events become rows:

| Event | Row |
|---|---|
| `system/init` | "session started · model · permission mode" (its `cwd` makes paths relative) |
| `assistant` text / thinking / tool_use | says / thinks (marker only) / one line per tool: `read <path>`, `edit <path>` + diff, `write <path> (n lines)`, `$ <command>`, `search /pattern/ in <path>`, `find files <glob>`, `delegate: …`, todo list |
| `user` tool_result | attached to its tool call: size, line count, error flag, preview; `tool_use_result.structuredPatch` as a diff, `stderr` shown |
| `user` with a plain string | a user row |
| `system/task_started`, `task_notification` | task rows (hidden with "system") |
| `rate_limit_event` | "rate limit: allowed (seven_day 38%)" (hidden with "system") |
| `system/thinking_tokens`, `system/ui_invalidate` | no row; thinking tokens are counted |
| `result` | cost, turns, duration, tokens (in / out / cache read / cache write), error subtypes such as `error_max_turns` |
| a line that is not JSON | text (codex and custom CLI agents write plain text); a broken JSON line is marked "unreadable" |
| `{"synthesised": …}` / `{"script": …}` | the mock agent |

Cost and tokens come from each agent's `result` event; while a step runs, tokens are summed from
its messages and the cost shows as "so far" for the finished steps. Paths in agent output are
shown relative to the run's worktree (`layer:` for the layer, `~` for the home directory).

## Safety

- **Three writes, guarded.** Only GET and HEAD, plus `POST /api/models`, `POST /api/start` and
  `POST /api/action` (with `POST /api/job/retry`, which belongs to the actions); every other method and path is 405 (GET on `/api/models` or `/api/start` is
  405 too), and no endpoint stops or changes a run. All three writes pass one guard
  (`Handler._write_guard`): the token as for every request, but from the `Authorization` header or
  the cookie only (a token in the query string is refused); a loopback `Host` when there is no
  token; an `Origin` header equal to `http://<Host>` (a missing one is refused); the
  `X-AL-Dashboard: 1` header, which a plain HTML form cannot send; `Content-Type:
  application/json`; a body of at most 4 KiB (models, actions) or 64 KiB (start); and no
  `--read-only`.
- **The model setting.** `POST /api/models` sets or resets one tier of a layer's local model setting
  (`agents/settings/models.json`). The tier and model id are validated like `al models` (a declared
  tier; a non-empty id without whitespace or control characters; an unknown id gets a note), and the
  file is replaced atomically (temporary file, `fsync`, rename). `--read-only`
  (`al dashboard --read-only`) turns the write off; the panel then says so.
- **Starting work.** `POST /api/start` is also refused with `--no-start`, and in a container
  (`--in-container`) unless `--allow-start-in-container` (403, naming the flag); for a layer without
  `adws/adw_modules/launch.py` (409, "run `al gen`"). The request may only name what `GET
  /api/startable` lists for that layer: a workflow of its compiled IR or one of its commands or hops
  (400 otherwise), with a request key, a declared class and tier, valid model ids and an issue mode
  the layer supports. The dashboard then runs the layer's own `adws/dispatch.py --start --json` as a
  short subprocess: an argv list, never a shell, the prompt on stdin. The launcher (the layer's
  `launch.start`, the same code as `al run`) checks it all again against the compiled layer, returns
  the run of a request key it has seen (a double click or a retry starts one run), asks for a
  confirmation (409) for a workflow that merges or deploys without a person and for the same request
  within five minutes, refuses at the layer's day limit or usage threshold (429), writes the prompt to
  `agents/<id>/request.md` (it never reaches a command line), and starts the run detached in its own
  session, so stopping or restarting the dashboard does not stop the run. A single-command run has no
  review and no ship step: it cannot ship. "Log as an issue, start now" opens the issue with a
  `<!-- al:claimed run=<id> -->` marker in its body, which the layer's poller and webhook skip;
  "let the trigger start it" opens it with the issue input's labels and starts nothing.
- **Actions.** `POST /api/action {"repo": KEY, "action": NAME}` (and `"confirm": NONCE`) runs `al` on a
  scanned repository, from this fixed allow-list only (AL = the found tool, APP = the repository's real
  path, LAYER = its layer, else `<folder>/<name>.al`):

  | Action | States | Writes | argv |
  |---|---|---|---|
  | `scout` | all | no | `AL scout APP` (stdout = document `LLM.txt`) |
  | `create` | none, setup | yes | `AL init APP`; `AL author LAYER [--agent …] --raw`; `AL gen LAYER`; `AL check LAYER` |
  | `adopt` | setup | yes | preview: `AL adopt LAYER --app APP --dry-run --diff` (stdout = `preview.diff.txt`); then `AL init APP`; `AL author LAYER --raw`; `AL adopt LAYER`; `AL gen LAYER`; `AL check LAYER` |
  | `gen` | layer | yes | `AL gen LAYER` |
  | `check` | layer | no | `AL check LAYER` |
  | `verify` | layer | yes | `AL verify LAYER --json` (stdout = `verify.json`) |
  | `explain` | layer | no | `AL explain LAYER --md <job>/docs/EXPLAIN.md` |

  Before its first step a job runs `AL doctor LAYER --for <its subcommands> [--agent …]` (10 s at most,
  not a step): a missing tool fails the first step with that sentence and skips the rest, so no agent time
  is spent. The steps run with `PATH` = the folders of the tools al_tools finds, then the dashboard's own,
  so the al tool's `uv` shebang works under a unit's fixed `PATH`; when `uv` itself is not found the job is
  written as failed at once and no runner starts. The author step runs with `--raw`, so the runner receives
  the agent's event stream and condenses it itself (see `steps/` under "What it reads").

  `POST /api/job/retry {"job": ID, "from": STEP}` runs a failed or interrupted job again from STEP, which
  must be its first step that did not pass (409 otherwise, and for a preview, a running job, a repository
  that is no longer a git repository in the workspace, or another job on it; 404 for an unknown job; 400
  for any other key, a non-string value or whitespace). It has the same write guard as `/api/action` and
  is refused by `--read-only` and `--no-actions` (403). The job is rewritten in place: STEP and the steps
  after it go back to pending, a reset `author` step gets `--accept-existing`, `retries` gets an entry,
  and the runner is launched again; it skips the steps that passed and writes `--- retry from STEP ---`
  into `output.log`.

  The request passes the same checks as `POST /api/models`, in the same order and with the same
  messages; any other key, a non-string value or whitespace in a value is 400, an unknown action 400,
  a repository that is not scanned 404. Then: the state must allow the action (409); the repository's
  real path must still be a git repository inside a workspace folder (409, so one replaced by a symlink
  after the scan is refused); `create` and `adopt` are refused when any `agentic.rkt` exists in LAYER or
  in the repository, committed or not (409, "never overwritten" / "has uncommitted changes"); no other job
  may be running on the repository (409). The argv lists are built from the scanned entry only, run
  without a shell (`Popen(argv, shell=False)`), never with a flag from the request. A writing action
  without `confirm` returns a preview (its command lines, the paths it writes, a nonce; for `adopt` a
  read-only dry-run job whose document is the diff); the confirmation must echo the nonce, which is
  single use, expires after 10 minutes and is bound to the repository's commit, branch, status and layer
  definition: a change in between is 409 "changed since the preview". A job is run by its own detached
  process (`dashboard.py --run-job DIR`, `start_new_session`), which holds the repository's `flock` for its
  lifetime (a second runner on the same repository records `refused`), refreshes its heartbeat every 5 s,
  kills a step's process group at its timeout, and stops at the first failing step. It survives a
  dashboard restart; one whose runner is gone is rewritten as `interrupted`, never shown as running.
  `--no-actions` (403 "actions are off") and `--read-only` turn actions off; without the al tool they are
  409 and every button says where it looked. No action in `--in-container` or `al dashboard --podman`.
- **Served files** are limited to a watched layer's `agents/`, `artefacts/`, `specs/`, its
  `agentic-layer.json` / `agentic.rkt` and `EXPLAIN.md`. Traversal (`..`, encoded or not), absolute
  paths, NUL bytes and anything whose real path leaves that tree (symlinked files or directories)
  are refused. Files are sent with `Content-Security-Policy: sandbox`, `nosniff`, and unknown
  types as downloads, so an HTML artefact cannot run in the dashboard's origin.
- **A job's files** (`/api/job/doc`, `/api/job/journal`, `/api/job/log`, `/api/job/file`) are read-only and
  confined to that job's directory: the id must match the job id pattern, the step must be one of the
  job's steps, the file a regular file whose real path stays in the job directory (a symlinked step file,
  `..`, an encoded traversal, an absolute name, a NUL, another job's file and an unknown step are all 404).
  `/api/job/journal?id&step&from=N` answers at most 5000 events with `next_from`; `/api/job/log?id&step&from=N`
  (or `&tail=N`) at most 20 000 lines or 2 MB, ANSI stripped, the home directory as `~`, with `next_from`
  and `total`; `/api/job/file?id&name` downloads only `output.log`, `steps/<step>.log` or
  `steps/<step>.journal.jsonl`, as an attachment with a `sandbox` CSP. The job output's tail
  (`/api/tail?what=job`) shows the home directory as `~` too.
- **Documents** (`/api/doc`, `/api/doc/download`, `/api/doc/zip`) serve only files listed for that run:
  in `specs/`, or in the run's own `agents/<id>/` and `artefacts/<id>/`, regular files whose real path
  stays in the layer. Anything else (traversal, absolute paths, symlinks out, another run's file,
  `state.json`, transcripts, a missing file) is 404. Only `.md`, `.txt`, `.json` and `.log` up to 1 MB
  are shown inline; Markdown is built as DOM text by the page's own renderer (raw HTML stays text,
  links only `http(s)`, `#` or `/`, with `rel="noopener noreferrer"`); downloads are attachments with a
  `sandbox` CSP; the zip is built in memory (at most 50 MB of files).
- **Agent output is untrusted.** The page never parses data as HTML: everything is set with
  `textContent` or SVG attributes (a test checks the script for `innerHTML` & co.), JSON responses
  escape `<`, `>` and `&`, and the page's CSP allows only its own script (no inline, no eval).
  A transcript full of `<script>` and `<img onerror>` displays as text (tested in Chrome).
- **Network.** Loopback by default, with a Host-header check against DNS rebinding. A non-loopback
  `--bind` needs `--token`; then every request needs it.

## Its own podman container

```bash
kit/dashboard/run-in-podman.sh [--port 8765] [--podman-socket] [--read-only] [--no-start] DIR [DIR …]
```

It builds a small image (`python:3.12-slim` plus the script and its static files) and runs it
rootless as your user (`--userns=keep-id`, so it can read state files that only you can read),
with every DIR mounted **read-only at the same path**, `--cap-drop=ALL`,
`--security-opt=no-new-privileges`, `--read-only`, pids/memory/CPU limits, on an `--internal`
podman network (no route out), publishing only `127.0.0.1:PORT`. Inside, it must listen on all
interfaces for the published port, so the script generates a random token and prints the URL.

The one writable mount: for each DIR that is a layer (has `agentic-layer.json`), its
`agents/settings` directory is created if missing and mounted read-write, so the Models panel can
switch that layer's models. A directory is mounted rather than the file because a bind-mounted
single file cannot be replaced atomically. Layers found by scanning a DIR get no writable mount, and
their panel says "not writable here" with the `al models` command to use on the host instead.
`--read-only` mounts nothing writable and turns the panel's write off. Starting work is always off
in the container: it has no `uv`, no application checkout and no writable layer, so the Start work
panel says so (`--no-start` is accepted and passed on). The container has no workspace and no
actions either (`--in-container`): they need the host's git and al.

What the container can and cannot show:

| | Host | Container | Container + `--podman-socket` |
|---|---|---|---|
| runs, steps, transcripts, logs, costs, artefacts | yes | yes | yes |
| slot pools | exact (`/proc/locks`) | inferred from run state | inferred |
| port leases alive? | yes (owner pid) | unknown | unknown |
| container name / removed | from run.log | from run.log | from podman |
| container state, CPU, memory | podman CLI | no | yes |

`--podman-socket` mounts your podman API socket. Mounted read-only or not, **a socket gives full
control of your podman** (start, stop, exec): the `:ro` flag only stops the file from being
replaced, not the API from being used. The dashboard only sends GET requests, but you are trusting
it with that power, which is why it is off by default.

## Tests

```bash
cd kit/dashboard
uv run --with pytest pytest -q tests                        # unit + HTTP tests (no browser, no network)
uv run --with pytest --with playwright pytest -q tests      # + real-browser tests in the host's Chrome
AL_DASHBOARD_REAL_LAYERS=../../layers uv run --with pytest pytest -q tests -k real   # every real transcript you have
```

Browser tests use the host's Google Chrome or Chromium (`AL_DASHBOARD_CHROME` to choose); they
skip without Playwright or Chrome, and nothing is downloaded. A run page sets `#main[data-rendered]`
(a count of its completed loads) for tests to wait on instead of sleeping, and a fetched detail equal to
the one shown does not re-render, so an idle run page keeps its nodes
(`tests/test_browser_layout.py::test_an_idle_run_page_keeps_its_nodes`); with `AL_DASHBOARD_SHOTS=DIR` its
two layout tests save `run-graph-sdlc-1280.png` and `cost-table-totals.png` to DIR. `podman`-marked tests skip without
podman and `python:3.12-slim`. The fixture layers are code (`tests/fixtures.py`, `python
tests/fixtures.py DIR` writes them out): a passed run, a failed run with a handoff, a resolve loop,
two review rounds, a fan-out, a run in progress with a half-written line, hostile HTML, corrupt
JSON, a run without `state.json`, a plain-text agent and a mock run. `tests/fixtures_events.py`
adds runs with `events.jsonl` (all file times identical, so only the events can give the right
timings), a running check whose log grows, a run that died and a fan-out candidate naming its parent.
`tests/test_generated_layer_e2e.py` generates a layer for a pinned example repository, starts its
`adws/dashboard.py`, runs two mock-agent workflows through the generated scripts with the page open
in Chrome, and checks the page live (skipped without Chrome or the testbed).
`tests/test_usage_models.py` has the runtime make a subscription run and a run a subscription limit
stopped (the mock agent replays the events of `kit/runtime/tests/data/usage/`), and checks the
"Subscription usage" panel, the limit marks on the timeline and the "API-equivalent" labels, in the
model and in Chrome; with `AL_DASHBOARD_SHOTS=DIR` its browser test saves
`subscription-usage-panel.png` and `subscription-usage-run.png` to DIR.
`tests/test_documents.py` covers a run's documents: the list (plan, `PR.md`, patch plans, verdicts),
path mapping, view, download, zip, refusals (all 404), the token and a new document arriving on the
event stream. `tests/test_browser_documents.py` (Chrome) checks the Documents tab, the rendered and raw
view, the download, an inert `<script>` in a plan, live appearance and the step panel's link; with
`AL_DASHBOARD_SHOTS=DIR` it saves `run-documents.png` and `run-documents-step-link.png` to DIR.
`tests/test_start.py` starts work through the real server and the layer's own launcher
(`tests/start_layer.py` builds a git app and runnable layers the way `al gen` lays them out, with the
mock agent): the catalog, a workflow and a `/plan-chore` single-command run with its plan as a
document, one run per request key (also from two threads at once), both confirmations, a run that
outlives the dashboard, every refusal, and the issue race against the layer's poller in both orders
and in trigger mode (`tests/fake_forgejo.py`, an in-memory Forgejo). `tests/test_browser_start.py`
(Chrome) types `/plan-c`, picks the completion, starts and lands on the run, and opens the ship
confirmation; with `AL_DASHBOARD_SHOTS=DIR` it saves `start-form.png`, `start-confirm.png` and
`start-run.png` to DIR.

`tests/test_workspace.py` covers the workspace scan (states, sibling layers, al scout's summary, depth,
symlinks out of the folder, the file and time bounds, git without fsmonitor or hooks, the cache and the
refresh limit, the default workspace, the al lookup, escaping); `tests/test_actions.py` the actions (the
allow-list and every refusal, the write checks shared with `/api/models`, `--read-only`, `--no-actions`,
preview and nonce, the adopt dry run, the per-repository lock, a job surviving a restart, a dead runner
shown as `interrupted`, the job's tail, changed files, document confinement, never overwriting a
definition, a missing al tool, a missing tool as a step's failure reason, the prerequisite check stopping a
job before any step, "Retry from STEP" and its refusals, the tools table, the job's `PATH`), with
`tests/workspace_fixtures.py`: four invented repositories (`pantry`, `quill`, `comet`, `fern`) and
`fake_al`, a stand-in for al that records its argv, can fail a subcommand (`fail`; its `doctor` answers
`ok` otherwise), can replay a recorded agent stream for one (`streams`, with `job_stream`, which puts the
test's workspace and home into it) and can keep one running (`hold`).
`tests/test_job_journal.py` covers the job journal: the condenser on the invented streams of
`tests/data/job-streams/` (`author-running`, `author-finished`, `author-failed`: hooks with long output,
tool calls, failed compiles, a report with Decisions and Left out) — hidden bookkeeping, condensed tool
calls, errors, the "doing" lines, plain and corrupt lines, the home directory as `~`, report parsing — then
the runner's `steps/` files and condensed `output.log`, a retried step's fresh files, `job_detail` for a
running, a finished and a failed job and for one from before `steps/`, the paged routes and every refusal
of `/api/job/file`. `tests/test_browser_job_activity.py` (Chrome) opens a running, a finished and a failed
create job at 1280 and 390 px: the steps row and its "doing" line, hooks hidden until **show everything**,
search, the outcome card, Decisions and Left out, hostile agent text staying text, and a failed job's step,
reason, **Retry from author** and **Download log** inside the first viewport, the download byte-equal to
the raw stream; the page only reads `/api/job/journal` and `/api/job/log`. With `AL_DASHBOARD_SHOTS=DIR` it
saves `job-running-1280.png`, `job-running-390.png`, `job-finished-1280.png`, `job-finished-390.png`,
`job-failed-1280.png` and `job-failed-390.png`.
`tests/test_browser_workspace.py` (Chrome) checks the Repos view, the selection in the URL, the filter,
the adopt preview's diff (an injected `<script>` stays text), Confirm and a job streaming to `passed`, and
disabled actions without al, and a failed job's reason with its Retry button; with `AL_DASHBOARD_SHOTS=DIR`
it saves `workspace-repos.png`, `workspace-adopt-preview.png`, `workspace-job.png`, `job-missing-tool.png`
and `job-retried.png`. `tests/tools/test_dashboard_workspace.py` (at the
repository root) runs the same flows end to end with the real al and the mock author agent.
`tests/test_writes_together.py` runs both features in one server: `--read-only` refuses all three
writes, `--no-start` and `--no-actions` each refuse only their own, each write has its body limit,
and both features' GET routes and banner states are there.

## Demo scripts (`demo/`)

| Script | Does |
|---|---|
| `live_demo.py` | builds a tiny app and its layer in a temp directory, starts the dashboard and three runs, records Chrome with Playwright and writes `live.mp4` / `live.gif`. Planning, commit and review use the mock agent; the build step uses `replay_agent.py`, a scripted stand-in CLI agent that streams a Claude-format transcript while it really edits the worktree (no LLM, $0). Checks run for real in podman |
| `screenshots.py` | screenshots of real local runs; refuses to write one whose page text contains the home directory, `/home/`, `~/` or the user name |
| `gif.py` | GIF encoder with the dashboard's own colours as palette, so status colours survive a small file |
| `perf.py` | the dashboard's own CPU use, idle and with busy runs |

## Declared in the layer

```racket
(dashboard local #:port 8765 #:bind "127.0.0.1" #:also ("../other.al") #:poll-ms 750 #:systemd #f)
(dashboard podman #:port 8765 #:podman-socket #f)   ; the container recipe above
(dashboard none)
```

`local` is the default when a layer says nothing. `al gen` then writes `adws/dashboard/` (this
script and its page; for `podman` also the `Containerfile` and `run-in-podman.sh`) and the launcher
`adws/dashboard.py`, which starts it with the layer and its `#:also` layers (paths relative to the
layer) and the declared port, bind address and poll interval; flags given to the launcher win
(`uv run adws/dashboard.py --port 9000 --open`). A non-loopback `#:bind` needs `#:token-env "VAR"`
(a compile-time rule); the launcher hands the token over in the environment, not on the command line.
`#:systemd #t` adds a user unit under `systemd/` (machine-specific, so git-ignored in an in-repo
layer; everything else is the same on every machine and committed). `al check` requires these files
when a dashboard is declared, and notes runs that died.

`al dashboard [LAYER_OR_DIR …] [--port N] [--open] [--podman]` runs this kit's dashboard over any
layers (a directory with `agentic-layer.json`) and directories (scanned), the current directory
when none is given: one page for several codebases. `--workspace DIR` (repeatable), `--workspace-depth N`,
`--no-workspace` and `--no-actions` are passed through (not with `--podman`).
