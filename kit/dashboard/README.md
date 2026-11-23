# al work dashboard

A live view of what the agents of one or more layers are doing (with guarded writes: which
model each tier uses, how deep each run's checks go, starting work: a workflow, a single command, a failed
run again or a batch of the full suites, and settling a finished run), which runs still need someone, which runs are in
flight and what each agent is doing right now, the workflow of every run as a graph that colours
in as steps finish, where the time went, what it cost, and every agent's transcript formatted for
people. One Python file (standard library only) serves one page (HTML, CSS and vanilla JS, no
CDN, no build step) and pushes updates over Server-Sent Events. Its **Repos** view also lists the git
repositories beside the layers with their al state and runs allow-listed `al` commands on one of them,
each confirmed first when it writes (see Safety, "Actions").

![Three runs started while the page is open; the third waits for a run slot](../../docs/demo/dashboard/live.gif)

[live.mp4](https://github.com/dancegit/al/releases/download/media-v1/demo-dashboard-live.mp4) (52 s): three runs of a demo layer, two at a time;
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
| `--scan DIR` | find layers below DIR, in-repo ones included (depth `--scan-depth`, default 4; `.git`, `node_modules`, `trees`, `testbeds`, … are skipped); rescanned every 10 s, so new layers appear; a layer in another git worktree of a repository already listed, with the same layer name, is not listed again (`--layer` always lists) |
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
| `--no-voice` | no voice helper panel and no speech service (implied by `--in-container`) |
| `--voice-dir DIR` | where voice conversations are kept (default `$XDG_STATE_HOME/al/dashboard-voice`, else `~/.local/state/al/dashboard-voice`) |
| `--voice-fake FILE` | tests: run the speech service with its fake engines and the transcripts of FILE (`{"transcripts": [...]}`) |

## What you see

| View | What it shows |
|---|---|
| **Now** (`1`) | every run in flight: layer, workflow, request, current step, the agent's latest action in one line ("editing tests/test_basic.py", "running pytest", "thinking"), elapsed time, cost so far, whether it waits for a slot, its container (name, state, CPU and memory when podman is available); a **Subscription usage** panel per layer: what the agent CLI last reported about the subscription's usage windows (5-hour, weekly, or whatever name the CLI uses): share used as a bar, status, reset time, when and by which run it was reported, and the layer's threshold when it declares one. It has three states: the windows, "API key: not applicable" (the last call ran on an API key), "no data yet"; where the CLI gave no share it says so. It is not a live meter: it changes when an agent call ends. Then slot pools per layer (runs / agents / checks, held / total, from the lock files) and leased ports. For a layer that places its runs on hosts (`(placement auto #:hosts …)`) a **Hosts** panel: each declared host with this layer's runs executing there now of its limit (and runs being started), its load average and CPUs, memory free, disk free, whether the sandbox image is there and how each agent logs in; a host that gave no answer says "no answer" with the reason. The run counts are live; the load facts are as old as the scheduler's last survey, and the panel says how old that is ("no survey yet" before the first). Each run of such a layer carries a **host badge** (`⌂ build-1`, `⌂ home` for a run that stayed; `· gate at home` once it came back for the gate, `· no answer` when it was lost) on its card, in the runs list and on its page. Then recently finished runs. Dollars of runs whose calls ran on a subscription are marked "API-equivalent" |
| **Runs** (`2`) | opens on **Needs you**: the runs that still need someone, each with a one-line reason and the next action (below). Chips switch to **Running**, **Landed**, **Settled** or **All**, each with its count; the filters (layer, workflow, status, class, dates, text) apply inside a chip, sortable by time, cost and duration; count, pass rate, total and median cost and time. The header reads "N need you", and the Runs link carries the same count; a host column when any listed run has a host. The status `lost` is a run whose host stopped answering: it is finished (it counts in the pass rate like a failed run) and its page says why and when |
| **Run** | the workflow as a graph from the layer's IR (agent / check / review / ship / fan-out nodes; `when` branches stacked in one column; resolve loops and the review → patch → re-check loop as back-edges; fan-out candidates as lanes linked to their own runs), coloured live with duration and cost per node; a timeline of every step, check attempt, resolve agent, reviewer, patch round and gate re-run, with agent / check / slot-wait totals, setup bars at the top for a sandbox image build (`building sandbox image <recipe> (<size>)`) and the preflight (`preflight: all checks passed` or `preflight: missing <checks>`) from `env.build.*` / `env.preflight` events (while a build runs the run's current action reads "building the sandbox image"), and marks for what the CLI reported about subscription usage, a limit stop and a wait for a reset (`limit.*` events); cost and tokens per agent ("API-equivalent" when the calls ran on a subscription); the outcome: gate decision and reasons, ship target or PR, **why the run stopped** (a run a subscription limit stopped carries a "limit stop" mark and a "Subscription limit" block with the window, the share used and the reset time) and what the agent asks a human to do, review verdicts per round and reviewer with issues by severity |
| **Step** | the exact prompt (sections collapsible), the transcript as one row per event: what the agent said, tool calls summarised (file read / edit with a diff / write, shell command, search pattern), results collapsed with size, errors marked, thinking as a marker, the final cost / turns / time. Filters (kind, errors only, tool), search with highlight, follow mode, raw JSON of any event, download of the raw `.jsonl`. Checks: every attempt's log with ANSI colours, its exit code and the resolve agent that followed. Under the head of an agent step the runtime checked, one line: "Stayed in its step" (HEAD unchanged, no refs changed, nothing outside the worktree), or "Left its step:" with the violation, what was restored and what was left for a person Next to a check's exit code an amber `N skipped` badge shows when the attempt skipped tests (its title lists the reasons, `no podman ×2`), and `in ENV` for a check that runs `#:in` another environment option; the graph's check node reads `N skipped` too. A check the runtime decided not to run has a decision instead of an attempt: a `not affected` badge with why and the paths that were compared, or a `reused` badge with the step whose pass on the same tree was reused and the tree hash (the log shown is that run's log); neither carries an exit code. |
| **Documents** | what the run has written for people to read, as soon as each file exists: the plan, the `.md` / `.txt` / `.json` / `.log` outputs its steps recorded (a review's `patch-<n>` plans among them), review patch plans, `PR.md` (the handoff or PR text) and other text files in `agents/<id>/`, each reviewer's verdict (`review-*/output.txt`) and text artefacts; with the step that produced each, its size and time. **View** renders Markdown in the page (with a raw toggle), pretty-prints JSON and shows text as text; **Download** saves the exact file, **Download all** a zip of them all. A step's panel links the documents it produced (the plan on `plan-feature`). A new document appears live |
| **Artefacts** | screenshots as thumbnails (click for full size), videos playable in the page with their keyframes, logs with their tail, all with the step that collected them; when an artefact's limit (`#:max-files`) kept files from the reviewers, a line under it says how many: "shots: 3 more files matched and were not handed to the reviewers" (the manifest's `left-out` entry) |
| **Kept on a branch** | on the run page of a workflow without a ship step: the branch its work was committed on, the commit, how many files changed, each kept file as a link to its copy in `artefacts/<id>/kept-files/`, the whole change as `changes.diff` (the files and the diff open in the Documents viewer), the files too large to copy (they are on the branch only); "nothing changed" when the run left nothing, "keeping failed" with the reason when the worktree was kept instead |
| **Repos** (`4`) | the workspace: every git repository of the folder(s), searchable, with its state badge (`layer`: an `agentic.rkt` in it or in a sibling `NAME.al`; `setup`: rule files, prompts, workflow scripts or a hand-built suite, the same detection as `al scout`; `none`), branch, a mark for uncommitted changes and its last activity; **refresh** rescans. A selected repository (`#/repo/<key>`) shows al scout's "Existing agent setup" summary, its layer with a link to its workflows and runs, its jobs, and the **al** panel: one button per action with the exact command line(s) it runs, disabled with the reason when it does not apply (`needs the al tool; looked for …`, `--read-only`, `--no-actions`, the state), and a tools table: what `al doctor` sees on this machine (racket, raco, uv, git, the author agent's CLI: where each was found, or the sentence saying where al looked, and its version; kept 5 minutes). **create** and **adopt** have a placement picker above their command lines: **beside the repository** (`<folder>/<name>.al`) or **inside the repository** (`<repository>/agentic`), each with what it means, the resulting path and three checks (exists and is kept / will be created; writable; not a layer yet), disabled with its reason when it cannot be used; the command lines are those of the selected placement. A writing action first shows what it runs and writes (for adopt the dry run's diff, line by line; for create and adopt the placement with its path and the detected choices, each editable, with a sentence saying what it decides and its fact) and runs on **Confirm**. **generate a layer…** beside **refresh**, and **guided** in the al panel of a repository without a layer, open guided generation (below) |
| **Job** | one action (`#/job/<id>`), in the order a person reads it: the header (repository, action, for a create or adopt its placement (`placement: inside the repository`), status, exit code, duration, why it failed, `needs a person: N` when the author left items out, **Download full output**); once it ended, the **outcome** card: for a passed job what was created (the layer with the files added, changed and removed) or adopted, what is `UNVERIFIED` and the next action (the last `next: …` line the steps printed); for a passed create or adopt the same card also holds the **result card** of its `result` step (forge clause, workflows, adopted rules, commands and suite workflows, what was left behind, unverified commands, the next steps, the first of which is the next action; paths as the job shows them), for a failed or interrupted job the failing step, the reason in one sentence (a generic "exited N" is replaced by the step's last error), up to 12 last relevant lines, **Retry from STEP** (asks first; the steps from the first that did not pass run again in the same job, a retried `author` with `--accept-existing`) and **Download log**; the **steps row**, one chip per step with status, exit code and time, a failed step's reason, and under the running one what it is doing right now ("reading the CI files", "compiling, attempt 2", "writing agentic.rkt", or a plain step's last line); the **activity** of the selected step (the running one, else the failed one, else the last agent step, so `author` rather than `scout`; a click picks another and the address keeps it as `?step=`): an agent step's journal with its messages as Markdown, one line per tool call (tool, target, result size, duration), errors in red, the final report and the totals, hooks, start-up, rate limits and other bookkeeping only with **show everything**; a plain step's own output as text (ANSI stripped); both the latest rows with **show earlier** and a search box, and the step's log (an agent step's **raw events**) and journal to download; the author's **Decisions** and **Left out and why** cards; the layer's files it added, changed or removed, and whether the repository changed (for a layer written inside the repository, when only `agentic/` changed: "the layer was written inside the repository: N path(s); commit agentic/ with the code", not a warning); its documents (`LLM.txt` of scout, `preview.diff.txt` of an adopt preview, `choices.json` of a create or adopt preview, `result.json`, `verify.json`, `EXPLAIN.md`); and, collapsed, **Full output** (every step as it ran, ANSI colours; an agent step condensed), opened by itself for a job from before the `steps/` files |
| **Layers** (`3`) | a **Start work** panel (below), then each layer's workflows as static graphs with input / trigger / environment / review, runs, pass rate, average time and cost per workflow, recent runs, and the layer's plain-language explanation when an `EXPLAIN.md` exists. After the declared workflows comes everything else the layer can run, from the runtime's registry: the group **Kit workflows** (`integrate`, one `promote-FROM-TO` per declared promotion or `promote` when the layer has no stages, `improve`, `decider`), each card with a `kit` mark, what it is for, the input it takes, how it is started, its setting, its steps (a graph when the registry has a definition) and its runs; an entry that is off (its setting says so, or its script is missing) is dimmed, marked `off`, with what turns it on. Then **Commands (single-command runs)** with the runs recorded as `command:NAME`, and **Suite commands** (the codebase commands `script` steps run) | A layer's page opens with its **Testing** panel (see "Testing depth and batches")

The **Start work** panel (on the Layers view; a layer chooser when several are watched) takes a
request, any workflow of the layer (adopted ones included) or a single command or hop (one recorded
step in the layer's normal environment that never ships), picked from a list or typed as `/NAME …`
with completion (arrow keys, Enter, Esc; `/command:NAME` when a workflow has the same name). It says
in one line what the choice does and whether anything ships without a person, takes an optional
class, tier and model ids, a **testing** depth for a workflow (layer default, light, standard or heavy,
each with its typical minutes; passed to the launcher as `--testing`), and, when the layer has a forge, can log the request as an issue ("start
now", or "let the trigger start it" for a workflow a cron or webhook trigger starts from issues). A
workflow that merges or deploys without a person, and the same request again within five minutes,
open a confirmation dialog. Then the page opens the new run. When starting is off, the panel says why
(`--read-only`, `--no-start`, the container, or a layer generated before this feature: "run `al gen`").
The list has a third group, **Kit workflows**, and the `/NAME` menu lists them marked `kit`. A kit
workflow takes no prompt: choosing `integrate` replaces the prompt box with a picker of the handed-over
runs it applies to (id, title, target branch), or with the reason when there is none (switched off in
`templates/integrate.json`, `adws/integrate.py` missing, or no run handed over because of a merge
conflict or a moved target); choosing a run shows the same preview and confirmation as **Resolve with an
agent** on the run's page, including the runtime's refusal of a run the picker listed (a protected path,
a changed branch). Choosing a promotion shows its two stages with **Promote** and **Dry run**. An entry a
person does not start from the page (`improve`, `decider`, anything that is off) is listed disabled,
with the reason. A run of a kit workflow carries a `kit` mark beside its name on the run page and a link
to what it acts on: the run it integrates, the promotion's stages, or the command of a single-command run.

**Guided generation (`#/generate`, `#/generate/<key>` with the repository chosen)** leads through create
or adopt in four steps: the repository (those with a layer are disabled, "already has a layer"; for one
with an agent setup a checkbox "adopt the existing agent setup", checked by default), the placement (the
picker of the al panel, read from `GET /api/repo`; Next is disabled until a usable one is selected), the
options (the detected choices, each with its explanation, fact and detected value; for adopt also the
dry run's diff) and a review (repository, action, placement and path, every choice with the changed ones
marked, "It runs", "It writes") with **Start**. It adds no endpoint: the options step sends the
`POST /api/action` preview of the buttons and Start its confirmation, with the same `placement`, so
every guard below applies unchanged. Back keeps the answers and the preview; another repository, action
or placement asks for a new preview; a refusal (for example "changed since the preview") is shown with
**detect again**. When actions are off (`--read-only`, `--no-actions`, no workspace, no al tool) the
first step says why and there is no Next.

Every run is in one bucket (`attention()` in dashboard.py, recomputed when a layer's runs change and
every 20 ticks):

| Bucket | Runs | Row shows |
|---|---|---|
| **Needs you** | a waiting run; a handed-over run whose work is not on its target branch (or only partly: "partly landed: N paths differ (…)"); a failed or died run nothing replaces and nobody settled | the reason (the handover reasons, "waiting at STEP", "STEP: first line of the error") and the next action ("review PR #N", "merge BRANCH into TARGET, or `al settle`", with "or `al integrate`" when the run can be integrated, the resume command, "Start again"), which ends ", or Give to an agent" with a **Give to an agent** button when the layer has `adws/assign.py`; a run whose integration failed or waits says so ("integration ID did not land: WHY", "integration ID waits for approval": approving is then the one next action); a run given to an agent twice that stopped for the same reason says so first ("given to an agent twice, stopped for the same reason: …") |
| **Running** | running and unknown runs; a handed-over run an integration run is bringing onto its target ("being integrated by ID") | the current step |
| **Landed** | a handed-over run whose work is on its target branch (see Landing below); a run an integration landed (`settlement.json` with `via: integrate`) | "landed in SHA (ancestor / forge / patch-id / paths), DATE"; "landed by ID (integration)", a link to the integration run on the run's page |
| **Settled** | a run a person settled (`al settle`, or the run page's form); a handed-over run a later landed run integrated ("superseded by B"); a failure a later run replaces ("replaced by NEW (status)") | what settled it: "superseded by B", "replaced by NEW", "AS by WHO: note" |
| done | passed runs | (only under All) |

Failures of one layer that stopped at the same step with the same reason (the first line of the error
with ids, numbers, paths and times dropped), each ending within 30 minutes of the one before, show in
Needs you as **one group row**: "5 runs: preflight, same reason", with **show runs** and **Start all
again**, which asks once and then starts each run again in turn (`POST /api/start {again}`), listing
each new run above the table. A run page shows "replaces OLD", "replaced by NEW", "superseded by B" and
the landing result in its Status block, a **Start again** button on a failed or died run nothing
replaces, and, on a finished run nobody settled, a **Settle this run** card: landed, superseded or
abandoned, an optional run or commit (`by`), a note, and your name (kept in the browser like the
promotion card's).

**Give to an agent.** Every run under Needs you has a **Give to an agent** button: in the Next column
of its row, and above the fields of its Settle card (address `…?assign=1`). It opens a card that says
what will happen, from the layer's own `adws/assign.py preview` (the same plan `al assign LAYER RUN
--preview` prints; `assign.plan` in the layer's runtime is the one function that computes it), and
nothing starts until you confirm. The plan follows from why the run stopped:

| Why it stopped | What starts |
|---|---|
| its review passed, the merge conflicts or the target moved | an integration run (what "Resolve with an agent" started): the branch onto the target, an agent for the conflicts, the checks, the review, the gate |
| its review rejected it, with blockers | an integration run that also addresses the blockers with the review's patch round, runs the checks again and reviews **once**, with no further patch round; "bring it onto TARGET first" when the target moved |
| its review rejected it only because proof is missing | the proof that exists is collected (or the plan's proof commands are run) and the review is asked again; no code changes |
| something only a person may decide (a `REQUIRES HUMAN` blocker, a protected path, a layer change, a spending limit, a branch a person changed, a pull request that waits) | nothing: the card names the decision |
| a check still failing after its retries | the run resumes from that check, its fixing agent on the heavy tier |
| a login failure, a time limit, a process that died, a waiting step | the run resumes from the step that stopped, unchanged |
| it failed before changing anything | the same request starts again; the new run replaces it |

The card shows why the run stopped, the plan in one line and its numbered steps, for an integration
the branch and its tip, the target and its head, the conflicted files (generated ones marked; "applies
cleanly" when there are none), the steps of the run and "waits for your approval before it ships" when
the layer asks for that, the review's blockers, the earlier attempts (when, by whom, which run, how it
ended) and "given to an agent N of 2 times". You can add one sentence for the agent (at most 500
characters); **Start** starts it and goes to the run that does it. A decision that is a person's to
approve (a `REQUIRES HUMAN` blocker, a protected path) has a name field and **I approve this,
continue**: the approval of exactly that item is recorded in `agents/<id>/assign.json`, nothing starts,
and the card shows the plan that remains, with the approval quoted to the agent and to the review. A
decision that is a person's only (a branch a person changed, a layer change, a spending limit) has no
such button. After two starts by a person the card says so and offers "give it once more (override)";
a run that stopped twice for the same reason says that it stays with you. Nothing here ships a
rejected review: every plan ends in the checks, the review and the unchanged gate, or with a person.
The button is shown disabled, with "run `al gen` to add adws/assign.py" as its title, in a layer
generated before `al assign`. A run under Needs you that waits only for a person's approval (its
summary's `approval`: a plan's `approval:` need, protected paths the evidence step found, a stand-in
proposal, a "needs a person" item whose plan can start once approved, an integration's approve step)
has **Approve & resume** in its row (`button.approve-resume`), with what it approves beside it. The
chip **waiting for approval (N)** (`.chip.appr`) keeps only these runs, and **Approve & resume all N**
(`button.approve-resume-all`, one layer at a time) takes every shown one. The run's page starts with a
banner (`[data-role='approve-banner']`, "This run waits for your approval") that lists the items, the
step the run resumes from and what happens after, with the same button. The button opens a
confirmation (`[data-role='approve-resume-dialog']`) that lists every item in full with its paths; it
needs your name (kept in the browser as `al.approve.who`) and takes an optional one-line note, then
sends `POST /api/approve-resume` (below). What it answered is shown in the row or the banner and
above the table. Nothing is offered for a stop only the layer author or the operator clears, nor for
a failing check, a limit or a promotion. In the Needs-you view, **Give all N to agents** (one layer at a time: it
is disabled while runs of several layers are shown; N counts the shown rows, a failure group as one
item handed over as its newest run, its other runs left under Needs you) opens a dialog with the plan
of each of those items, and only those, in the order they would start (resumes and restarts first, then one integration at a time; what stays with a person
last, as "skipped: why") and **Start N**; what started is listed above the table. Beside it is the
switch **Automatic** (`Automatic: off` / `Automatic: on`, one layer at a time). While it is on, the
dashboard does for every run of the layer under Needs you what a person would do with **Give to an
agent**: the stand-in's loop starts the run's plan (resume from the failed check, resume unchanged,
start the request again, an integration run, the review's blockers or missing proof), without a
click. It covers the runs that wait already (switching it on asks first and names how many) and every
run that stops later; one run per look of the loop (5 s), oldest stop first, and each run once: the
runtime counts the stand-in's one attempt over a run and every run it replaces, so a request that was
started again and failed again is not started a third time. It never takes a run a person gave to an
agent or one started with `decider: off`. What only a person may decide (a `REQUIRES HUMAN` blocker,
a protected path, a spending limit, a pull request that waits, a branch a person changed) stays under
Needs you, and a line under the switch says why: `f0000012: stays with you: needs-a-person: …`; a run
that was given reads `f0000013 → resumed in place (check)` or `f0000014 → 5c1d2e3f (start-again)`
(the lines are kept in memory, the last 30 of this dashboard process; the durable record is the run's
`assign.json`). Nothing ships without the checks, the review and the unchanged gate. The switch is the
layer's local setting `agents/settings/assign.json`, never committed; the crumbs say `automatic on`.
It follows the stand-in loop's flags: nothing is given with `--read-only`, `--no-stand-in`,
`--no-start` or in the container (the page then says "it does nothing in this dashboard"), and
`--stand-in-only` carries it out without the page. In a layer whose model stand-in is `suggest` or
`auto` the rules go first: the stand-in is asked about a run only after the switch tried it and it
stayed. A run's Status
block shows `given to an agent: 1 of 2; last: landed` with a link to that run. The original's page
links to its integration run (`integration` in its Status block, the badge "landed by ID
(integration)"), and the integration run's page links back (`integrates`). An integration that waits
at its `approve` step shows an **Approve and ship** card with a name field. Beside the button, **Check if it has landed** and **Find what
replaced it** ask `GET /api/settle/evidence` and list what was compared ("tip 4b8f959 of BRANCH: the
same change (equal patch-id) is a commit of main", "run c0000002 (passed, landed) names run f0000007 in
its request", or what differs); when something was found they fill the Settle form in and the button
reads "Settle as landed by 4b3cb6e". They only read: the write is the Settle button's `POST /api/settle`.
When the layer's stand-in decider looked at a stopped run (`al decider`, `agents/<id>/decision.md`),
the run's outcome shows a **Decision** block after "Why this stopped": a badge (`auto`, `suggested`,
`answered: go` …, `not used`, `failed`), the decider's one-line summary, its mode, action, the step it
resumes from, its instructions, the status, the last answer (kind, channel, time, text), why it was
not used or failed, and a link to `decision.md`. Every value is text, never markup, and the answer code
is not shown. The block is read-only, like the rest of the run page: answers are given with
`al answer LAYER RUN go|alter|explain|reject`, by replying to the notification e-mail, or with the
webhook's `decision` payload.

A run whose plan ends with a Proof plan table has a **Proof plan** card on its run page
(`[data-role='proof-plan']`). It has a row per criterion with its state (`planned`, `proven`,
`files-missing`, …) from the moment the plan exists, before anything is built. For a row proven by a
check pass the run carried forward as not affected, the Evidence cell shows
"carried from <tree> (only <files> changed)", with the first 8 characters of the tree the pass ran on. When the run stopped
at `prove` or `evidence`, a stop line says why and names the party who can clear it (layer author,
approver, person), with the fix (the exact declaration a layer author adds, or the question for an
approver). Only for a failed run stopped for an **approver** is there an **Approve** button
("Approve vocabulary"): it opens the Approve & resume confirmation (below) when the run has an
`approval`, and in a layer generated before that it sends `POST /api/approve` (below); the run resumes
from the step that stopped. Below the table come the review rounds, each with what it resolved, what is still open and
what is new, why the rounds ended, and the debt filed to the task list. The Start work panel has an
**approve** field for workflows (comma-separated: `vocabulary, edit:PATH, dependency:NAME`), sent as
`approvals` with the start request: pre-approvals for that run, like `al run --approve`. On the
Layers view, a layer with runs that have a Proof plan shows a **Reviews** line: review rounds per run,
repeated blockers, idle hours, runs stopped early, and the declaration to add first (the numbers of
`al kpis`' Reviews and proof). All of it is shown as text, never markup. Screenshots: `proof-table.png`,
`stopped-before-build.png`, `converged-review.png`.

A run whose `script` step exited with a code the step declares as waiting (`#:waiting-exit (75)`)
ended **waiting**: it is neither failed nor passed, its worktree is kept, and nobody is notified.
The run list, its status filter, the graph node and the timeline show it as `waiting` (⏸), and the
run's header shows the step it waits at and the command that resumes it
(`uv run adws/adw_<workflow>.py --resume --adw-id <id>`).

A step whose agent CLI did not exit after its final result (an `agent.leftovers` event: the runtime
stopped it and the commands it left running) reads `leftover commands stopped` on its graph node,
whose title says "the agent finished; its leftover commands were stopped"; the arrow out of a
running step into a pending one is dashed in the running colour and labelled `waiting`, so it does
not read like the untaken arrow into a skipped step.

A check step whose command was not run because no changed path touches what it covers has the
status **not-affected** (∅, dashed and dimmed like `skipped`, never green): the graph node reads
`not affected`, the timeline shows the words instead of a bar, and the step panel says why and
lists the compared paths. A check whose earlier pass on an identical tree was reused keeps its
status and reads `reused` on its node. The run's outcome has a block **What the checks proved**:
one row per check and place (its check step or a review round, then the gate) with the result in
the runtime's words (`passed`, `failed (exit N)`, `not affected (WHY; N paths compared)`,
`reused from STEP, same tree HASH`) and the evidence (the tree hash and log, or the compared
paths). It is built from the run's ledger (`check_results` in `state.json`) and absent for runs
from before it; everything in it is shown as text.

A review whose final verdict failed is shown as **rejected** (⊗, amber), with each round's verdicts
under the review step. Its run went on to the ship step, where the gate handed the work to a person,
so the run is **handed-over** (↪, amber), with the reasons (`review-rejected`, the gate's fallback
reasons) in the run's header. A run whose gate fell back is handed-over too. Records written before
these statuses existed (review `passed` with a failed verdict, run `passed`) are read the same way
when the files are read; the files are never rewritten. Pass rates count handed-over runs as
finished, not passed.

A run whose environment prepares (`(environment worktree #:prepare (…))`, or a podman option that reuses a
dev container: its postCreateCommand) starts with a **setup** node `prepare` on the graph and the timeline;
its log is the one in `state.scripts["prepare"]` (`agents/<id>/scripts/prepare.<n>.log`), shown like a script step's
(screenshots: `run-setup-step.png`, `run-setup-step-log.png`).

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
agents/settings/models.json (the Models panel), agents/settings/testing.json (the Testing panel),
agents/settings/assign.json (the Automatic switch of Needs you),
agents/<id>/testing-request.json ("Full before it ships") and, for the actions, its jobs directory; starting
work is done by the layer's own `adws/dispatch.py --start`, which writes the run's files; the
actions run `al`, which writes what that command writes. `--read-only` turns all of it off, and
`--no-start` and `--no-actions` each turn off their own.

| File | Used for |
|---|---|
| `agentic-layer.json` | the workflows (graphs; a `script` step is its own node type), slot limits, spending limits (`budget`: shown on the Now view with what today's runs cost, and against each run's cost), review options, declared commands |
| `agents/<id>/events.jsonl` | when present (runs of this runtime version and later): exact start and end of every agent call (with the agent, its kind and model), check attempt (exit code, re-check or not, skipped tests and their reasons, the environment it ran `#:in`), a check that was not run (`check.decision`: not affected or reused, why, the compared paths, the tree), gate and deploy command, slot wait and the container; the check running now; subscription usage reports, limit stops and waits (`limit.status`, `limit.stop`, `limit.wait`). See "Run events" in `docs/DESIGN.md`. Older runs have no such file and are read as below, from file times |
| `agents/<id>/state.json` | status, class, request, step records (status, start, end, detail, the agent, model and model tier used), every agent call's tier and why it was chosen (`agent_calls`), cost and spending limits (`cost`, `budget`: a run a limit stopped carries a distinct "budget stop" mark; `cost.billing` for "API-equivalent"), what the run recorded about subscription usage (`usage`: billing mode, the limit that stopped it, waits), script steps (`scripts`), checks (with `outcome` and `why` when the runtime recorded them), what the checks proved (`check_results`), review rounds, ship and gate, fan-out candidates and a candidate's parent (`fanout_parent`), artefacts. Liveness: `pid`, `host` and `heartbeat` (refreshed every ~5 s). A run whose state says running but whose heartbeat is older than 20 s while its process is gone is shown as **died**, with when it was last seen (other host, or inside the container where pids are not visible: after 5 min without a heartbeat). Older files without newer keys are fine. A run without `state.json` is shown as `unknown` from its `run.log`; `remote` (a run placed on a host): the host badge, and while its `phase` is `remote` the run is judged alive by its heartbeat alone (the local pid is a proxy; died after 300 s without one), never by a local pid; the status `lost` |
| `agents/subscription-usage.json` | the Now view's "Subscription usage" panel: the billing mode and the latest report per usage window, written by the runtime after each agent call. Read-only, polled like the run files; a change is pushed to the page. Missing = "no data yet"; anything unreadable or of the wrong type is ignored, and its content is shown as text |
| `agents/hosts.json` | the Now view's "Hosts" panel, for a layer whose IR has a `placement`: the scheduler's last survey (`at`, per host its runs, limit, CPUs, load, memory, disk, image and logins, or `silent` with the reason; `pending` starts), merged with the declared hosts in their order and the layer's running runs per host (a run counts on its host while `state.json` `remote.phase` is `remote`, else at home). Read-only, polled like the run files; a change is pushed to the page. Missing or without a readable `at` = "no survey yet"; a host the survey does not name = "not surveyed yet"; every value is shown as text |
| `agents/settings/models.json` | the Now view's "Models" panel: the layer's local model setting (`{"models": {TIER: MODEL_ID}, "version": 1}`, never committed), the same file `al models LAYER --set TIER=ID` writes. With the IR's agent tiers it gives each tier's model and its source (`local setting`, `layer`, `built-in default`). Polled like the usage file; a change is pushed to the page. An unreadable file is shown as an error and not used. Also the one file the dashboard writes (see Safety) |
| `agents/settings/testing.json` | the Testing panel's level: the layer's local testing setting (`{"version": 1, "level": "light\|standard\|heavy"}`, never committed), the file `al testing LAYER --set LEVEL` writes; read and written by `read_testing_setting` / `write_testing_setting`, mirrors of the runtime's `testing.read_setting` / `write_setting`. An unreadable file is shown as an error and not used |
| `agents/settings/assign.json` | the Automatic switch of the Needs-you view: the dashboard's own local setting (`{"version": 1, "auto": true}`, never committed, read by no runtime module), written atomically by `POST /api/assign {action: "auto", on: true}` and removed when it is switched off (`read_assign_auto` / `write_assign_auto`). A file of any other shape is shown as an error under the switch and nothing is given |
| `agents/testing/batches.jsonl`, `misses.jsonl`, `suites.json` | written by the layer's `adw_modules/batch.py` and `testing.py`: one line per batch (`{id, branch, stage, head, since, status: green\|red, depth, checks, delivered, at, culprit, fix_run, source}`), one per miss a batch bisected to a run (`{batch, run, cmd, strategies, tests, files, at}`), and each command's full suite (`{CMD: {seconds, tests, commit, at}}`): the Testing panel's batch state, miss rates and typical minutes, and a culprit's "broke the batch". Polled like the settings; the ledger is part of every run's signature, so a new culprit is marked at once |
| `state.json` `testing`, `checks[NAME].testing`, `check_results[].depth` | a run's depth (`{requested, depth, source, escalation}`), each check's accounting (`{depth, asked, why, selected, units, strategies, ran, total, seconds, full_seconds}`, shown in the runtime's words: "targeted: ran 14 of 812 tests (changed-tests 3, imports 9, coverage 2): 41 s instead of about 9 min") and the depth of each ledger entry (the Depth column of "What the checks proved"). Runs from before have none of it and read as before |
| `agents/<id>/testing-request.json` | "Full before it ships" (`{"depth": "full", "by", "at"}`): written by `POST /api/run-testing` or `al testing LAYER --full-before-ship RUN`; the run's gate then runs the full suites |
| `adws/verify_batch.py` | "Run the full suites now": its presence enables the button; `verify_batch.py run --detach --json [--stage S]` is run (see Safety) |
| `agents/<id>/run.log` | without events: container name and removal, slot waits (`[slots] … waiting for a free runs slot`), gate re-run exit codes. Newer runtimes start each line with its UTC time, which is stripped before matching |
| `agents/<id>/<step>/{prompt.md,raw.jsonl,output.txt,stderr.log}` | one directory per agent call (the transcript's init event also says whether the call ran on a subscription or an API key, so older runs are labelled too). `<check>-resolve-<n>` belongs to that check, `review-<n>`, `review-<round>-<n>`, `patch-<n>`, `implement-<n>` to the review step. Without events: start = `prompt.md` mtime, end = `output.txt` mtime |
| `agents/<id>/scripts/<name>.<n>.log` | the output of a `script` step, shown when its node is opened |
| `agents/<id>/checks/<name>.<attempt>.log`, `<name>.not-affected.log`, `gate-<name>.log` | ANSI logs (for a check that was not run: the decision, as text), followed live while the check runs (newer runtimes write a check's output to its log as it comes). Without events: attempts end at the log's mtime and start where whatever ran before them ended, since the runtime is sequential; a log only appears when its command finished |
| `agents/<id>/PR.md` | the "Why this stopped" and "What the agent asks" sections of a handoff; also a document of the run |
| `state.json` `plan`, `outputs`, `review.rounds[].patch.patch_plan`, `artefacts` | the run's **Documents**: recorded paths (absolute on the host or in a container, or layer-relative) mapped to the layer's `specs/`, `agents/<id>/`, `artefacts/<id>/`; with `PR.md`, other text files in `agents/<id>/` and `review-*/output.txt` |
| `agents/.slots/<pool>.<n>.lock`, `agents/.ports.json` | which slots are held (from `/proc/locks`, read-only) and by which run; leased ports and whether their owner is alive |
| `artefacts/<id>/manifest.json` | artefacts when `state.json` has none |
| `commands/<name>.md`, `hops/<name>.md` | the `argument-hint` of their front matter, shown next to `/name` in the Start work panel (regular files inside those directories only) |
| `adws/dispatch.py`, `adws/adw_modules/launch.py` | starting work: their presence enables the Start work panel; `dispatch.py --start --json` is run (see Safety) |
| `adws/adw_modules/registry.py` | what the layer can run (its workflows, the kit's, its commands): loaded by path into the dashboard's process (standard library only, no side effects), the layer's own copy first, else the copy beside the dashboard (`../runtime/adw_modules/` in the kit checkout, `../adw_modules/` in a generated layer). The module is cached by path and mtime, the built registry by the signature of `agentic-layer.json`, `templates/integrate.json`, `agents/settings/decider.json` and `adws/`. A copy that does not load falls back to the next; with none (the dashboard's container and a layer generated before the registry) the declared workflows and commands are listed with the line "generated before the registry: run al gen NAME" |
| `templates/integrate.json`, `agents/settings/decider.json` | whether `integrate` and the stand-in decider are on (read by the registry, with the runtime's rules: a missing file is the default, a broken one is a problem line, never an error page) |
| `agents/starts.json` | written by the layer's launcher, not read by the page: the request keys of the last two days (`{key: {run_id, target, prompt_sha, at, issue}}`) behind the double-start guard; `agents/.starts.lock` serialises starts |
| `agents/<id>/request.md`, `agents/<id>/launch.log` | a started run's prompt (mode 0600) and the output of its detached process until the run's own `run.log` takes over; both are documents of the run. "Start again" rebuilds its request from `request.md` (else `state.prompt`), the run's workflow or command, its fixed class and its tier |
| `agents/<id>/settlement.json` | a person's settlement of a finished run (`{"as": landed\|superseded\|abandoned, "by", "note", "who", "at", "via": cli\|dashboard}`, the record before it under `"previous"`, one level; `"pruned"` after `al runs … prune`); written by `al settle` and `POST /api/settle`, atomically, mode 0600; also part of a run's signature, so a new one moves the run at once |
| `agents/<id>/integration.json` | the integration run of a handed-over run (`{run, status: queued\|running\|waiting\|landed\|failed, why, commit, target, by, at, previous}`), written by the layer's runtime (`adw_modules/integrate.py`), never by the dashboard; part of a run's signature. A landed integration also writes the run's `settlement.json` with `"as": "landed"`, `"by": <integration run>`, `"via": "integrate"` |
| `state.json` `integration`, `definition.integration` | an integration run: the run it integrates (`integrates` in its summary), its conflicted files and report; its graph is drawn from `definition` (workflow `integrate`, steps of kind `integrate`: prepare, resolve, approve) |
| `adws/integrate.py`, `templates/integrate.json` | the integration of a handed-over run: the script's presence and the settings (`enabled`, `approval`) decide whether a row's next action names `al integrate` (`integrate` in a run's summary); `integrate.py preview RUN --json` is run for `GET /api/integrate/preview`, `run RUN --detach --json --by dashboard` and `approve RUN --who NAME --detach --json` for the writes (see Safety) |
| `adws/assign.py` | "Give to an agent": its presence enables the button (`assign.script` in a run's summary); `assign.py preview RUN --json` is run for the plan, `give RUN --detach --by dashboard [--note TEXT] [--override] --json`, `approve RUN ITEM --who NAME --via dashboard --json`, and `all RUN … --preview --json` / `all RUN … --by dashboard --json` (the runs under Needs you) for the writes (see Safety) |
| `agents/<id>/approvable.json` | what a stopped run waits for a person's approval of (`{version, run, basis, waiting, kind: proof\|protected-paths\|gate\|proposal\|decision\|integration, items: [{id, kind, text, what, paths, decisions}], from_step, after, why, token, at}`), written by the layer's runtime (`adw_modules/approvable.py`) through `adws/dispatch.py --approvable`, never by the dashboard; part of a run's signature. `read_approvable` gives a row its `approval` (`{kind, items: [{id, kind, text, paths}], from_step, after, token}`, texts shortened and cut) only when the record says `waiting`, has items and a token, and its `basis` equals `approval_basis` of the run's files now (status, end, the counts of approvals and attempts, the decision's and the integration's status: the same string as the runtime's `approvable.basis`). Every 5 seconds the dashboard asks the launcher, once per run and basis and at most 20 runs per layer and look, about the Needs-you runs without a fitting record; never with `--read-only`, `--no-start`, or in a container without `--allow-start-in-container` |
| `agents/<id>/assign.json` | how often a run was given to an agent and what a person approved (`{version, attempts: [{n, at, by: cli\|dashboard\|decider, who, kind, note, run, reason, outcome: started\|landed\|passed\|stopped, after, why}], approvals: [{id, kind, text, path, by, at, via}]}`), written by the layer's runtime (`adw_modules/assign.py`), never by the dashboard; part of a run's signature. `read_assign` mirrors the runtime's reading: `clicks` (attempts by `cli` or `dashboard`), `left` (of 2), `same_reason` (the last two stopped attempts ended for the same reason). An attempt the file still calls `started` is shown with what `integration.json` or the run's status already says |
| `artefacts/<id>/manifest.json` entries of type `left-out` | `left_out` of the run detail: per artefact id, how many matching files were not handed over (`left_out` of `matched`), shown on the Artefacts tab |
| `state.json` `replaces`, `branch`, `ship.commit`, `ship.pr`, `delivery.branch` | what landing and settlement need: the run a run starts again (a run whose `replaces` names another makes that one "replaced by"), the run's branch and shipped commit, its pull request, and its target branch (`delivery.branch` with stages, else `codebase.defaultBranch`, else `main`) |
| the application repository (`codebase.root`, `AL_APP_ROOT` when set) | **Landing**, read-only: whether a handed-over run's tip (its branch, else `ship.commit`) is on the target branch's heads (`refs/heads/<target>` and every `refs/remotes/*/<target>`, read from the ref files): (1) `merge-base --is-ancestor` (landed in the oldest commit of `rev-list --ancestry-path`), (2) the forge says the pull request is merged, (3) the stable `patch-id` of the branch's change equals one of the last 500 target commits' (a squash merge), (4) every path the branch changed has the same blob on the target (an integration; some only: "partly"). git through the same hardened `safe_git` as the workspace scan, 10 s per call, never a fetch, never a write. Cached per (repository, run tip, target heads), so a new target commit recomputes; at most 8 uncached checks per tick (the others show "checking"). A later landed run whose prompt names a run's branch or id, or whose tip holds the run's tip, supersedes it |
| `adws/dispatch.py --pr-state N --json` | the forge's answer for a run with a pull request whose local result is open: the layer's own runtime asks its forge read-only (`pr_state`, so credentials stay with the layer), kept 10 minutes per run and target heads; not asked for forge `none` or in a container |
| `state.json` `definition` | a single-command run's one-step workflow (`command:<name>`), which the compiled layer does not declare: the run's graph is drawn from it; likewise a promotion run's workflow (`promote-<from>-<to>`, steps of kind `promote`) |
| `state.json` `promotion`, `promotions/<id>/acceptance.jsonl` | the **promotion card** of a promotion run (below); the decisions are read through the same safe-path rule as every served file |
| `agents/promotions/<from>-<to>.json` | the open promotions: the layer page's Promote / Continue buttons, and whether a card still takes decisions |
| `state.json` `delivery_request`, `agents/requests/<from>-<to>.json` | the **delivery request** a hands-off cycle opened, updated or adopted (`delivery_request.py`): a row on the run page (link to the pull request, or the layer-relative path of `agents/requests/<from>-<to>.md`, with its state), and the same line on each promotion of the layer page and on the promotion card; with a forge the state is asked again through the layer's `adws/dispatch.py --pr-state` and kept `PR_STATE_SECONDS` |
| `adws/promote.py` | promoting from the page: its presence enables the buttons; it is run for every promotion write (see Safety) |
| workspace repositories | the Repos view, read-only: directories below each workspace folder (depth `--workspace-depth`; symlinked entries, hidden and pruned directories and a sibling `NAME.al` skipped; at most 500) that hold `.git`. Per repository `git ls-files -z` (at most 20 000 paths; a path whose real path leaves the repository is dropped, so a symlinked rule file pointing elsewhere is never read), given to `al scout`'s detection (`tools/al_adopt.py`, `tools/al_suite.py`, which read files and run nothing), `symbolic-ref`, `status --porcelain`, `log -1`. git runs with `core.fsmonitor=false`, `core.untrackedCache=false`, `core.hooksPath=/dev/null`, `--no-optional-locks` (the index is never rewritten), no prompt, no inherited `GIT_*` variables, and its process group is killed after 5 s. A repository is reread only when its signature changes (stats of `.git/HEAD`, `.git/index`, `.git/logs/HEAD`, its directory, the sibling `NAME.al/agentic.rkt` and its layer's IR); rescanned every 60 s and on **refresh** (at most once per 5 s). A layer of a workspace repository that has `agentic-layer.json` is watched like any other |
| `<jobs-dir>/<id>/job.json`, `output.log`, `docs/`, `steps/` | the Job view: an action's steps (argv, the command line as shown, `stream` (`agent` for the scout and author steps), exit code, times, `reason` of a failed step), its status and `reason`, the runner's `pid` / `host` / `heartbeat` (as a run's `state.json`), `path` (the folders of the tools found by tools/al_tools.py, then the dashboard's `PATH`: the steps' `PATH`), `preflight` (the `al doctor LAYER --for …` argv run before the first pending step), `retries` (`{"from", "at"}` per retry), changed files; its output; its documents. `<jobs-dir>/locks/` holds one lock file per repository. The second place the dashboard writes, outside every repository and layer. Two dashboards with different `--jobs-dir` do not see each other's locks. `steps/<name>.log` is one step's whole output, stdout and stderr as they arrived (an agent step's raw Claude Code event stream), started afresh when a retry runs the step again, while `output.log` keeps every attempt. `steps/<name>.journal.jsonl` is an agent step's journal, written by the runner as the stream arrives (`AgentJournal`, which wraps the run Step view's transcript normaliser): one event per line with `t`, `kind` and `text`, the kinds `message`, `tool` (`id`, `tool`, `target`, `doing`), `tool.done` (`bytes`, `lines`, `error`, `ms`), `error`, `report` (`decisions`, `left_out`), `result` (`cost`, `turns`, `ms`, `tokens`) and `hidden` (`what`: hook, init, system, rate, task, thinking, meta), every path shortened (the layer and repository as shown, the home directory as `~`); only its one-line form goes into `output.log`. The page reads a step through `/api/job/journal` and `/api/job/log`, never a step file whole |

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

## The stand-in

The dashboard is the control room of the stand-in (`kit/runtime/adw_modules/standin.py`): a strong
model that proposes, or takes, the decisions a run waits on a person for, inside the scope the layer
declares with `(stand-in off|suggest|auto #:may (…) …)`. The dashboard never imports a layer's
runtime; everything below goes through the layer's own `adws/standin.py` (argv list, no shell, the
dashboard's token not passed on), as starts go through `adws/dispatch.py`. A layer generated before
the stand-in has no such script: its panel says "run `al gen`" and the three endpoints answer 409.

- **The panel.** The Now view has a Stand-in block per layer, beside Models: the mode as three
  choices (off: a person reads every hand-over; suggest: it proposes, you answer; auto: it takes the
  decisions ticked below and proposes the others) with where it comes from, one check box per
  decision kind (ticked: it may take it alone; disabled: the layer's `#:may` does not offer it; the
  `#:never` kinds as text), the limits (`at most N actions per run · N per hour`), **Pause** /
  **Continue**, `handled today N · waiting on you N`, and the last 30 lines of its log, newest first.
  The view is the answer of `adws/standin.py status`, kept until `agents/settings/decider.json`, the
  log or the layer changes. A broken setting is shown as an error and the loop does nothing.
- **Suggestions.** A run whose `state.decision` waits for an answer carries `suggestion` in its
  summary (kind, action, summary, instruction, confidence, what the stand-in read). The Needs you
  row shows `proposes: ACTION — SUMMARY`, the chip **with a suggestion (M)** filters to those runs,
  and the run page shows the proposal above the settle card with **Go ahead** (`go`), **Yes, with
  this change** (`alter`, a text field) and **No, I decide** (`reject`; the card closes and the
  normal actions stay). **Ask the stand-in** on a failed, died, handed-over or waiting run without a
  suggestion, and **Ask the stand-in for all N** on the Runs view of one layer, file a suggestion
  whatever the mode and change nothing else.
- **Decisions.** The run page lists every entry of `agents/<id>/decisions.jsonl` in a Decisions card
  (when, `stand-in` or `person`, the badge `automatic`, what, why, what happened next), draws a mark
  per decision on the timeline, shows the stand-in's mode for the run in the Status block
  (`suggest (layer default)` or `auto (--decider)`), and splits the cost (`$1.20 (stand-in $0.08)`).
  A settlement the stand-in wrote has **Undo**. The header's second count on the Runs link is how
  many items the stand-in handled today.
- **The loop.** While the dashboard runs, `Model.tick_standin` looks every 5 s at each layer whose
  mode is suggest or auto and that is not paused, and hands the first new item of its Needs you list
  to one worker thread, which runs `adws/standin.py consider` (one decision at a time; an agent call
  may take minutes). An item is new when the run is not running, stopped after the stand-in was
  switched on, was not started with `decider: off`, and the stand-in has not looked at this stop
  yet; failed or interrupted jobs on the layer's repository count too, and a `retry-job` the
  stand-in decides is carried out here. Older items are left alone: the panel offers **Handle the N
  items already waiting**, which first asks for a suggestion each and lists them, then **Do these M**
  for those inside its scope. The loop is off with `--read-only`, in the podman container and with
  `--no-stand-in` (the panel still shows and switches the mode, and says why the loop is off).
  `--stand-in-only` runs the loop without the page (no HTTP server, until SIGTERM); with `#:systemd #t`
  on the declaration `al gen` writes `systemd/al-<name>-stand-in.service` for it. The same loop and
  worker also carry out the **Automatic** switch of the Needs-you view, whatever the stand-in's mode:
  per look and layer, the oldest run it has not tried is given to an agent with `adws/assign.py give
  RUN --by decider --detach --json` (also off with `--no-start`), before the stand-in is asked.
- **What a person did** on the page (settled, undid, started again, retried a job) is noted in the
  same record for a layer that uses the stand-in, so the stand-in never acts on a run a person
  touched since it stopped.

## Promotions

A layer with stages (`(delivery …)` or `(stage …)`) has promotions (`al promote`; see "Promote a stage" in
`docs/GUIDE.md`). On the dashboard:

- **The layer page** lists each promotion (`sprint → main`, who approves, method, tag) with a **Promote
  FROM → TO** button (**Continue** when one is open) and a **Dry run** button. Both start the layer's own
  `adws/promote.py run FROM TO --detach` and land on the promotion run.
- **A promotion run's page** draws its steps (`open`, `checks`, `pack`, `request`, `approval`, …, `close`;
  node type `promote`) and shows the **promotion card**: each change with its request, its acceptance
  criteria and the proof that covers each (checks with exit codes, pictures and recordings as links, the
  reviewer verdict; a criterion with no passing check and no picture is marked "no proof"), the decisions
  recorded so far, links to the presentation pack (`artefacts/<id>/pack/index.html`), `sign-off.md` and
  `approval.md`. With your name and a comment you can **Accept** or **Refuse** (revert, or revert and start a
  patch run) the selected changes, **Approve all**, and **Continue** the run. A decision is a line in
  `promotions/<id>/acceptance.jsonl`, written by `adws/promote.py decide`; the card shows it at once, the run
  acts on it when it continues.
- **The delivery request** of each stage pair (what is waiting to go one stage further) is shown on the
  layer page's promotions, on the promotion card and, for the run whose cycle made it, as the run page's
  **delivery request** row: the pull request's link or the path of `agents/requests/<from>-<to>.md`, its
  state (`open`, `merged`, `closed`; on a forge refreshed through the layer's runtime) and what the last
  cycle did (`opened`, `updated`, `adopted`, …). It comes from `agents/requests/<from>-<to>.json` and the
  run's `state.json`; al never merges it.
- The run card and the runs list carry a badge: `promote sprint → main · 3 changes · waiting for approval`.
- The pack page is served inline as `text/html` under `Content-Security-Policy: sandbox allow-same-origin;
  default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'`: it may show its own
  pictures and recordings and its inline style, and can run no script at all.

## Voice helper

A layer that declares `(voice kokoro)` (with `#:dashboard-helper`, the default) gets a voice helper panel on
its layer page and on the panel of a repository whose layer is watched. The person speaks (hold to talk, or
always listen with voice-activity detection) or types; the helper answers aloud and in text, and proposes
work as a **card**. How to use it: "Talk to the dashboard" in `docs/GUIDE.md`.

- **Speech service.** Speech runs in a separate uv script, the layer's `adws/voice/serve.py`
  (faster-whisper, Kokoro int8 ONNX with 4 threads, the Silero VAD bundled in faster-whisper), so
  `dashboard.py` stays standard-library. The dashboard starts it on demand per layer
  (`uv run --script adws/voice/serve.py serve --layer L --port 0`, its own session), bound to `127.0.0.1`,
  with a random token in `AL_VOICE_TOKEN` that only the dashboard knows (never on a command line or in the
  page), and proxies to it; it is stopped after 10 idle minutes and when the dashboard stops. The service
  loads models only from the cache `al voice LAYER setup` verified, never downloads, and refuses (and logs)
  every non-loopback connection. Without the models the panel says "voice is not set up" and shows the
  command.
- **The helper's mind** is the layer's own agent, run through `adws/dispatch.py --helper --json` (the
  prompt on stdin): claude-code with every tool disabled (`--tools ""`, `--strict-mcp-config`,
  `--max-turns 1`) in an empty temporary directory, or the mock agent's `mock/helper.json`. It answers with
  one JSON object per turn. Its only tools are a fixed, read-only registry inside the dashboard
  (`HELPER_TOOLS`: `list_runs`, `run_summary`, `run_documents`, `startables`, `repo_state`, `queue`,
  `propose`); it cannot read files, run processes or write. Recognised speech is quoted to it as data.
- **Proposals.** `propose` validates an action (`start`, `retry`, `resume`, `answer`, `stop`, or a `scout`,
  `check` or `explain` job) and creates a card whose summary the server writes. The person's "yes", "no"
  or a change is classified by the server, never by the agent: yes confirms the card, a change supersedes it.
- **Transcript** in `<voice-dir>/<conversation>.jsonl` (mode 0600). Audio stays in memory unless **Keep
  recordings** is ticked; then utterances are written under the layer's `agents/voice/<conversation>/`.

## Testing depth and batches

A layer with `(testing …)` checks each run with the tests its change concerns and proves the whole per
batch (see "Depths, batches and misses" in `docs/DESIGN.md`). On the dashboard:

- **The Testing panel** (the layer page, first card): the three levels as a segmented control, **light**
  (smoke), **standard** (targeted) and **heavy** (full), each with its typical minutes (the median check time
  of the last 20 finished runs at that depth; heavy from the suite timings when no full run is known), the
  current one marked "now", and where it comes from ("from the layer file" or "from the local setting").
  **Apply** writes the local setting (`agents/settings/testing.json`), **Reset** removes it; both apply to
  runs started from then on. A level the layer refuses is disabled with the reason as its title: below full
  while a workflow that merges or deploys without a person lands on the final stage and no batch rhythm is
  declared (the compiler's sentence, from `testing_allowed`, the mirror of `testing.setting_allowed`). A
  layer without `(testing …)` says so: every check runs its full form. Then a table per command: its forms
  (`full`, `targeted`, `smoke`), its targeted runs and misses over the runs delivered in batches that ended
  (the newest 50), the miss rate (per strategy in its title) and a **runs full** pill above
  `#:max-miss-percent`. Last, per stage (or the default branch without stages): the last batch (green or red,
  its head, when, its run), for a red one the culprit run, the fix run and the failing tests, the runs that
  landed since, when the next batch is due, and **Run the full suites now**.
- **The start form** has a **testing** select (layer default, light, standard, heavy).
- **A run's page** says its depth under the request ("Testing: targeted (layer policy) · escalated: core
  path …"), whether a person asked for the full suites before it ships, and while the run runs a **Full
  before it ships** button. "What the checks proved" gains a **Depth** column: a `depth-pill` (full plain,
  targeted dashed, smoke dotted) with the accounting. A run a batch was bisected to carries a red **broke the
  batch** badge (in its header and the runs list) and a banner with the batch run, the failing tests and the
  fix run.

## Safety

- **Writes, guarded** (and the stand-in's three, below). Only GET and HEAD, plus `POST /api/models`, `POST /api/start`,
  `POST /api/settle` and `POST /api/action` (with `POST /api/job/retry`, which belongs to the actions, `POST /api/promotion`, `POST /api/integrate`, `POST /api/assign`, `POST /api/approve` and `POST /api/approve-resume`, which start or resume runs through the layer's own scripts, and the three Testing writes `POST /api/testing`, `POST /api/batch` and `POST /api/run-testing`), `POST /api/decider`, `POST /api/decision` and `POST /api/standin`, the voice helper's routes
  (below) and its one write, `POST /api/voice/approve`; every other method and path is 405 (GET on `/api/models`, `/api/start`, `/api/settle`, `/api/integrate`, `/api/assign`, `/api/approve`, `/api/approve-resume`, `/api/testing`, `/api/batch` or `/api/run-testing` is
  405 too). No endpoint changes a run's records (a settlement and a testing request are files of their own), and only an approved voice proposal can resume, answer or stop a run. All four writes pass one guard
  (`Handler._write_guard`): the token as for every request, but from the `Authorization` header or
  the cookie only (a token in the query string is refused); a loopback `Host` when there is no
  token; an `Origin` header equal to `http://<Host>` (a missing one is refused); the
  `X-AL-Dashboard: 1` header, which a plain HTML form cannot send; `Content-Type:
  application/json`; a body of at most 4 KiB (models, actions) or 64 KiB (start); and no
  `--read-only`.
- **The stand-in's three writes** pass the same guard and take known keys only (400 otherwise); an
  unknown layer is 404 and a layer without `adws/standin.py` 409.
  `POST /api/decider` `{layer, mode: off|suggest|auto}`, `{layer, reset: true}`, `{layer, may: [KIND…]}` or
  `{layer, paused: bool}` changes the layer's local setting `agents/settings/decider.json` through
  `adws/standin.py set` (the file `al decider` writes, byte for byte); a kind the layer's `#:may`
  does not offer is 400; it answers `{ok, standin}` with the new view.
  `POST /api/decision` `{layer, run, answer: go|alter|reject|explain, text}` is a person's answer to a
  suggestion, applied by `adws/standin.py answer` (the entry of `al answer`, a mail reply and the
  webhook): 200 `{status, message}`, 400 for a bad answer, 404 for an unknown run, 409 when nothing
  waits; `go` and `alter` resume or start work, so they are refused with `--no-start`.
  `POST /api/standin` `{layer, run}` or `{layer, runs: [≤ 200 ids]}` asks the stand-in about failed, died,
  handed-over or waiting runs, and `{layer, apply: [≤ 200 ids]}` lets it take the stored suggestions
  inside its scope and limits (403 with `--no-start`); it answers 202 `{queued}` and the results
  arrive over the `run` events. `POST /api/settle` `{layer, run, undo: true}` takes the last
  settlement back (the one before it returns, if any; 404 when there is none).
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
  short subprocess: an argv list, never a shell, the prompt on stdin. Every layer script runs under the
  dashboard's own Python; if that file has gone (a pruned uv cache), it runs under `uv run --quiet
  --script`, then under `python3` from PATH, and without any of them the error names the missing
  interpreter and says to restart the dashboard. The launcher (the layer's
  `launch.start`, the same code as `al run`) checks it all again against the compiled layer, returns
  the run of a request key it has seen (a double click or a retry starts one run), asks for a
  confirmation (409) for a workflow that merges or deploys without a person and for the same request
  within five minutes, refuses at the layer's day limit or usage threshold (429), writes the prompt to
  `agents/<id>/request.md` (it never reaches a command line), and starts the run detached in its own
  session, so stopping or restarting the dashboard does not stop the run. A single-command run has no
  review and no ship step: it cannot ship. "Log as an issue, start now" opens the issue with a
  `<!-- al:claimed run=<id> -->` marker in its body, which the layer's poller and webhook skip;
  "let the trigger start it" opens it with the issue input's labels and starts nothing.
- **Actions.** `POST /api/action {"repo": KEY, "action": NAME}` (and `"confirm": NONCE`, and for `create`
  and `adopt` `"placement": "beside" | "inside"` and `"choices": {KEY: VALUE}`) runs `al` on a scanned
  repository, from this fixed allow-list only (AL = the found tool, APP = the repository's real path,
  LAYER = its layer, else `<folder>/<name>.al` or, for the inside placement, `<repository>/agentic`):
  `{layer, again: RUN, request_key, confirm_duplicate, confirm_ships}` (no target, no prompt) starts
  the failed or died run RUN again: the server rebuilds the request from RUN's own records (its
  workflow, or its command for a single-command run; the full prompt from `request.md`; its class when
  it was fixed; the tier it was started with) and the launcher gets `--replaces RUN`, so the new run
  records `replaces` and RUN reads as "replaced by" it. A run that is not failed or died is 400, one a
  run already replaces 409 (naming it), unless the request key is the one that started that run (a
  double click: the launcher answers with it). "Start again" of an issue-sourced run restarts from the
  recorded prompt, not from the issue.
- **Actions.** `POST /api/action {"repo": KEY, "action": NAME, "placement": P, "confirm": NONCE, "choices": {…},
  "snapshot": ID, "request": TEXT}` (`repo` and `action` always; `confirm` to confirm a preview; `placement`
  for `create` and `adopt` only; `choices` for `create`, `adopt` and `reauthor`; `snapshot` for `restore` and
  `request` for `improve`, each required there and refused anywhere else) runs `al` on a scanned repository, from this fixed allow-list only (AL = the found tool,
  APP = the repository's real path, LAYER = its layer, else `<folder>/<name>.al` for the placement
  `beside`, the default, or `<repository>/agentic` for `inside`):

  | Action | States | Writes | argv |
  |---|---|---|---|
  | `scout` | all | no | `AL scout APP` (stdout = document `LLM.txt`) |
  | `create` | none, setup | yes | preview: `AL create APP --choices --json` (stdout = `choices.json`); then `AL init APP [--in-repo]` (`--in-repo` for the placement `inside`); `AL scout APP --into LAYER [--agent …] --raw`; `AL adopt LAYER --app APP`; `AL author LAYER [--agent …] --raw [--choice KEY=VALUE …]`; `AL gen LAYER`; `AL check LAYER`; `AL result LAYER --json` (stdout = `result.json`) |
  | `adopt` | setup | yes | preview: `AL adopt LAYER --app APP --dry-run --diff` (stdout = `preview.diff.txt`) and `AL create APP --choices --json` (`choices.json`); then the same seven steps as `create` |
  | `gen` | layer | yes | `AL gen LAYER` |
  | `check` | layer | no | `AL check LAYER` |
  | `verify` | layer | yes | `AL verify LAYER --json` (stdout = `verify.json`) |
  | `explain` | layer | no | `AL explain LAYER --md <job>/docs/EXPLAIN.md` |
  | `snapshot` | layer | yes | `AL snapshot LAYER --note 'from the dashboard' --json` (stdout = `snapshot.json`) |
  | `regen` | layer | yes | preview: `AL gen LAYER --list-edits` (stdout = `hand-edits.txt`); then `AL snapshot LAYER --note 'before regen' --json` (`snapshot.json`); `AL gen LAYER --discard-edits` |
  | `reauthor` | layer | yes | preview: `AL create APP --choices --json` (`choices.json`); then `AL snapshot LAYER --note 'before reauthor' --json` (`snapshot.json`); `AL author LAYER --set-aside`; `AL scout APP --into LAYER [--agent …] --raw`; `AL author LAYER [--agent …] --raw [--choice KEY=VALUE …]`; `AL gen LAYER`; `AL check LAYER`; `AL author LAYER --diff-set-aside` (stdout = `definition.diff.txt`) |
  | `improve` | layer | yes | `AL improve LAYER -- REQUEST` |
  | `restore` | all | yes | preview: `AL snapshot LAYER --restore ID --dry-run` (stdout = `restore.txt`); then `AL snapshot LAYER --restore ID` |

  `placement` says where `create` and `adopt` put the layer. One function, `placement_view`, computes
  each placement's path and whether it is usable; `GET /api/repo` lists both under the action's
  `placements` (with `path`, `exists`, `writable`, `layer`, `ok`, `problem` and that placement's command
  lines and written paths), and the same function guards the request, so what the page shows is what
  the server enforces. A `placement` on another action, or a value other than `beside` and `inside`, is
  400 and starts nothing. A placement whose path is already a layer (an `agentic.rkt` or an
  `agentic-layer.json` there), exists and is not a directory, is not writable or, for `inside`, already
  holds tracked files of the repository is 409 with that sentence, before any job. A preview's nonce is
  bound to its placement: a confirmation that names another one is 409 ("the placement changed since the
  preview") and spends the nonce. The answer of a create or adopt preview has
  `"placement": {"name", "path"}`; the job records `placement` and its page shows it.

  `create` and `adopt` run the sequence of `al create` / `al adopt-all` (tools/al_create.py, with
  `--in-repo` for `inside`; a test keeps the two equal for both placements): `adopt` is a clear no-op when the repository has nothing to adopt, so the author sees
  the adopted material when there is some. The start form shows each choice of `choices.json` (forge,
  host, repository, input, trigger, environment, review, delivery preset) with the fact that supports it;
  each is editable, a changed one is marked "changed", and only the changed ones are sent as `choices`.
  They are checked against `CHOICE_RULES` (the keys and values of `CHOICES` in tools/al_author.py; any
  other key, a value that is not a string or not allowed, a host or repository that does not match its
  pattern, or `choices` on another action is 400 and starts nothing), recorded as the job's `choices`, and
  passed to the author step as `--choice KEY=VALUE`, sorted by key. The job view shows the `result`
  step's `result.json` as the result card, inside the passed outcome card: the layer, its forge clause, workflows, what was adopted and
  left behind, the commands that failed or did not run, and what to do next.

  `snapshot`, `regen`, `reauthor` and `improve` change a layer that exists; none of them, nor `restore`,
  removes the layer folder, `agents/` or `adopted/`. `restore` is not a button of its own: it starts from
  a row of the repository's **Snapshots** card or from the outcome of a failed `regen` or `reauthor`, and
  is allowed in every state, because after a failed `reauthor` set the definition aside a beside layer is
  no longer scanned as one: its LAYER is the repository's layer, else the first placement folder (beside,
  then inside) whose records hold ID (`snapshot_layer`). `snapshot` must match `SNAPSHOT_ID` (the id
  pattern of tools/al_snapshot.py; else 400 "snapshot: not a snapshot id"; missing on `restore`, 400
  "restore needs a snapshot id"), and an id that no record of the layer names is 409 ("no snapshot ID of
  NAME's layer"). `request` is stripped and passed as one argv element after `--`, so a request that starts
  with `-` is never an option; missing, empty or only whitespace is 400 ("improve needs a request: say what
  to change"), and so is one longer than `IMPROVE_REQUEST_CHARS` (2000) or holding a newline, a tab or
  another control character. A preview's nonce is bound to the typed value: a confirmation with another
  snapshot or request is 409 ("the request changed since the preview" / "the snapshot changed …") and
  spends the nonce. A failed or interrupted `regen` or `reauthor` whose `snapshot` step passed has, in its
  outcome, `restore: {snapshot, command}` (the id read from its `snapshot.json`), and a `reauthor` that got
  past `set-aside` also `definition_aside: true`; a passed `reauthor` has `diff: "definition.diff.txt"`,
  which the job page shows as a diff under "What changed in agentic.rkt".

  `GET /api/repo` also has `help` (for state `layer`, one line each on when to use gen, regen, reauthor
  and improve; else `[]`) and `snapshots`: the records `LAYER/.work/snapshots/ID.json` read by
  `snapshot_list` (the file name must match `SNAPSHOT_ID` and the JSON name that id; anything else is
  skipped), newest first, at most 50, each `{id, kind, created, note, commit (12), files, restore}` with
  `restore` the command line `al snapshot LAYER --restore ID` as shown (no absolute path). The page shows
  them as the **Snapshots** card, each row with a **restore…** button that opens the restore preview.
  An action view has `param` (`"request"` for improve: the page shows a one-line field and keeps the
  button disabled while it is empty); views are listed for every action except `restore`.

  Before its first step a job runs `AL doctor LAYER --for <its subcommands> [--agent …]` (10 s at most,
  not a step): a missing tool fails the first step with that sentence and skips the rest, so no agent time
  is spent. The steps run with `PATH` = the folders of the tools al_tools finds, then the dashboard's own,
  so the al tool's `uv` shebang works under a unit's fixed `PATH`; when `uv` itself is not found the job is
  written as failed at once and no runner starts. The scout and author steps run with `--raw`, so the runner receives
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
  messages; any other key, a non-string value (except `choices`) or whitespace in a value is 400, an unknown action 400,
  a repository that is not scanned 404. Then: the state must allow the action (409); the repository's
  real path must still be a git repository inside a workspace folder (409, so one replaced by a symlink
  after the scan is refused); `create` and `adopt` are refused when any `agentic.rkt` exists in LAYER or
  in the repository, committed or not (409, "never overwritten" / "has uncommitted changes"); no other job
  may be running on the repository (409). The argv lists are built from the scanned entry only, run
  without a shell (`Popen(argv, shell=False)`), never with a flag from the request. A writing action
  without `confirm` returns a preview (its command lines, the paths it writes, a nonce; for `create` a
  read-only job whose document is `choices.json`, for `adopt` one with the dry run's diff as well); the
  confirmation must echo the nonce, which is
  single use, expires after 10 minutes and is bound to the repository's commit, branch, status and layer
  definition: a change in between is 409 "changed since the preview". A job is run by its own detached
  process (`dashboard.py --run-job DIR`, `start_new_session`), which holds the repository's `flock` for its
  lifetime (a second runner on the same repository records `refused`), refreshes its heartbeat every 5 s,
  kills a step's process group at its timeout, and stops at the first failing step. It survives a
  dashboard restart; one whose runner is gone is rewritten as `interrupted`, never shown as running.
  `--no-actions` (403 "actions are off") and `--read-only` turn actions off; without the al tool they are
  409 and every button says where it looked. No action in `--in-container` or `al dashboard --podman`.
- **The voice routes** (`/api/voice/session`, `service`, `utterance`, `listen`, `turn`, `speak`, `cancel`,
  `recordings`) pass the same guard as the writes, in the same order and with the same messages, except
  the `--read-only` refusal, plus the per-browser `al_voice_session` cookie (HttpOnly, SameSite=Strict);
  `recordings` is refused under `--read-only`. They talk and listen; none of them starts anything. A layer
  without `(voice …)` answers 404.
- **Approval.** `POST /api/voice/approve {layer, proposal, digest}` is the one write of the voice helper. It
  passes the full write guard and the session cookie, and succeeds only for a proposal of the same browser
  session (403 otherwise) that the person confirmed by answering yes (409 "not confirmed: answer yes first"),
  unchanged since (409 "changed after confirmation"), within two minutes of the yes (410), once (409 on a
  second click). `--read-only` refuses every card; `--no-start` (and a container without
  `--allow-start-in-container`) refuses start, retry, resume, answer and stop; `--no-actions` refuses jobs
  (403; the conversation keeps working and the card says why). A start or retry goes through the same code as
  `POST /api/start`, a job through the same code as `POST /api/action`, a resume, answer or stop through the
  layer's own `adws/dispatch.py --resume/--stop --json` (a stop sends SIGTERM to the run's process group on
  this host; the run ends `failed`, "stopped by a person").
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

### `POST /api/settle`

`{layer, run, as: landed|superseded|abandoned, by, note, who}` passes the same guard as every write (a body
of at most 4 KiB; refused with `--read-only`). Only those keys are accepted. `by` is empty, a run of the
layer (8 hexadecimal characters with a `state.json`) or a commit (7 to 40 hexadecimal characters that `git
cat-file -e` finds in the application repository); a note is at most 500 characters (kept as one
paragraph), `who` at most 80 printable characters on one line. An unknown layer or run is 404, a running or
unknown run 409, any other bad field 400. It writes `agents/<id>/settlement.json` (above) and nothing
else, recomputes the layer's buckets and answers `{"status": "settled", "record": …}`. `al settle` writes
the same record with `"via": "cli"`.

### `POST /api/promotion`

`{layer, action: decide|start|continue, from, to, decision, changes, by, comment, then, dry_run}` passes the
same guard as every write (loopback Host or token, the token never from the URL, Origin naming the Host,
`X-AL-Dashboard: 1`, `application/json`, a body of at most 8 KiB, not `--read-only`); `start` and
`continue` are also refused with `--no-start` and in a container without `--allow-start-in-container`.
Only those keys are accepted; `action` and `decision` come from fixed lists, change ids are 8 hexadecimal
characters, `by` is 1–80 printable characters on one line, a comment at most 2000 characters, `then` is
`revert` or `patch`, the stage names are plain names. The layer's own `adws/promote.py` (argv list, no
shell, the dashboard's token not passed on, a 60 s limit) does the rest; a layer without it (no stages, or
generated before promotions) answers 409 "run `al gen`".

### `GET /api/startable`, `GET /api/layer`: what a layer can run

Both are built from the registry (`load_registry`), so they list the same things. `GET
/api/startable?layer=K` answers `{layer, workflows, commands, kit, forge, issue, tiers, classes,
collisions, problems, start}`: `workflows` and `commands` as before; `kit` is one object per kit entry:
`{name, kind, purpose, steps, input: {kind: run|stages|request, …}, start: {by_hand, how, command, why},
ships, setting: {where, key, on, turn_on}|null, off, available, script, page_start: {enabled, why}}`. The
entry whose input is a run (`integrate`) also has `candidates`: the layer's runs in Needs you for which
the page offers the integration, newest first, as `{id, title, target, reasons}`, and `none_why` when the
list is empty; a promotion entry has `open`, `approval`, `approvers` and `method`. A listed candidate
can still be refused by the runtime's preview. `POST /api/start` answers a kit name with 400 and the
entry's sentence (`integrate takes a handed-over run: choose it in the form`), never "no workflow named";
starting stays with `POST /api/integrate` (the preview's nonce) and `POST /api/promotion`.

`GET /api/layer?layer=K` keeps `adws` (the declared workflows) and adds `kit` (each kit entry with
`graph` or `steps`, `stats` and `recent` of the runs whose workflow it is), `commands` (the single-command
entries with their runs), `suite` (`{name, purpose, used_by, why}`) and `problems` (the registry's). A
run summary has `kit` (true for a run of a kit workflow) and `acts_on`: `{kind: "run", id}`,
`{kind: "promotion", from, to}`, `{kind: "command", name}` or null. A run's steps are drawn from the
definition the run recorded when it has one (a kit run records its own), else the declared workflow,
else the registry entry.

### `POST /api/approve`

`{layer, run, what: [APPROVAL, …]}` and nothing else is the Approve button of a run stopped for a
decision only a person may take. It passes the guard of every write (loopback Host or token, the token
never from the URL, Origin naming the Host, `X-AL-Dashboard: 1`, `application/json`, not `--read-only`)
and is refused with `--no-start` and in a container without `--allow-start-in-container` (403). `run`
is 8 hexadecimal characters; `what` holds 1 to 20 approvals, each a word of lower-case letters, digits
and `-`, optionally `:ARG` (`vocabulary`, `edit:PATH`, `dependency:NAME`); anything else is 400, an
unknown layer 404. The layer's own `adws/dispatch.py --grant RUN --who dashboard --json` (argv list, no
shell) then records the approvals (who: `dashboard`) and resumes the run from the step that stopped:
202 `{"status": "resumed", "message", "url": "#/run/K/RUN"}`. A run that is not stopped for an
approver is 409 `{"error", "reason", "refused": true}`; a launcher that failed is 502 with the tail of
its stderr. `al approve` does the same from the command line.

### `POST /api/approve-resume`

Approve & resume of a run that waits only for a person's approval. One run: `{layer, run, items: [ID,
…], token, who, note}`; several of one layer: `{layer, runs: [{run, items, token}, …], who, note}` (1
to 50 runs, each once); nothing else. `items` and `token` are the ids and the token of the run's
`approval` as the page showed it; `who` is 1 to 80 printable characters; `note` is optional, at most
500 printable characters on one line. It passes the guard of every write and is refused with
`--no-start` and in a container without `--allow-start-in-container` (403); a malformed body is 400,
an unknown layer or run 404, a GET 405.

The dashboard first compares with what it shows now: the run must be under Needs you with an
`approval` whose token is the one sent, else 409 `{"error", "reason": "stale"}`; an item that is not
one of the shown ids is 409 with reason `bad-item`. Then the layer's own `adws/dispatch.py
--approve-resume RUN --item … --token T --who W [--note N] --json` (argv list, no shell, without the
dashboard's token) derives what the run waits for again, records every approval where its runtime
reads it (with who, when, `via: dashboard` and the note) and resumes the run with the mechanism that
stop has: 202 `{"status": "resumed", "message", "run_id", "url": "#/run/K/RUN_ID"}`, where `run_id` is
the run to watch (another run when the approval started an integration). A refusal of the runtime is
409 `{"error", "reason", "refused": true}`: `running` (a live process holds the run; nothing is
recorded), `not-waiting` (nothing is offered any more, with why), `stale` (what it derives is not what
was shown), `bad-item`. A launcher that failed is 502 with the tail of its stderr.

Several runs are taken in the order given, each on its own: 202 `{"status": "done", "resumed": N,
"results": [{run, status: resumed | refused, message, reason, run_id, url}]}` when at least one
resumed, else 409 with the same body. `al approve LAYER RUN --all` does the same for one run from
the command line.

### `GET /api/integrate/preview`, `POST /api/integrate`, `GET /api/settle/evidence`

`GET /api/integrate/preview?layer=K&run=ID` is read-only (the host and token checks of every GET): it
runs the layer's own `adws/integrate.py preview ID --json` (argv list, no shell, the dashboard's token
not passed on, a 60 s limit), which checks whether the run can be integrated and does a dry merge that
writes nothing into the application (`git merge-tree --write-tree` with a temporary object directory).
The answer is `{eligible, reason, why, run, branch, tip, target, target_head, clean, conflicts,
generated, approval, ahead, steps, tiers, start: {enabled, why}}` and, when the run is eligible and
this dashboard may start runs, a `nonce` that is valid for 10 minutes for this layer and run. A layer
without the script answers 409 "run `al gen`".

`POST /api/integrate` takes `{layer, run, action: start|approve, confirm, model_set, who}` and nothing
else, behind the guard of every write (loopback Host or token, the token never from the URL, Origin
naming the Host, `X-AL-Dashboard: 1`, `application/json`, at most 4 KiB, not `--read-only`), and is
refused with `--no-start` and in a container without `--allow-start-in-container`. `start` needs the
`nonce` of a preview as `confirm`: unknown, expired, used before or of another layer or run is 409 "the
preview is stale: open it again". `model_set` is empty or one of the layer's tiers (400 otherwise).
The layer's `adws/integrate.py run ID --detach --json --by dashboard` then starts the integration
run: 202 `{"status": "started", "run_id", "original", "url": "#/run/K/NEW"}`, and the two runs are
read again at once, so the original leaves Needs you without waiting for the next tick. A refusal of
the layer's runtime is 409 `{"error": "<reason>: <words>", "reason"}` (the reasons of
`adw_modules/integrate.py`: `review-rejected`, `protected-path`, `branch-changed`, `settled`,
`already-integrating`, …). `approve` takes `who` (1–80 printable characters on one line) and runs
`integrate.py approve ID --who NAME --detach --json`: 202, or 409 `not-waiting`.

`GET /api/settle/evidence?layer=K&run=ID&kind=landed|replaced` is read-only: `{found, as, by, note,
evidence: [lines], why}`. `landed` runs the landed check now (not bounded per tick; the forge is asked
when the layer has one) and also accepts the commit of the run's integration run when it is in the
target's history; `replaced` lists later runs that start it again (`replaces`), name its id or branch
in their request, or have the same source (a task or an issue), and proposes the newest that has
landed. An unknown kind is 400, an unknown layer or run 404. Nothing is written.

### `POST /api/testing`, `POST /api/batch`, `POST /api/run-testing`

All three pass the guard of every write (loopback Host or token, the token never from the URL, Origin
naming the Host, `X-AL-Dashboard: 1`, `application/json`, at most 4 KiB, not `--read-only`) and accept
only the keys named here.

- `POST /api/testing {layer, level: light|standard|heavy}` or `{layer, reset: true}` sets or removes the
  layer's local testing level (`agents/settings/testing.json`, replaced atomically like the model setting).
  Any other level (a depth name included) or key is 400, an unknown layer 404; a layer whose `agents/settings`
  is not writable here, or a level `testing_allowed` refuses, is 409 with the reason. The answer carries the
  panel's new state; the layers are pushed to every page.
- `POST /api/batch {layer, stage?}` ("Run the full suites now") is also refused with `--no-start` and in a
  container without `--allow-start-in-container` (403). `stage` must be a declared stage (400). A layer
  without `adws/verify_batch.py` (generated before batch verification) is 409 "run `al gen`". The layer's
  own `adws/verify_batch.py run --detach --json [--stage S]` (argv list, no shell, the dashboard's token not
  passed on, a 60 s limit) starts the kit workflow `verify-batch`: 202 `{"status": "started", "run_id",
  "url"}`; a refusal of the layer's runtime (a batch of that branch already running, …) is 409
  `{"error": "<reason>: <words>", "reason"}`.
- `POST /api/run-testing {layer, adw_id, depth: "full"}` ("Full before it ships") writes
  `agents/<id>/testing-request.json` `{"depth": "full", "by": "dashboard", "at"}` (mode 0600, atomically)
  for a run that is running: the run's gate then runs the full suites. `depth` other than `full` or a bad
  run id is 400, an unknown layer or run 404, a run that is not running (or died) 409.
- `POST /api/start` accepts `testing` (`light`, `standard`, `heavy`, or empty for the layer's own level; 400
  otherwise) and passes it to the layer's launcher as `--testing`.

### `GET /api/assign/preview`, `GET /api/assign/bulk`, `POST /api/assign`

`GET /api/assign/preview?layer=K&run=ID` (the host and token checks of every GET) runs the layer's own
`adws/assign.py preview ID --json` (argv list, no shell, the dashboard's token not passed on, a 60 s
limit) and answers its plan: `{run, asked, title, status, stop: {status, step, kind, key, why}, kind:
integrate|blockers|proof|person|check|resume|start-again|none, summary, steps, moved, decisions: [{id,
kind, text, path, approvable, approved}], blockers, found, commands, integrate (the answer of the
integration preview, for the kinds that start one), source, from_step, tier, attempts, clicks, left,
same_reason, blocked, blocked_why, note}` with `start: {enabled, why}`, `tiers` and every path
shortened. It starts nothing and changes nothing in the application; the one thing the runtime may
write is the outcome of an earlier attempt that has ended, into `assign.json`. A `nonce`, valid for 10
minutes for this layer and run, is added when this dashboard may start runs and there is something to
confirm: a plan that can start, one only the limit blocks, or a decision a person can approve. A plan
that cannot start (`blocked`: `needs-a-person`, `limit`, `settled`, `not-stopped`, `running`) is still
200; a layer without the script answers 409 "generated before `al assign`: run `al gen NAME`", an
unknown layer or run 404.

`GET /api/assign/bulk?layer=K` runs `adws/assign.py all RUN … --preview --json` with the layer's runs
under Needs you and answers `{layer, plans: [{run, title, kind, summary, skip, why}], startable, start}`
in the order a bulk start gives them, `skip` saying why a run is left out, and one `nonce` for exactly
those runs. `runs=ID,ID` is optional and limits the preview to those runs (the rows the page shows, at
most 200; echoed as `runs`): empty or malformed is 400, an id that is not a stopped run of the layer
under Needs you is 409 naming it, and the `nonce` covers exactly that list.

`POST /api/assign` takes `{layer, run, action: start|approve|bulk, confirm, note, override, item, who,
runs}` and nothing else (`run`, `note`, `override` to start; `run`, `item`, `who` to approve; only
`runs` for `bulk`; any other key, or a key of another action, is 400), or `{layer, action: "auto", on}`
(the Automatic switch, below), behind the guard of every write (loopback
Host or token, the token never from the URL, Origin naming the Host, `X-AL-Dashboard: 1`,
`application/json`, at most 4 KiB, not `--read-only`), and every action is refused with `--no-start` and
in a container without `--allow-start-in-container` (403), as `POST /api/integrate` is. `confirm` is the
`nonce` of a preview: missing, unknown, expired, used before or of another layer or run is 409 "the
preview is stale: open it again", and a refused confirmation spends it.

- `start`: `note` is at most 500 printable characters on one line, `override` true or false (400
  otherwise). The layer's `adws/assign.py give ID --detach --by dashboard [--note TEXT] [--override]
  --json` records the attempt and starts the plan: 202 `{"status": "started", "kind", "run", "run_id",
  "attempt", "url": "#/run/K/RUN_ID"}`, where `run_id` is the run to watch (a new integration run, the
  same run for a resume, the new run of a start-again); the runs are read again at once. A refusal of
  the runtime is 409 `{"error": "<reason>: <words>", "reason"}` (`needs-a-person`, `limit` without the
  override, `settled`, `running`, …, and the reasons of the integration).
- `approve`: `item` is a decision's id, `who` 1–80 printable characters on one line. `assign.py approve
  ID ITEM --who NAME --via dashboard --json` records the approval of exactly that item and starts
  nothing: 200 with the new plan and a new `nonce`; an item the plan does not have, one that is approved
  already or one that is a person's only is 409 `bad-item`.
- `bulk`: `assign.py all RUN … --by dashboard --json` with the runs of the bulk preview: 202 `{"status":
  "started", "started": N, "results": [{run, kind, status: started|skipped|error, run_id, why, url}]}`.
  `runs`, optional, is a list of run ids (400 otherwise) and must equal the list the preview was asked
  for; a different one is 409 "the preview is stale" and spends the nonce.
- `auto`: the body is exactly `{layer, action: "auto", on: true|false}` (no `confirm`: it starts
  nothing itself; anything else is 400 `expected {"layer", "action": "auto", "on": true|false}; nothing
  else`). It writes `agents/settings/assign.json` (`{"version": 1, "auto": true}`, atomically) or
  removes it, and answers 200 `{"ok": true, "assign_auto": {…}}` with the layer's new view; the layers
  are published again. Refused: 403 with `--read-only` (the write guard), 404 for an unknown layer, 409
  "generated before `al assign`: run `al gen NAME`" in a layer without `adws/assign.py`, 409 with the
  reason when nothing may start (`--no-start`, a container without `--allow-start-in-container`) or
  when `agents/settings` is not writable. Switching off also forgets which stops this process tried,
  so a later switch-on looks at every run again (the runtime still gives each run once).

Every layer of the snapshot (and of the `layers` event) carries `assign_auto`: `{available` (the layer
has `adws/assign.py`), `on`, `writable` (the page may switch it), `why` (why not), `loop` (`on`, or
`off: --read-only | in a container | --no-stand-in | --no-start`: whether this process gives the
runs), `error` (a broken setting file: then `on` is false and nothing is given), `log: [{at, run,
status: started|skipped|error, kind, run_id, why}]}` (what the loop did in this process, newest last, at
most 30; no absolute path).

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
`tests/test_hosts_model.py` and `tests/test_browser_hosts.py` cover runs placed on hosts
(`fixtures.build_hosts`: the layer `notes` with a run on `build-1`, a lost run on `build-2`, a run at
home and a survey in `agents/hosts.json`): the host in a run's summary, `lost` as a finished status,
liveness by heartbeat alone, the Hosts panel's data and its push to the page, and the page in Chrome;
with `AL_DASHBOARD_SHOTS=DIR` the browser test saves `hosts-panel.png` and `run-on-host.png` to DIR.
`tests/test_documents.py` covers a run's documents: the list (plan, `PR.md`, patch plans, verdicts),
path mapping, view, download, zip, refusals (all 404), the token and a new document arriving on the
event stream. `tests/test_browser_documents.py` (Chrome) checks the Documents tab, the rendered and raw
view, the download, an inert `<script>` in a plan, live appearance and the step panel's link; with
`AL_DASHBOARD_SHOTS=DIR` it saves `run-documents.png` and `run-documents-step-link.png` to DIR.
`tests/test_browser_discipline.py` (Chrome) opens a build step that stayed in its step and one that
left it, and with `AL_DASHBOARD_SHOTS=DIR` saves `step-discipline-ok.png` and
`step-discipline-violation.png` to DIR.
`tests/test_browser_leftovers.py` (Chrome) opens a run whose `build` agent left commands running and a
running run, and checks the node's `leftover commands stopped` line and note and the dashed `waiting`
edge and label; with `AL_DASHBOARD_SHOTS=DIR` it saves `run-leftovers-stopped.png` and
`run-waiting-edge.png` to DIR.
`tests/test_start.py` starts work through the real server and the layer's own launcher
(`tests/start_layer.py` builds a git app and runnable layers the way `al gen` lays them out, with the
mock agent): the catalog, a workflow and a `/plan-chore` single-command run with its plan as a
document, one run per request key (also from two threads at once), both confirmations, a run that
outlives the dashboard, every refusal, and the issue race against the layer's poller in both orders
and in trigger mode (`tests/fake_forgejo.py`, an in-memory Forgejo). `tests/test_browser_start.py`
(Chrome) types `/plan-c`, picks the completion, starts and lands on the run, and opens the ship
confirmation; with `AL_DASHBOARD_SHOTS=DIR` it saves `start-form.png`, `start-confirm.png` and
`start-run.png` to DIR. `tests/test_browser_promotion.py` (Chrome) checks the promotion card, the pack
page and the layer page's Promotions section; with `AL_DASHBOARD_SHOTS=DIR` it saves
`promotion-card.png`, `promotion-refused.png`, `promotion-pack.png` and `promotion-layer.png` (the
layer page's Promotions section with its buttons) to DIR.

`tests/settle_fixtures.py` builds the runs-list scenario as code: an invented application `orchard/` with
real git history and its layer `orchard.al/` with 27 runs (five merged, three squash-merged, two
integrated by a later run, four open, one with a pull request; five failures replaced by running
restarts; a waiting run; an older failure), and `build_group_scenario` with the five failures alone.
`tests/test_landing.py` checks Landing on small repositories built in the test (ancestor, patch-id, equal
paths, partly, open, the forge's answer, a gone branch, a remote-tracking target, the cache per target
head with a counting `safe_git`, and that refs, HEAD and the index are unchanged afterwards);
`tests/test_settle_model.py` the settlement record, the buckets of the scenario, replacement,
supersession, failure groups, `normalise_reason`, `needs_you` and the 8 checks per tick;
`tests/test_settle_http.py` `POST /api/settle` behind every guard, its refusals and the snapshot's counts;
`tests/test_standin_model.py` the Stand-in panel's view, the suggestion in a run's summary, the decisions, timeline marks and cost split of its detail, which items the loop takes, and the undo of a settlement;
`tests/test_standin_http.py` `POST /api/decider`, `/api/decision` and `/api/standin` behind every guard and under `--read-only`, each answer, "Ask the stand-in", the loop, the undo and `--stand-in-only`, on real layers with the mock agent;
`tests/test_browser_standin.py` (Chrome) the panel in each mode, a suggestion and its three answers, the Decisions card, the log and the header counts, and the items that waited already; with `AL_DASHBOARD_SHOTS=DIR` it saves `standin-panel-off.png`, `standin-panel-suggest.png`, `standin-panel-auto.png`, `standin-suggestion.png`, `standin-timeline.png`, `standin-log.png` and `standin-existing-items.png`;
`tests/test_start.py` "Start again" (`again`); `tests/test_browser_runs_settle.py` (Chrome) the chips and
counts, landed and settled rows, a failure group's **Start all again** and the settle form; with
`AL_DASHBOARD_SHOTS=DIR` it saves `runs-needs-you-1280.png`, `runs-needs-you-390.png`,
`runs-landed-1280.png`, `runs-settled-1280.png`, `runs-failure-group-1280.png`,
`runs-failure-group-started-1280.png` and `run-settle-form-1280.png`. `tests/tools/test_al_runs.py` (at
the repository root) runs `al runs`, `al settle` and `al runs … prune` on the same scenario.

`tests/integrate_fixtures.py` builds the scenario of a run that could not merge as code: an invented
application `pantry/` (a `stock.py`, an `INDEX.md` generated by `./index.sh`, a protected `deploy/`) with
real git history, and its layer `pantry.al/` (the mock agent, a workflow that merges by itself,
`templates/integrate.json`, a mock script for the resolve step) with nine runs: a merge conflict in a
source file and a generated file, a branch that applies cleanly on a moved `main`, a rejected review, a
change to the protected path, a branch a person changed, work that is on `main` by a squash merge, a run
a later run redid, and the two runs that moved `main`. `tests/test_integrate_model.py` checks which runs
are offered the button, the buckets while an integration runs, waits, failed or landed, the evidence
for a settlement, the graph of an integration run, and that a settlement written by the layer's runtime
is read by the dashboard; `tests/test_integrate_http.py` the three endpoints on a real server: the
preview and its nonce (used once, not for another run), every write guard, `--read-only` and
`--no-start`, a start that lands the work and moves the original to Landed, an unknown tier, the
approval, and that the evidence endpoint writes nothing; `tests/test_browser_integrate.py` (Chrome) the
**Give to an agent** button in the row and on the page, the preview at 1280 and 390 pixels, the whole
flow from the click to "nothing needs you", the two evidence buttons and a run that stays with a person. With `AL_DASHBOARD_SHOTS=DIR` it saves
`integrate-needs-you-1280.png`, `integrate-button-1280.png`, `integrate-preview-1280.png`,
`integrate-preview-390.png`, `integrate-running-1280.png`, `integrate-landed-original-1280.png`,
`integrate-runs-landed-1280.png`, `integrate-nothing-needs-you-1280.png`,
`integrate-evidence-landed-1280.png` and `integrate-evidence-replaced-1280.png`.
`tests/tools/test_al_integrate.py` (at the repository root) runs `al integrate` on the same scenario.

`tests/test_registry_views.py` checks, on the same scenario and on the promotion site, that both views
and the start form's data contain the kit entries beside the declared ones, that the picker lists only
the runs `integrate` applies to and says why when there are none (switched off, a broken settings file,
the script missing, nothing handed over), that `integrate` starts from the form's data on a handed-over
run, that a kit name is never a free-text start, that a kit run's page draws its steps with and without
a recorded definition, and what a layer without a registry module shows.
`tests/test_browser_registry.py` (Chrome) shows the Layers view's Kit workflows group, the picker, the
empty picker with its reason, a start from the form and the kit run's page; with
`AL_DASHBOARD_SHOTS=DIR` it saves `layers-kit-workflows-1280.png`, `start-integrate-picker-1280.png`,
`start-integrate-picker-empty-1280.png` and `kit-run-page-1280.png`. `tests/tools/test_registry_guard.py`
(at the repository root) generates the full example layer and fails when a script under `adws/` or a
workflow is missing from the registry or from what these two views return;
`kit/runtime/tests/test_registry.py` keeps the registry equal to the kit modules it describes.
`tests/assign_fixtures.py` extends that scenario for "Give to an agent": four more stopped runs written
as records on real branches (a review that rejected the change after two rounds, with one blocker and
a manifest that says 3 of 5 screenshots were left out; a rejected run with a `REQUIRES HUMAN` blocker; a
failed check whose worktree is kept; a run that failed before changing anything), a patch round and an
image artefact in the workflow, and the mock script that addresses the blocker; `KINDS` names the plan
of each of the nine runs under Needs you. `tests/test_assign_model.py` checks what a run's summary says
about `assign.json` (and that the constants and the counting are the runtime's), the next action of
every run under Needs you with and without `adws/assign.py`, the reason of a run that stopped twice for
the same reason, the left-out count of the run detail and the record in a run's signature;
`tests/test_assign_http.py` the three endpoints on a real server: a preview with the right kind for
every stopped run that starts and writes nothing, the nonce (missing, used, another run's, a bulk's),
a start that lands the addressed blocker through the gate, `--read-only` and `--no-start`, an approval
recorded for exactly one item with the plan that remains, the bulk order and a bulk start, unknown keys
and a long note, and a layer without the script; `tests/test_browser_assign.py` (Chrome) the button in
every Needs-you row and on every such page (disabled with its reason in a layer generated before
`al assign`), the plan of a rejected review at 1280 and 390 pixels and its start with a sentence for
the agent, "I approve this, continue" and the plan that remains, **Give all N to agents** with the
plans first, and the left-out count on the Artefacts tab. With `AL_DASHBOARD_SHOTS=DIR` it saves
`work-assign-needs-you-1280.png`, `work-assign-button-1280.png`, `work-assign-preview-rejected-1280.png`,
`work-assign-preview-rejected-390.png`, `work-assign-preview-person-1280.png`, `work-assign-approved-1280.png`,
`assign-bulk-1280.png` and `work-assign-left-out-1280.png`. The Automatic switch:
`tests/test_assign_auto_model.py` checks the setting's one format, its atomic write and removal, the
`assign_auto` view of a layer (a broken file, `--read-only`, `--no-start`, `--no-stand-in`, the
container, a layer without the script), which runs the loop takes (every run under Needs you, oldest
stop first; not a stop it tried, a run the stand-in or a person gave, or one started with `decider:
off`), that it queues one `give` per look and none where it may not run, and that the model stand-in
waits for the rules; `tests/test_assign_auto_http.py` the request on a real server (400, 403, 404 and
409 answers, the write and the published view with `--no-stand-in`) and, end to end with the layer's
own `adws/assign.py` and the mock agent, a failed check resumed, an unchanged run started again, a
`person` run that stays with its reason, and that nothing is given twice;
`tests/test_browser_assign_auto.py` (Chrome) the switch in the Needs-you view, its confirmation and
Cancel, the report lines, the crumbs and switching it off, and with `AL_DASHBOARD_SHOTS=DIR` saves
`work-assign-auto-confirm-1280.png` and `work-assign-auto-on-1280.png`. `tests/tools/test_al_assign.py` (at the
repository root) runs `al assign`.

`tests/approvable_fixtures.py` adds four stopped runs to the demo-shop fixture, with the
`approvable.json` records the runtime would write (a proof stop, an evidence stop with two protected
paths, a record with another basis, a `needs-declaration` stop without a record).
`tests/test_approvable_model.py` checks the row's `approval`, that a record about another state of the
run is ignored, the count behind the chip and hostile text kept as data;
`tests/test_approve_resume_http.py` the endpoint on a real layer: the row gets its approval from the
layer's launcher, an approval recorded with who and note that resumes the run, the refusals (a stale
token, an unknown item, a run whose process is alive), several runs at once, malformed bodies and the
guard; `tests/test_browser_approve_resume.py` (Chrome) the chip, the row button with its confirmation
and the run page's banner. With `AL_DASHBOARD_SHOTS=DIR` it saves `run-approve-row.png`,
`run-approve-confirm.png` and `run-approve-banner.png`.

`tests/test_workspace.py` covers the workspace scan (states, sibling layers, al scout's summary, depth,
symlinks out of the folder, the file and time bounds, git without fsmonitor or hooks, the cache and the
refresh limit, the default workspace, the al lookup, escaping); `tests/test_actions.py` the actions (the
allow-list and every refusal, the write checks shared with `/api/models`, `--read-only`, `--no-actions`,
preview and nonce, the adopt dry run, the create and adopt order (init, scout, adopt, author, gen, check,
result), the detected choices, a changed choice reaching the author step, refused choices, the result
card's document, the per-repository lock, a job surviving a restart, a dead runner
shown as `interrupted`, the job's tail, changed files, document confinement, never overwriting a
definition, a missing al tool, a missing tool as a step's failure reason, the prerequisite check stopping a
job before any step, "Retry from STEP" and its refusals, the tools table, the job's `PATH`), with
`tests/workspace_fixtures.py`: four invented repositories (`pantry`, `quill`, `comet`, `fern`) and
`fake_al`, a stand-in for al that records its argv, can fail a subcommand (`fail`; its `doctor` answers
`ok` otherwise), can replay a recorded agent stream for one (`streams`, with `job_stream`, which puts the
test's workspace and home into it), can keep one running (`hold`), and answers `create --choices --json`
with the start form's choices and `result --json` with a result card (`choices`, `card`).
`tests/test_placement.py` covers the placement of create and adopt: the two paths, `placement_view` for a
fresh repository, an existing folder, a path that is already a layer, a file or symlink there, an
unwritable folder and tracked files under `agentic/`; the action views listing both placements; the
default (`beside`), the preview naming the chosen path, `inside` reaching every step's argv for create
and adopt, refused values (400), the nonce bound to the placement and an unusable placement (409), the
same write guard, and a layer created inside listed as an in-repo layer.
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
disabled actions without al, a failed job's reason with its Retry button, the start form's detected choices
(a changed review reaching the author step) and the result card; with `AL_DASHBOARD_SHOTS=DIR` it saves
`workspace-repos.png`, `workspace-adopt-preview.png`, `workspace-job.png`, `job-missing-tool.png`,
`job-retried.png`, `start-form-choices.png` and `result-card.png`. `tests/test_browser_generate.py` (Chrome)
checks the placement picker of the al panel (the path, the checks and the command lines of the selected
placement, the placement in the confirmation dialog and on the job page) and guided generation: the four
steps to a started job with only `POST /api/action` sent and nothing but the read-only preview run before
Start, Back keeping the answers and a new placement asking again, an unusable placement disabled, adopt by
default for a repository with a setup, and the reason shown when actions are off; with
`AL_DASHBOARD_SHOTS=DIR` it saves `generate-placement-panel.png`, `generate-wizard-repo.png`,
`generate-wizard-placement.png`, `generate-wizard-options.png`, `generate-wizard-review.png` and
`generate-wizard-job.png`. `tests/tools/test_dashboard_workspace.py` (at the
repository root) runs the same flows end to end with the real al and the mock author agent, a create
inside the repository included.
`tests/test_voice_helper.py` covers proposals (confirm then approve, every refusal, expiry with an injected
clock, single use, the flags per action kind, retry as a start), `classify_answer`, the fixed tool
registry and that speech cannot widen it, `speakable`, `parse_helper_reply`, the four-tool loop limit,
`is_echo` and where conversations are kept; `tests/test_voice_server.py` runs the routes against the speech
service in fake mode (`--voice-fake`, `tests/voice_fixtures.py`): status, guards, a card from an utterance,
yes without Approve starting nothing, Approve starting a mock run, every refusal, the job, resume, answer
and stop cards, speak and cancel, echo, recordings and the service's start and reaping.
`tests/test_browser_voice.py` (Chrome with a fake microphone and fake engines, 1280 and 390 px) checks every
panel state, hold to talk, always listen, approve, barge-in, the synthesizer following the audio, reduced
motion, `--read-only` and hostile text; with `AL_DASHBOARD_SHOTS=DIR` it saves `voice-*.png` and
`voice-exchange-{1280,390}.webm`. `tests/test_browser_voice_real.py` (markers `voice`, `slow`;
`AL_VOICE_REAL=1` and the models set up) runs the same flow with the real engines.
`tests/testing_fixtures.py` builds a layer with targeted testing as code (`build_testing_site`: the layer
of `tests/start_layer.py` with a `testing` IR, commands with narrower forms, the sprint, six runs: targeted,
escalated to full, the culprit of a red batch, its fix run, the batch run and a running run; the ledger with
a green and a red batch, a miss and suite timings; a stand-in `adws/verify_batch.py` that records its argv).
`tests/test_testing_model.py` checks the Testing panel's model (the undeclared note, levels and typical
minutes, miss rates and the fallback, the setting mirrors and the refusal sentence, batches per stage with
culprit and fix run), a run's depth and accounting, the "broke the batch" badge, the proof rows' depth and
the watcher; `tests/test_testing_http.py` the three endpoints behind every guard, `--read-only`,
`--no-start` and the container, and `testing` reaching the launcher's argv; `tests/test_browser_testing.py`
(Chrome) switches the panel to heavy, a run page's depth, Depth column and "Full before it ships", the
culprit's banner, the badge in the runs list, "Run the full suites now" and the start form's choice; with
`AL_DASHBOARD_SHOTS=DIR` it saves `testing-panel-1280.png`, `testing-heavy-1280.png`,
`run-targeted-accounting-1280.png`, `batch-failed-1280.png` and `start-testing-1280.png`.

`tests/test_writes_together.py` runs both features in one server: `--read-only` refuses all three
writes, `--no-start` and `--no-actions` each refuse only their own, each write has its body limit,
and both features' GET routes and banner states are there; the voice helper's Approve follows the same
flags per kind of card while its conversation keeps working.

## Demo scripts (`demo/`)

| Script | Does |
|---|---|
| `live_demo.py` | builds a tiny app and its layer in a temp directory, starts the dashboard and three runs, records Chrome with Playwright and writes `live.gif` into `--out` and the video into the ignored media folder as `demo-dashboard-live.mp4` (a release asset, `docs/media.json`). Planning, commit and review use the mock agent; the build step uses `replay_agent.py`, a scripted stand-in CLI agent that streams a Claude-format transcript while it really edits the worktree (no LLM, $0). Checks run for real in podman |
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
