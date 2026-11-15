# Web application demo: a real agent changes a web app, with screenshots and video as proof

This demo shows `al` doing what a small team would do with a web application. Two requests are
filed as issues on a Forgejo server. A real agent (Claude Opus 5.5, model `claude-opus-5-5`)
works on each one inside a container. It plans the change, writes it, runs the app's tests, opens
the app in a real browser and records screenshots and a video. Then:

- **Scenario A: a visual change that a human reviews.** The request is *"Change the page background
  colour to dark slate (#1e293b) and make the text light so it stays readable."* The run opens a
  pull request. The issue carries a screenshot of the current page, so the PR description shows the
  **before** picture. The PR's *Proof* comment shows the **after** screenshots and the video's
  keyframes, uploaded to Forgejo. A person looks and merges; the script does this as one clearly
  labelled step (`HUMAN STEP: approve and merge in Forgejo`).
- **Scenario B: a new feature that ships with no human.** The request is *"Add a search box above the
  notes list that filters the notes as you type; show 'No notes match' when nothing matches."* The
  agent also extends the browser scenario so the video demonstrates the search. Two independent
  review agents each read the diff, the logs, every screenshot and every video keyframe. They must
  both approve. Then the hard rules (tests and e2e re-run on the merged result, allowed work class,
  diff size, protected files) decide, and the change is merged through the Forgejo API.

Everything is thrown away afterwards: the Forgejo server, the containers, the clone, the layer.
What remains are the files in [`docs/demo/webdemo/`](../../docs/demo/webdemo).

## What is in this directory

| Path | What it is |
|---|---|
| `app/` | The example app, **Notes**: Flask, one template, one stylesheet, unit tests, and `e2e/capture.py`, which walks `e2e/scenario.py` in headless Chromium (1000×640) and records `e2e-results/NN-<name>.png` and `e2e-results/session.webm`. It fails on any broken expectation or console error. |
| `layer.rkt.in` | The agentic layer (`#lang al`). The script fills in the Forgejo address. It declares Claude inside a podman container (`#:agent-inside #t`, `bypassPermissions`), the `test` and `e2e` commands, the screenshot and video artefacts (8 keyframes), a human review and a zero-touch review with 2 reviewers, and two workflows: `ui-change` and `feature`. |
| `webdemo_live.sh` | The live test: setup, the two scenarios with assertions, outputs, teardown. |

## Run it

```bash
tests/webdemo/webdemo_live.sh            # about 8–10 minutes once the image exists
AL_KEEP=1 tests/webdemo/webdemo_live.sh  # keep the temp dir (layer, runs, artefacts) for inspection
```

You need `podman`, `uv`, `racket`, `curl`, `jq`, `git`, `ffmpeg`, `google-chrome` (host Chrome takes the
PR page screenshots), the images `docker.io/library/python:3.12-slim` and
`codeberg.org/forgejo/forgejo:11` (pulled if missing), and a logged-in Claude CLI
(`~/.claude/.credentials.json`, mounted read-only into the container).

The first run builds the container image `al-notes-demo:latest`. It contains Python, Flask, pytest,
Playwright with Chromium and its ffmpeg, and the Claude CLI, and takes several minutes on a slow link.
Later runs reuse it. Delete it with `podman rmi al-notes-demo:latest` to force a rebuild.

`AL_WEBDEMO_ONLY=a` or `=b` runs one scenario for debugging. Nothing is copied to
`docs/demo/webdemo/` unless both scenarios ran and every check passed.

**Cost and time.** Real agent usage, on the subscription or API key the Claude CLI is logged in
with. Across three full runs: scenario A took 111–123 s and cost $0.47–0.52 (3 agent steps: plan,
build, commit). Scenario B took 254–283 s and cost $1.31–1.45 (5 agent steps: plan, build, commit
and the two reviewers). That is $1.78–1.96 per full run. The recorded run (`summary.json`) cost
$1.78.

## What the script checks

- **Setup:** Forgejo answers, the repository is public, the layer compiles, and `al verify --cmd e2e`
  passes on the untouched app in the layer's own container.
- **Scenario A:** the run passes. The PR is open and targets `main`. The PR description shows the
  before screenshot. Exactly one Proof comment embeds attachment images, and they are served without a
  login. The unit tests and the e2e capture passed in the container. After the human step, the PR is
  merged and `static/style.css` on `main` contains `#1e293b`.
- **Scenario B:** the run passes. Two reviewer verdicts are both `success: true`, and each lists
  screenshots **and** video keyframes in `artefacts_reviewed`. The gate decided `merge` and the PR is
  merged. `main` contains the feature ("No notes match"), the extended `e2e/scenario.py`, and still
  scenario A's dark theme. Eight keyframes were extracted.
- **Both:** the local clone of the app is untouched by the run: same HEAD, branch and
  `git status --porcelain`. Between the scenarios the script pulls `main` into that clone, as a
  maintainer would. That is the only change to it, and it is made by the script, not by a run.

## The output files (`docs/demo/webdemo/`)

| File | What it is |
|---|---|
| `before.png` | The untouched app (light theme), captured by its own e2e scenario in the demo image |
| `a-after.png` | Scenario A: the same screen after the agent's change, from the run's e2e screenshots |
| `a-pr-page.png` | Scenario A: the pull request page in real Chrome: before picture, run summary, Proof comment |
| `a-run.log` | Scenario A: the run's log |
| `b-feature.webm` | Scenario B: the video the e2e capture recorded (what the reviewers' keyframes come from) |
| `b-feature.mp4` | The same video as H.264/yuv420p for players that cannot play WebM |
| `b-feature.gif` | The same video as an 800 px GIF (≤ 1.5 MB) |
| `b-keyframes/*.png` | The 8 keyframes the runtime extracted and both reviewers read |
| `b-verdicts.json` | The two reviewers' verdicts (summary, issues, artefacts_reviewed) and the gate decision |
| `b-pr-page.png` | Scenario B: the merged pull request page in real Chrome |
| `b-run.log` | Scenario B: the run's log |
| `summary.json` | Per scenario: prompt, wall-clock seconds, agent cost in USD, agent steps, PR number, commits |

Temp paths in the logs are replaced with `<tmp>`. The PR pages contain the temp paths as they were
during the run.

## Notes

- The proof comment is posted by the runtime (`kit/runtime/adw_modules/ship.py`, `post_proof`). On
  Forgejo, the files are attached to that comment. The GitHub and `none` forges cannot host files, so
  the PR keeps only its text list of artefacts.
- Chromium runs with `--no-sandbox --disable-dev-shm-usage`. Its own sandbox needs user namespaces,
  which the rootless, capability-dropped run container does not grant; the container is the sandbox.
- The before picture comes from a second capture, because `al verify` discards its throwaway
  worktree and the `e2e-results/` in it. That capture runs in the same image with the run container's
  hardening flags, on `git archive main`.
