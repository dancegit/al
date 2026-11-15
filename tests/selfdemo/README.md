# Self demo: changing an agentic layer through al's own reviewed workflow

An agentic layer is the set of prompts, workflows and rules that agents follow on a codebase. Over
time you want to change it: "let me queue work in a text file", "every plan must say how to undo the
change". This demo shows those changes being made **by al's own workflow**, `al improve`, and arriving
the way any change to code arrives: as a pull request that a person reads and merges.

The app is the same small Notes web app as the web demo ([`tests/webdemo/app`](../webdemo/app)). This
time its layer is **checked in with the code**: it lives in `agentic/` inside the app's repository
(placement `in-repo`), so the team shares it and changes to it are reviewed like code.

A real agent (Claude Opus 5.5, model `claude-opus-5-5`) does the work. A throwaway Forgejo server
hosts the repository. The recorded outputs in `docs/demo/selfdemo/` were produced with Claude Opus 5.5 on every step, before the default changed to Claude Opus 5.5 for routine steps and Claude Fable 5.2 (`claude-fable-5-2`) for planning, building and review.

## What happens

**Setup.** The script starts Forgejo, makes a public repository, copies the Notes app, writes its
layer into `agentic/` ([`layer.rkt.in`](layer.rkt.in): the web demo's layer, placed in the repository),
generates it, commits everything and pushes. `al check` passes and `git status` is clean.

**The layer protects itself.** First, an ordinary workflow tries to change the layer: a scripted
build step (no real agent here) edits the planning prompt in `agentic/` along with an app file. The
workflow is the zero-touch `feature` workflow, which would normally merge on its own. It does not:
the gate stops it with `protected-path:agentic/commands/plan-chore.md`, and the change goes to a
person. Only al's own layer-change workflow may change the layer. This runs in a throwaway clone.

**Change 1, a change to the definition.** The request: *"I want to queue work in a text file: add a
task-file prompt input and a poller trigger, and let the ui-change workflow accept them."*
`al improve agentic "…"` runs al's layer-change workflow. In a private worktree an agent plans the
smallest change, makes it in `agentic/agentic.rkt`, and then al's checks run, each with a chance for
the agent to fix what failed:

1. the definition compiles;
2. `al gen` regenerates the layer without overwriting any hand edit, and a second `al gen` changes nothing;
3. only files of the layer changed, never the app;
4. `al check` passes;
5. every workflow of the changed layer completes a dry run (stand-in agent, real tests and browser run) on a clean copy of the app;
6. `al verify` runs any codebase command whose definition changed (none here).

The result is a pull request on Forgejo. Its description is written for someone who does not read
Racket: the request, **what changes for you**, the plain-language explanation of every part of the
layer that changed (before and after), the files that changed and the checks. A labelled `HUMAN STEP`
merges it after the script has taken a screenshot of the page. Because the layer is checked in, the
pull request already contains the regenerated files: after merging, `al gen` changes nothing.

Then the new ability is used, for free: a task appended to `agentic/tasks.md` is picked up by the
poller and run by `ui-change` with the stand-in agent.

**Change 2, a change to a prompt.** The request: *"Every plan must end with a 'Rollback' section that
says exactly how to undo the change."* Same workflow, same kind of pull request; this time the
interesting part is the prompt itself, so the description shows the changed planning prompt as a diff.
After the merge, **one real agent step** checks the effect: the planning step of `ui-change` runs
for real on a small request ("Make the label of the Add button bold") and its plan must end with a
Rollback section.

Throughout, the script checks that the application's working tree stays clean: runs keep their state
in ignored directories of `agentic/`.

## Run it

```bash
tests/selfdemo/selfdemo_live.sh                 # about 25 minutes
AL_KEEP=1 tests/selfdemo/selfdemo_live.sh       # keep the temp dir for inspection
AL_SELFDEMO_DRY=1 tests/selfdemo/selfdemo_live.sh   # free rehearsal: a scripted stand-in agent
```

You need `podman`, `uv`, `racket`, `curl`, `jq`, `git`, `ffmpeg`, `google-chrome`, a logged-in Claude
CLI on the host (`~/.claude/.credentials.json`), and the images `codeberg.org/forgejo/forgejo:11` and
`docker.io/library/python:3.12-slim`. The container image `al-notes-demo:latest` is shared with the web
demo and built on first use.

`al improve` runs its agent on the host, in a git worktree, with the layer's own agent declaration;
`bypassPermissions` (allowed for that agent only inside its container) becomes `acceptEdits`. It does
not use a container because its checks need `racket`, `uv`, the `al` tools and `podman` itself.

`AL_SELFDEMO_ONLY=1` or `=2` runs one change. Nothing is copied to `docs/demo/selfdemo/` unless both
changes ran with the real agent and every check passed. In the dry rehearsal the forge is never used
(dry runs never open pull requests), so the assertions about the pull request fail by design, and the
"merge" is a local one.

**Cost and time.** See `summary.json` in `docs/demo/selfdemo/` for the recorded run: seconds, cost,
agent steps, pull request numbers and check attempts per change, and the record of earlier attempts.

## The output files (`docs/demo/selfdemo/`)

| File | What it is |
|---|---|
| `N-request.txt` | The request given to `al improve` |
| `N-run.log` | The run's log (temp paths replaced with `<tmp>`, the repository path with `<repo>`) |
| `N-pr-page.png` | The pull request page in real Chrome |
| `N-pr-explain.png` | A readable crop: "What changes for you" and the explanation before → after |
| `N-layer-diff.patch` | The change to `agentic/agentic.rkt` and `agentic/templates/` only |
| `1-task-poll.log` | The poller picking up a task from `agentic/tasks.md` (stand-in agent) |
| `2-plan-with-rollback.md` | The plan the real agent produced after change 2 was merged |
| `protection-run.log` | The ordinary workflow that tried to change the layer and was stopped |
| `summary.json` | Seconds, cost, agent steps, PR numbers, check attempts, the protection result, earlier attempts |
