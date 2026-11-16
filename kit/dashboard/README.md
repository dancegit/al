# al work dashboard

A live view of what the agents of one or more layers are doing (with two guarded writes: which
model each tier uses, and starting work: a workflow or a single command): which runs are in
flight and what each agent is doing right now, the workflow of every run as a graph that colours
in as steps finish, where the time went, what it cost, and every agent's transcript formatted for
people. One Python file (standard library only) serves one page (HTML, CSS and vanilla JS, no
CDN, no build step) and pushes updates over Server-Sent Events.

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
| `--in-container` | the host's processes and locks are not visible: infer slot use from run state; starting work is off |
| `--read-only` | change nothing: no model settings, no starting work |
| `--no-start` | never start work from the page (the Models panel still writes) |
| `--allow-start-in-container` | with `--in-container`: allow starting work, only for an operator-built image that has `uv`, the application checkout and a writable layer (the image of `run-in-podman.sh` has none of them) |

## What you see

| View | What it shows |
|---|---|
| **Now** (`1`) | every run in flight: layer, workflow, request, current step, the agent's latest action in one line ("editing tests/test_basic.py", "running pytest", "thinking"), elapsed time, cost so far, whether it waits for a slot, its container (name, state, CPU and memory when podman is available); a **Subscription usage** panel per layer: what the agent CLI last reported about the subscription's usage windows (5-hour, weekly, or whatever name the CLI uses): share used as a bar, status, reset time, when and by which run it was reported, and the layer's threshold when it declares one. It has three states: the windows, "API key: not applicable" (the last call ran on an API key), "no data yet"; where the CLI gave no share it says so. It is not a live meter: it changes when an agent call ends. Then slot pools per layer (runs / agents / checks, held / total, from the lock files) and leased ports; recently finished runs. Dollars of runs whose calls ran on a subscription are marked "API-equivalent" |
| **Runs** (`2`) | history with filters (layer, workflow, status, class, dates, text), sortable by time, cost and duration; count, pass rate, total and median cost and time |
| **Run** | the workflow as a graph from the layer's IR (agent / check / review / ship / fan-out nodes; `when` branches stacked in one column; resolve loops and the review → patch → re-check loop as back-edges; fan-out candidates as lanes linked to their own runs), coloured live with duration and cost per node; a timeline of every step, check attempt, resolve agent, reviewer, patch round and gate re-run, with agent / check / slot-wait totals, and marks for what the CLI reported about subscription usage, a limit stop and a wait for a reset (`limit.*` events); cost and tokens per agent ("API-equivalent" when the calls ran on a subscription); the outcome: gate decision and reasons, ship target or PR, **why the run stopped** (a run a subscription limit stopped carries a "limit stop" mark and a "Subscription limit" block with the window, the share used and the reset time) and what the agent asks a human to do, review verdicts per round and reviewer with issues by severity |
| **Step** | the exact prompt (sections collapsible), the transcript as one row per event: what the agent said, tool calls summarised (file read / edit with a diff / write, shell command, search pattern), results collapsed with size, errors marked, thinking as a marker, the final cost / turns / time. Filters (kind, errors only, tool), search with highlight, follow mode, raw JSON of any event, download of the raw `.jsonl`. Checks: every attempt's log with ANSI colours, its exit code and the resolve agent that followed |
| **Documents** | what the run has written for people to read, as soon as each file exists: the plan, the `.md` / `.txt` / `.json` / `.log` outputs its steps recorded (a review's `patch-<n>` plans among them), review patch plans, `PR.md` (the handoff or PR text) and other text files in `agents/<id>/`, each reviewer's verdict (`review-*/output.txt`) and text artefacts; with the step that produced each, its size and time. **View** renders Markdown in the page (with a raw toggle), pretty-prints JSON and shows text as text; **Download** saves the exact file, **Download all** a zip of them all. A step's panel links the documents it produced (the plan on `plan-feature`). A new document appears live |
| **Artefacts** | screenshots as thumbnails (click for full size), videos playable in the page with their keyframes, logs with their tail, all with the step that collected them |
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

Every view is a URL (`#/run/flask/11bf99ac/step/build?q=static`), so a link can go into a PR or a
chat. Keys: `j`/`k` move through runs, nodes or transcript events, `Enter` opens, `/` searches,
`f` follows, `Esc` goes back, `?` lists them.

## What it reads, and how

Everything comes from files the runtime already writes. The dashboard itself writes one file,
agents/settings/models.json (the Models panel; off with `--read-only`); starting work is done by the
layer's own `adws/dispatch.py --start`, which writes the run's files (off with `--read-only` or
`--no-start`).

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

- **Two writes, guarded.** Only GET and HEAD, plus `POST /api/models` and `POST /api/start`; every
  other method and path is 405 (GET on either of the two too), and no endpoint stops or changes a
  run. Both writes pass one guard (`Handler._write_guard`): the token as for every request, but from
  the `Authorization` header or the cookie only (a token in the query string is refused); a loopback
  `Host` when there is no token; an `Origin` header equal to `http://<Host>` (a missing one is
  refused); the `X-AL-Dashboard: 1` header, which a plain HTML form cannot send; `Content-Type:
  application/json`; a body of at most 4 KiB (models) or 64 KiB (start); and no `--read-only`.
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
- **Served files** are limited to a watched layer's `agents/`, `artefacts/`, `specs/`, its
  `agentic-layer.json` / `agentic.rkt` and `EXPLAIN.md`. Traversal (`..`, encoded or not), absolute
  paths, NUL bytes and anything whose real path leaves that tree (symlinked files or directories)
  are refused. Files are sent with `Content-Security-Policy: sandbox`, `nosniff`, and unknown
  types as downloads, so an HTML artefact cannot run in the dashboard's origin.
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
panel says so (`--no-start` is accepted and passed on).

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
skip without Playwright or Chrome, and nothing is downloaded. `podman`-marked tests skip without
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
when none is given: one page for several codebases.
