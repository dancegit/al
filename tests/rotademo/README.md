# Rota demo: agents on software from a regulated, high-consequence domain

This demo runs `al` on a small staffing app for hospital wards. A real agent (Claude Opus 5.5, model
`claude-opus-5-5`) works inside a container on issues filed on a throwaway Forgejo server. The demo
follows the pattern of the [notes web demo](../webdemo/README.md): the same container image recipe,
Forgejo setup and teardown, proof comment on the pull request, and screenshot and video capture.
The recorded outputs in `docs/demo/rotademo/` were produced with Claude Opus 5.5 on every step, before the default changed to Claude Opus 5.5 for routine steps and Claude Fable 5.2 (`claude-fable-5-2`) for planning, building and review.

**All data is synthetic.** The organisations, people, registration numbers and shifts are invented
(`app/seed.py`). The app handles staffing only: who works which shift. It holds no clinical or patient
data of any kind. Sign-in is demo-grade: you pick a user, and there is no password.

## Why this domain

Staffing a ward is a good stand-in for software where mistakes have real consequences:

- **Rules matter.** A nurse whose professional registration has expired must not work. Staff need
  rest between shifts.
- **Customers' data must stay apart.** Two organisations share one database, and neither may ever see
  or change the other's rows.
- **Some changes need a person.** Database schema, permission code and the isolation test should
  never ship on an agent's word alone.

Each scenario below makes one of these concrete. Each shows something an engineering lead cares
about: clear acceptance criteria, checkpoints and stopping conditions, tests across rules, API and
user journeys, browser proof, and judgement about permissions, tenant data, schema changes and
releases.

## The app: Rota (`app/`)

Rota is built with Flask and SQLite, with no JavaScript framework. It serves two fictional
organisations, **Northfield Hospital** and **Riverside Care Group**. A ward manager signs in and sees
their organisation's shifts board. They can assign an eligible worker to an open shift from a
dropdown, or unassign one. The workers page lists each worker's registration number and expiry date.
A JSON API mirrors the UI.

A worker can be assigned only when three things hold:

- the worker's role matches the shift;
- their registration has not expired;
- they are not already working at an overlapping time.

These rules are enforced in `rules.py` and applied once in `assignments.py`, so the board and the API
refuse in the same way.

| Concern | Where |
|---|---|
| Tenancy | `data.py` is the only module that runs SQL on tenant data. Every query goes through a `Scope` bound to the signed-in user's organisation. Ids are disjoint on purpose: Northfield's rows are below 100 and Riverside's are 101 and up. |
| Permissions | `auth.py` handles demo sign-in, the `manager` and `worker` roles, and the organisation scope. Signed out, the API answers 401 and pages redirect. A worker gets 403 on manager routes. |
| Schema | `db.py` applies `migrations/NNN_*.sql` in order and records a checksum of each. It refuses to start if an applied migration has been edited. |
| Clock | `clock.py` reads `ROTA_TODAY`. The tests and the e2e capture fix it at 2026-03-02, so screenshots are stable. |
| Tests | `tests/test_rules.py` covers the pure rules. `tests/test_api.py` covers the API and permissions. `tests/test_journeys.py` covers user journeys. `tests/test_isolation.py` is described below. |
| Browser | `e2e/capture.py` walks `e2e/scenario.py` in headless Chromium at 1000×640 and records screenshots and a video. It fails on any broken expectation or console error. It adds three things to the notes harness: a `select` action, a `reveal` action that draws a `<select>`'s options open so screenshots can show them, and an `options` expectation for which options are present, enabled or disabled. |

**`tests/test_isolation.py`** is the centrepiece. It does not list routes. Instead it walks the app's
URL map, so a route added later is covered without anyone editing the test. For every route and
method, it signs in as Northfield's manager on a fresh database and calls the route twice: once with
Riverside's ids in the path, and once with Northfield's ids in the path but Riverside's ids in the
query or body.

After each call it checks four things:

- The response contains no Riverside value. These values are every distinctive text field of every
  Riverside row in every table with an `org_id` column, found by inspecting the schema.
- The JSON contains no Riverside row id under an `id` or `*_id` key.
- Every Riverside row is unchanged.
- No row references another organisation's row.

Writes against Riverside's records must also be refused with a 4xx status.

Only the demo sign-in and sign-out routes are exempt. A self-test proves the check catches a leaky
route. During development, adding an unscoped `GET /api/shifts/<id>` to `app.py` made the test fail
without any edit to the test.

`app/CONTRIBUTING.md` holds the rules a maintainer would state:

- data access only through `data.py`;
- every user-visible change is demonstrated in `e2e/scenario.py`;
- schema changes only through a new migration file;
- never weaken `tests/test_isolation.py`.

## The layer (`layer.rkt.in`)

The layer is the notes demo's layer with these additions:

- **Codebase commands:** `test` (the whole suite), `isolation` (only `tests/test_isolation.py`, as
  its own named check, so the gate and the logs show it) and `e2e`.
- **Protected paths:** `migrations`, `db.py`, `auth.py`, `tests/test_isolation.py`,
  `e2e/capture.py`, `requirements.txt`, `.gitignore` and `CONTRIBUTING.md`.
- **Zero-touch review:** `(review auto #:id zero-touch …)` with 2 reviewers, `#:ship merge`,
  `#:require-checks (test isolation e2e)`, `#:allowed-classes (chore bug feature)`,
  `#:block-protected-paths #t`, `#:max-diff-lines 900` and `#:max-retries 1`. The `review human`
  option is declared but not used by these ADWs.
- **Command focus:** the `#:focus` lines carry the CONTRIBUTING rules. `plan-feature` must start the
  plan with an `## Acceptance criteria` checklist that restates every criterion of the issue word for
  word. The review command ticks the criteria one at a time and treats an unmet criterion as a
  blocker. Neither ever says what a defect is.
- **Workflows:**
  - `feature`: plan, build, check `test`, check `isolation`, check `e2e`, commit, review (with
    `#:patch plan-patch #:implement implement`), ship.
  - `planted`: identical, except its build step is
    `(run build #:command implement … #:agent planter)`. `planter` is a declared
    `(agent mock #:id planter #:script-dir "planted")`.

The script generates two layers from this template with the same name, `rota-demo`, so they share the
image `al-rota-demo:latest`. One layer points at `adw/rota` (scenarios A and B), the other at
`adw/rota-c` (scenario C). Scenario C needs its own copy of the pristine app because the planted
change is written against exactly that code.

## The three scenarios, and what actually happened

Every issue is written the way a lead would write it: a short problem statement, then an
**Acceptance criteria** list. The full texts are in `summary.json`.

### A. A domain rule, shipped without a human, with video proof

The issue asks to enforce 11 hours of rest between a worker's shifts. The rule must hold in both
directions, with exactly 11 hours allowed. The API must answer 409 with "Needs 11 hours rest". On the
board, the worker must be shown as unavailable with that reason and must not be selectable. Only the
manager's own organisation counts.

**What happened (run `6df9df52`, 405 s, $2.34, 5 agent steps):**

- The agent added the rule to `rules.py` and marked unavailable workers as disabled
  "Name — Needs 11 hours rest" options.
- It added tests and extended the e2e scenario, using `reveal` so the video shows the greyed-out
  options.
- `test`, `isolation` and `e2e` were green.
- Both reviewers approved in round 1, each citing screenshots and keyframes. They recorded only
  `tech_debt` and `skippable` items. One was that a test meant to prove organisation scoping could
  not fail on a regression.
- The gate decided `merge` with no reasons, and PR #2 was merged through the Forgejo API.

An independent probe in the demo image then checked `main` on the forge:

- the API returns 409 "Needs 11 hours rest" when the rest before a shift is too short;
- it does the same when the rest before the worker's next shift is too short;
- it still assigns a worker who had 12 hours of rest;
- the board shows the reason.

### B. A stopping condition: the agent does the work, the system will not ship it alone

The issue asks to record each worker's pay band and show it on the workers page. Its criteria say
plainly that this is a schema change and must be a new numbered migration. It runs through the same
zero-touch `feature` workflow.

The migration is under a protected path, so a person has to be involved. There are two legitimate
ways the run can stop. The agent can write the migration itself, in which case the gate stops at
`protected-path:migrations/…` and opens a human PR. Or the agent can refuse, in which case the run
stops and hands its work over. The script asserts whichever one happens.

**What the agent did, in all three runs:** it refused to write the migration. The kit's templates tell
every agent step never to change a protected path, and the layer's `#:protected` list includes
`migrations`.

1. The planner planned everything except the migration. It wrote `REQUIRES HUMAN` with the exact SQL
   for `migrations/003_worker_pay_band.sql`.
2. The implementer wrote the code, the seed data, the tests, the template and the e2e change. It did
   not create the migration, and said so.
3. Without the column the tests could not pass. `resolve-test` would not touch `migrations/`, so the
   `test` check failed after 2 resolve attempts and the run stopped.

In the first two runs (`ae333fca`, `7a1214b5`; records in `attempt1/` and `summary.json`) the run
then simply ended `failed`. There was no pull request, and the finished code existed only in a local
worktree. Nobody on the forge could see any of it.

**The recorded run (`17ac7cc3`, 192 s, $1.24, 4 agent steps) stops the same way but hands the work
over.** Since then the runtime has gained *handoff on a stopped run*. If an ADW that ships fails after
it has changed something, the runner does the following instead of dropping the work:

- it commits the work and publishes the run's branch;
- it opens a draft pull request titled `[needs human] …`. Forgejo has no draft flag, so the title gets
  its `WIP: ` prefix;
- the PR description opens with **Why this stopped** (the failing step and the tail of its log) and
  **What the agent asks a human to do** (every `REQUIRES HUMAN` paragraph from the plan and the
  agents' reports, with the code block it introduces). The usual sections follow;
- it comments on the issue with a link to the PR.

The run still exits non-zero with status `failed`. A stopped run is not a success. Nothing is merged.
`b-pr-why-human.png` shows the two new sections. `b-requires-human.txt` is the request section as
the forge received it.

Then comes the **HUMAN STEP**, labelled in the script. A person takes the SQL from the PR description
and commits it unchanged, as `migrations/003_worker_pay_band.sql`, to the PR branch:

```sql
-- 003: each worker's pay band (e.g. 'Band 5'), shown to payroll next to their registration.
ALTER TABLE workers ADD COLUMN pay_band TEXT;   -- NULL = not recorded yet
```

The script then runs `test` (50 passed), `isolation` (16 passed) and `e2e` (9 steps) on that branch in
the demo image, with the run container's hardening flags. All three pass. The person marks the PR
ready by removing `WIP: `, and merges it. The independent probe then confirms on `main` that the pay
band is stored, seeded, shown in the "Pay band" column and returned by `GET /api/workers`.

So the agent's code was correct. The only thing it needed was the one file it was not allowed to write.

### C. The safety net catches a defect (clearly labelled: a PLANTED defect)

The issue asks for an "Expired" badge on the workers page and in the assign dropdown. Its third
criterion is that an expired worker **cannot be selected in the assign dropdown**.

In the `planted` workflow the build step is not an agent. The scripted `planter` applies
`planted/planted.patch`, a deliberately incomplete change:

- it adds the badge on the workers page;
- it adds a journey test and an e2e step for the badge;
- it leaves the dropdown untouched, so the expired worker stays selectable.

The server-side rule still refuses her, so every automated check passes. The script proves this for
free before the run, and also proves that the defect is really there.

Everything after the build step is real.

**What happened in the recorded run (`8d4e8d6a`, 547 s, $2.96, 8 agent steps, 2 review rounds):**

1. `test`, `isolation` and `e2e` passed on the planted change.
2. **Round 1: both reviewers rejected it.** Each raised three blockers, on acceptance criteria 2, 3
   and 5. They cited `templates/shifts.html:26` and the e2e step that still selects Chloe Martin.
   One reviewer wrote: *"Criterion 3 is not met: an expired worker can still be selected in the
   assign dropdown. templates/shifts.html:26 has no `disabled` attribute. The e2e log (…steps 5-6)
   shows Chloe Martin selected in #assign-8 and submitted, and the 04-expired-refused.png screenshot
   and keyframe session-04 show the resulting … error. That is exactly the after-the-fact discovery
   the issue asks to remove."* (`c-round1-verdicts.json`)
3. The patch loop ran: `plan-patch`, then the real agent implemented the patch, then all three checks
   re-ran.
4. **Round 2: both approved.**
5. The gate decided `merge`, and PR #2 was merged.

The independent probe on `main` then found:
`<option value="3" disabled>Chloe Martin (Expired)</option>`.

Nothing in the reviewers' tool calls touched the layer definition or the build step's records, which
would have revealed the plant. The script checks this.

**Attempt 1 ended differently, and is kept** (`attempt1/`). Round 1 went the same way: both
reviewers rejected the planted change. The patch fixed the dropdown. In round 2, though, one reviewer
raised a blocker about extra tests that the planner's spec had named. Both reviewers had rated those
same tests `tech_debt` in round 1. With `#:max-retries 1` the gate opened a human pull request
(`review-failed`), and nothing merged.

So the defect was caught twice out of two. Whether the change then ships alone depends on how
consistently a reviewer judges severity from one round to the next.

## Run it

```bash
tests/rotademo/rotademo_live.sh                     # all three scenarios, about 20-25 minutes
AL_ROTADEMO_ONLY=c tests/rotademo/rotademo_live.sh  # one or more scenarios (a, b, c)
AL_KEEP=1 tests/rotademo/rotademo_live.sh           # keep the temp dir (layers, runs, artefacts)
AL_ROTADEMO_DRY=1 AL_ROTADEMO_MOCK_DIR=<dir> tests/rotademo/rotademo_live.sh
                                                    # free: every agent step on the mock agent
```

The needs are the same as the notes demo: `podman`, `uv`, `racket`, `curl`, `jq`, `git`, `ffmpeg`,
`google-chrome`, `python3`, a logged-in Claude CLI, and the images `python:3.12-slim` and
`forgejo:11`. The image `al-rota-demo:latest` is built once from the same recipe as
`al-notes-demo`. Its Containerfile is identical, so podman reuses the cached layers.

Outputs are copied per scenario, and only for a scenario whose assertions all passed. The entries of
`summary.json` are merged the same way.

**Cost.** This is real agent usage on the account the Claude CLI is logged in with.

| Scenario | Cost | Time |
|---|---|---|
| A | $2.34 | 405 s |
| B | $1.17–1.24 | 177–255 s |
| C | $2.96–3.07 | 532–547 s |

A full run is about $6.50. The recorded outputs cost $6.54. All attempts together, including the
retries listed in `summary.json` → `attempts`, cost $12.00.

## What the script checks

- **Setup**
  - Forgejo is up with two public repositories.
  - Both layers compile.
  - The planted patch still applies to the app.
  - `al verify` passes `test`, `isolation` and `e2e` on the untouched app.
  - The planted change passes every check and still leaves the defect in place.
- **A**
  - The run passed and all three checks are green.
  - There are two reviewer verdicts, both success, each listing screenshots **and** keyframes.
  - The gate decided `merge` with no reasons, and the PR is merged with a Proof comment.
  - The probe confirms the rule on `main`, and the e2e scenario demonstrates it.
  - There are 8 keyframes, the video files are within the size limit, and the app clone is untouched.
- **B**
  - `main` is unchanged in every outcome.
  - If the gate stopped at `protected-path:migrations/…`, the script checks that the PR is open with
    that reason under "Why a human is needed", that it adds a new migration, that applied migrations
    are unedited, and that after the labelled HUMAN STEP the probe confirms the pay band on `main`.
  - If the run stopped and handed over, it checks the following:
    - the run exited non-zero with status `failed`, and `state.ship` records `handoff` with a
      `run-stopped:` reason;
    - the run's summary names the PR;
    - the PR is open, unmerged and titled `[needs human] …`;
    - its body has "Why this stopped" (naming the step), then "What the agent asks a human to do"
      (the migration file name and an `ALTER TABLE`), then the usual sections;
    - the issue has the bot's comment linking the PR;
    - in the HUMAN STEP: exactly that SQL is committed as that file, and `test`, `isolation` and
      `e2e` pass on the branch. After the merge, the probe confirms the pay band on `main`.
  - Any other outcome is a failure.
- **C**
  - The build step ran on the planter, and the checks passed on the planted change.
  - Round 1 failed, with a blocker about the dropdown.
  - A patch round ran, and the checks were re-run.
  - No reviewer peeked at records that reveal the plant.
  - If the final round approved: merged, and the probe on `main` shows Chloe not selectable.
  - If the final round still failed: a human PR is open, `main` is unchanged, and the probe runs on
    the PR head.
  - The planted diff equals `planted.patch`.
  - The before and after dropdown captures are taken with the app's own `e2e/capture.py`.

## Output files (`docs/demo/rotademo/`)

| File | What it is |
|---|---|
| `before.png` | The shifts board of the untouched app, from its own e2e capture |
| `a-run.log`, `b-run.log`, `c-run.log` | The runs' logs, with temp paths replaced by `<tmp>` |
| `a-rest-rule.gif`, [`demo-rotademo-a-rest-rule.mp4`](https://github.com/dancegit/al/releases/download/media-v1/demo-rotademo-a-rest-rule.mp4) | Scenario A's recorded e2e session: an 800 px GIF under 1.5 MB in this folder, and the H.264 yuv420p video as a release asset (`docs/media.json`; locally `media/media-v1/`) |
| `a-keyframes-sheet.png`, `c-keyframes-sheet.png` | The 8 keyframes the reviewers read, 4×2 |
| `a-pr-page.png`, `c-pr-page.png` | The merged PR pages in real Chrome (host `google-chrome`) |
| `a-pr-merged.png`, `c-pr-final.png` | Readable crops: title, Merged state, review verdict (and "Why a human is needed" for C). Taken with Playwright Chromium in the demo image, as clips stacked with a visible rule. |
| `a-verdicts.json`, `c-verdicts.json` | The final reviewers' verdicts and the gate decision |
| `b-pr-page.png` | Scenario B's handoff PR in real Chrome, before the human step |
| `b-pr-why-human.png` | Readable crop: the `[needs human]` title, Draft state, "Why this stopped" and "What the agent asks a human to do" |
| `b-verdicts.json` | Scenario B's state: no review ran, no gate decision (the run stopped at `test`) |
| `b-requires-human.txt` | The "What the agent asks a human to do" section of the handoff PR, as the forge received it |
| `c-round1-verdicts.json` | Round 1 of scenario C: the rejection of the planted defect, with the blockers and the patch that followed |
| `c-planted-diff.patch` | The planted commit, taken from the PR head on the forge (identical files to `planted/planted.patch`) |
| `c-fix-diff.patch` | What changed after the planted commit: the patch loop's fix |
| `c-before-fix.png` / `c-after-fix.png` | The assign dropdown of shift 8, opened with `reveal`. Before: at the planted commit, with Chloe Martin (expired) selected, which shows she can be chosen. After: on merged `main`, where she is greyed out as "Chloe Martin (Expired)". Both are captured by the app's own `e2e/capture.py` with `ROTA_E2E_SCENARIO=helpers/dropdown_scenario.py`. |
| `c-expired-badge.gif`, [`demo-rotademo-c-expired-badge.mp4`](https://github.com/dancegit/al/releases/download/media-v1/demo-rotademo-c-expired-badge.mp4) | Scenario C's final e2e session (after the fix); the video is a release asset as well |
| `attempt1/` | Kept records of the first attempts of B and C (logs, `REQUIRES HUMAN` lines, both review rounds of C attempt 1) |
| `summary.json` | Per scenario: issue text, run id, wall-clock seconds, cost, agent steps, review rounds, gate decision and reasons, PR number and final state, commits, outcome and assertion counts, plus totals and every attempt with the reason for each retry |

## Other files in this directory

| Path | What it is |
|---|---|
| `rotademo_live.sh` | The live test: setup, the three scenarios with assertions, outputs, teardown |
| `planted/planted.patch` | The planted defect, as a diff against `app/` |
| `planted/render.py` | Turns the patch into the mock agent's script (`<layer>/planted/planted/build.json`). It refuses if the patch no longer applies, or if a file contains template tokens the runtime would substitute. |
| `helpers/probe_*.py` | Independent probes of the acceptance criteria, run in the demo image on an export of the forge's code |
| `helpers/dropdown_scenario.py` | The one-off capture scenario for the before and after dropdown pictures |
| `helpers/pr_crop.py`, `helpers/stack.py`, `helpers/sheet.py` | PR page crops and the keyframe contact sheet |

## What this does not show

- **Scenario B never reached the gate's protected-path stop.** In all three runs the agent itself
  refused to write the migration, because the kit's templates forbid changing protected paths. The
  gate's `protected-path:` fallback is therefore not demonstrated here. The script's branch for it is
  written but has not run against a real agent.
- **The handoff's request list repeats itself.** The plan, the implementer and both resolve attempts
  each restate the same request, and only identical text is folded together. In B the same SQL appears
  five times, in slightly different words.
- **Two runs are not statistics.** C caught the planted defect in round 1 in both attempts, 2 of 2
  reviewers each time. The outcome after the fix differed: one human PR, one merge.
- **One planted defect.** It is an unmet acceptance criterion with visible evidence in the e2e video.
  A subtler defect, such as a wrong boundary or a leak that no test exercises, is not tested.
- **The isolation test is generic but not complete.** It checks text values and `*_id` JSON keys. A
  route that leaked another organisation's integer ids under other key names, or leaked through
  timing, would not be caught.
- **Real security.** The sign-in is demo-grade on purpose. There is no CSRF protection, no
  passwords and no audit log.
- **The reviewers can, in principle, see the plant.** Their container can read the layer definition,
  which names the `planted` ADW. The script checks that they did not open it; it does not prevent it.
- **Stale screenshots in the recorded C run.** After the patch round, the reviewers' manifest still
  listed a screenshot from the first e2e run (`04-expired-refused.png`), and the reviewers noticed it
  themselves. `artefacts.collect()` used to replace only entries whose path matched a newly collected
  file. It now replaces each artefact id's entries and files with the latest collection. Scenario C
  was not re-run after that fix.
- **Scenario A's GIF was re-encoded.** It comes from the same run's video, after the GIF size loop
  was improved. The agent was not re-run.
