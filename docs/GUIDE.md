# Guide: set up and use `al` on your own codebase

This guide takes you from a repository with nothing agent-specific in it to agents shipping reviewed
changes on it. Each step says what to run, what you should see, and what to do when it goes wrong.
If you have not read the [README](../README.md) yet, read "The idea" there first; it takes two minutes.

**What you will have at the end:** a folder (the *agentic layer*) holding prompts written for your
codebase, workflows that plan, build, test, review and ship, a sandbox for agents to work in, and a way
to hand them work. Your repository's own files are not changed by any of this.

## Before you start

You need:

| Tool | Why | Check |
|---|---|---|
| `git` | everything | `git --version` |
| [Racket](https://racket-lang.org) (tested with 9.x) | the layer language and compiler | `racket --version` |
| [`uv`](https://docs.astral.sh/uv/) | runs every script; no virtualenv to manage | `uv --version` |
| a coding agent CLI: Claude Code (`claude`) or Codex (`codex`) | the agent that does the work | `claude --version` |
| `podman` (optional, recommended) | containers for agents and checks | `podman --version` |
| `ffmpeg` (optional) | only if you want video as proof | `ffmpeg -version` |
| `nix` (optional) | only if you want a Nix toolchain shell | `nix --version` |

Get `al` and make the command available:

```bash
git clone <this repository> ~/tools/al
alias al=~/tools/al/tools/al          # or add ~/tools/al/tools to your PATH
al --help
```

Your codebase must be a git checkout with at least one commit. Throughout the guide it is
`~/code/myapp`; replace that with your path.

## Step 1 — Create the layer folder

```bash
al init ~/code/myapp
```

To keep the layer inside your repository instead, versioned with the code, use
`al init ~/code/myapp --in-repo`. It creates `~/code/myapp/agentic`. Every later command then takes
`~/code/myapp/agentic` where this guide writes `~/code/myapp.al`, and scripts are run from the repository
root as `uv run agentic/adws/…`. Step 14 explains what is committed and the one rule that comes with it.

You should see the path of the new folder, `~/code/myapp.al`, containing two files (three with
`--in-repo`: a `.gitignore` as well):

- `LLM.txt`: a draft description of your codebase, made by a script from facts it can prove (manifests,
  lock files, CI config). Lines it could not establish are marked `UNVERIFIED`.
- `AL-DSL.llms.txt`: the complete reference of the layer language, for the LLM that will write your layer.

The command also prints the relative path from the layer to your codebase. You need it in step 4.

The draft records your repository's web address, without any user name or token.

## Step 2 — Describe the codebase

The draft is not good enough to build on: it has guesses and gaps. An LLM completes it by reading your
repository, and must cite a file for every command it lists.

Open your coding agent **in the `al` repository** (`cd ~/tools/al && claude`) and give it this, with your paths:

```text
Follow meta/commands/scout-codebase.md with
$1 = ~/code/myapp
$2 = ~/code/myapp.al/LLM.txt
The codebase is read-only: do not change anything in it and do not run its build.
```

Then read `LLM.txt` yourself. It is the one document everything else depends on, and it is short. Check:

- **Commands:** is each one what you actually run? Is anything missing (a type checker, an end-to-end suite)?
  A repository with several suites gets one row per suite, with names of your choosing (`test-unit`,
  `test-api`). The names in the template are suggestions.
- **Protected paths:** CI config, migrations, lock files, licence, security-sensitive code. Add what the agent missed.
- **Conventions:** the rules you would tell a new colleague. One line each. Rules that are written in
  no file are yours to add; say so under Evidence ("stated by the maintainer"), and consider putting
  them in a CONTRIBUTING file so the next scout finds them.
- **`UNVERIFIED` lines:** fill in what you know. Leaving a duration unknown is fine.

**If your repository already has an agent setup** (a `CLAUDE.md` or `AGENTS.md`, prompts under
`.claude/commands/`, an `adws/` folder, agent steps in CI), `LLM.txt` has an `## Existing agent setup`
table listing it. Check its "in use?" column, then bring it into the layer:

```bash
al adopt ~/code/myapp.al --app ~/code/myapp --dry-run     # shows what it would do, changes nothing
al adopt ~/code/myapp.al --app ~/code/myapp
```

Rule files become documents every agent must read; your prompts become commands of the layer, as they
are; your workflow scripts become proposed workflows that a person reviews. It never changes your
repository. Read `adopted/REPORT.md`, especially "For a person to decide": where an existing rule
contradicts a built-in prompt, it shows both texts and leaves the choice to you. If you run `al adopt`
before `agentic.rkt` exists, run it again after step 4.

*If it goes wrong:* the agent invents a command. Delete the row. A command that is not written down in
your repository does not belong in `LLM.txt`.

## Step 3 — Decide how you want agents to work

Every workflow answers four questions. Decide your answers in plain words; you do not need to know the syntax.

| Question | Common answers | Say this |
|---|---|---|
| Where does work come from? | a text file of tasks · issues on GitHub or Forgejo · the command line · spec files · voice | "tasks from a text file" |
| What starts a run? | you, by hand · a poller · a webhook · the forge's CI | "a poller" |
| Where does the agent work? | a private copy of the repo · plus a container · plus a Nix shell | "in a container" |
| Who decides it is done? | you, via a pull request · an agent reviews and you merge · automatic, under hard rules | "I review a pull request" |

**Start conservative:** tasks from a text file or the command line, a container, and you reviewing a
pull request (or a branch, if you have no forge). You can loosen it later, one step at a time.

One exception to "a container": if your tests themselves start containers or drive a browser, run the
checks on your machine first. A container in which those tests are skipped proves less.

## Step 4 — Have the layer written

In the same agent session (still in the `al` repository):

```text
Follow meta/commands/author-layer.md with
$1 = ~/code/myapp.al/LLM.txt
$2 = ~/code/myapp.al/agentic.rkt
$3 = Preferences: tasks from a text file and the command line; started by hand or by a poller;
     agents work in a git worktree inside a podman container; a human reviews; no forge.
     The codebase root, relative to the layer folder, is ../myapp
```

If the layer is in your repository, the last line of the preferences is instead
`The layer is in the repository: (codebase #:root "..") and (layer in-repo #:dir "agentic")`.

The agent writes `agentic.rkt` and compiles it. If the compiler rejects it, the error names the line and
lists the valid choices, and the agent fixes it. When it reports success, check for yourself:

```bash
al compile ~/code/myapp.al > /dev/null && echo "compiles"
```

*If it goes wrong:* the error is always of the form `agentic.rkt:LINE:COL: … expected one of: …` or
`… is not a declared …; declared: …`. Give it to the agent with
`Follow meta/commands/repair-layer.md with $1 = ~/code/myapp.al/agentic.rkt  $2 = <the error text>`.

## Step 5 — Read what was written

```bash
al explain ~/code/myapp.al
```

This prints the layer back in plain language, block by block: the commands, where work comes from,
where agents work, who decides, and each workflow's steps. It includes the defaults the author did not
write down. If a sentence there is not what you want, that is the block to change in `agentic.rkt`.
For a document and an animation: `al explain ~/code/myapp.al --md layer.md --gif layer.gif`.
The seven example layers are explained this way in [`docs/layers`](layers/README.md).

## Step 6 — Generate the layer

```bash
al gen ~/code/myapp.al
```

You should see `generated N files`. The folder now contains:

```
commands/   the prompts, with your commands, paths and rules filled in
hops/       prompts that take a plan or another prompt as input
adws/       one runnable script per workflow, plus triggers
env/        the container recipe and the Nix file (only if you chose a container or Nix)
tasks.md    the task list (if you chose it)
README.md   a table of your workflows and how to run them
```

Also: `experts/` (if you declared any), `templates/` (your own prompt templates, step 14),
`agentic-layer.json` (the compiled definition), `conditional_docs.md` (generated from the documents you
told agents to read), and a live dashboard under `adws/`.

Open one prompt, for example `commands/plan-chore.md`, and read it as if you were the agent. It should
mention your real commands and paths. If it reads as generic, your `LLM.txt` conventions were thin; add
them and repeat steps 4 to 6.

## Step 7 — Prove the commands are true

The compiler proves the layer is consistent. It cannot know whether `make test` really works in the
container the layer describes. This step can:

```bash
al verify ~/code/myapp.al              # every command, on a clean throwaway copy, in the layer's environment
al verify ~/code/myapp.al --cmd test   # one command
```

The clean copy is made from your default branch (`#:default-branch` in `agentic.rkt`, `main` unless you
say otherwise), not from whatever you have checked out. Commit and merge what you want checked first.

Expect the first run to be slow: it builds the container image and downloads your dependencies.
You should end with `verify: N/N ok`. Do not skip this step. On the example repositories it found
mistakes in layers that compiled perfectly.

*If it goes wrong:*

| Symptom | Usual cause | Fix |
|---|---|---|
| `command not found` in the container | the image lacks a tool | add it to the podman option's `#:packages` or `#:setup` in `agentic.rkt`, then `al gen` |
| cannot write to `.git`, or `git submodule` fails | the container sees your repository's git data read-only, by design | change the command so it does not need to write there |
| a test cannot bind a port that is busy on your machine | it ran on the host | run checks in a container: each run has its own network |
| needs the network but has none | the podman option has `#:network none` | remove it, or add a `#:caches` entry and warm the cache |
| Nix takes minutes or stalls | fetching nixpkgs | set `#:nixpkgs` to a local or pinned flake reference |
| a command from your README fails on the clean copy although it works for you | your checkout has files the clean copy lacks (build output, caches), or the documented command was never run as written | find the command that works, put it in `LLM.txt` with its citation, and correct the document |
| a command passes one time and fails the next | a test that depends on timing | do not use it as a check: an agent would be sent to "fix" it. Declare a deterministic variant, and fix the test |

## Step 8 — Check, then rehearse for free

```bash
al check ~/code/myapp.al                      # the layer is complete; your own uncommitted work is only noted
cd ~/code/myapp.al
uv run adws/adw_plan-build.py "Add a note to the README about running the tests" --dry-run
```

`--dry-run` uses a stand-in instead of a real agent: no model is called and nothing is charged. You
should see each step start and pass, ending with `status: passed`. It proves the plumbing: the private
copy of your repository is created, the steps run in order, state and logs land in `agents/<run id>/`.
The workflow names are in the layer's `README.md`; yours may differ from `plan-build`.

## Watch the work

Every generated layer comes with a live dashboard. Start it before your first real run and leave it open:

```bash
cd ~/code/myapp.al
uv run adws/dashboard.py --open            # http://127.0.0.1:8765/
```

It shows what is running now, each workflow as a graph that colours in as steps finish, where the time
and the money went, and every agent step's transcript in readable form, with filters and search. A
check's log can be read while the check is still running. A run whose process was killed is shown as
"died", which is different from "failed". The page cannot start, stop or change a run. Its one write is
the Models panel (see [Choose models and limit spending](#choose-models-and-limit-spending)), which
changes the layer's local model setting; start it with `--read-only` to turn that off too.

To watch several codebases at once: `al dashboard ~/code` (finds every layer under that folder;
`al dashboard ~/code --read-only` for a page that changes nothing). To run it in its own locked-down
container, declare `(dashboard podman)` in `agentic.rkt`; to turn it off, `(dashboard none)`. It listens
on your own machine only; exposing it elsewhere requires naming a token variable with `#:token-env`.

## Step 9 — The first real run

Pick something small and well defined: a missing test, a documentation fix. Write the request as you
would for a colleague, with acceptance criteria if there is any room for interpretation.

```bash
cd ~/code/myapp.al
uv run adws/adw_sdlc.py "Add a test that <specific behaviour>. Acceptance: <what must be true>."
```

Watch the step lines. A run takes the agent's time (two to four minutes for a small change) plus your
checks, and the build step runs the plan's validation commands once itself before the checks run them
again. With a five-minute test suite, expect ten to fifteen minutes. Keep `#:validation` on the planning
commands to your fast commands.

Like `al verify`, a run starts from your default branch, not from what you have checked out. If you are
on another branch, the run's branch will not be based on it: take the change with `git cherry-pick`, or
merge after your branch has caught up. At the end:

- **With a forge:** a pull request, with the plan, the check results and any proof attached.
- **Without one:** a branch in your repository and `agents/<run id>/PR.md` with the same text.
  Look at the change with `git -C ~/code/myapp log -p <branch>`.

Your checkout is as you left it (`git -C ~/code/myapp status`). Merge the branch or the pull request if
you are happy. If the workflow has a `document` step, what it wrote appears in the pull request (or
`PR.md`) under "Documentation".

**If the run stops** (a check will not go green, or the agent says something needs a person), it does
not just fail: it opens a pull request marked `[needs human]` (or writes `PR.md`) saying why it stopped
and what it asks you to do.

*Where does the agent itself run?* By default on your machine, in the private copy, with permission to
edit files and run commands there. To put the agent inside the container as well, the image must
contain the agent CLI. This is the recipe the examples use for Claude Code:

```racket
(agent claude-code #:id claude-sandboxed #:permission-mode bypassPermissions)
(environment podman #:base-image "docker.io/library/python:3.12-slim"
             #:packages ("git" "curl" "ca-certificates")
             #:setup ("HOME=/opt/claude bash -c 'curl -fsSL https://claude.ai/install.sh | bash' && chmod -R a+rX /opt/claude && ln -s /opt/claude/.local/bin/claude /usr/local/bin/claude")
             #:agent-inside #t #:credentials (claude))
```

and the workflow names that agent with `#:agent claude-sandboxed`. The compiler only accepts an agent
with all permissions when it runs inside a container.

## Choose models and limit spending

By default every step runs on Claude Opus 5.5: routine steps (classify, commit) and planning, building
and review alike. Claude Fable 5.2 is the opt-in for the heaviest work. Models are grouped in named tiers
on the agent, and prompts and steps pick a tier. To opt in for every machine, write it in the layer
(here with a cheaper `fast` tier for commits):

```racket
(agent claude-code #:models ((fast "claude-haiku-5-5") (base "claude-opus-5-5") (heavy "claude-fable-5-2" #:effort high)))
(command commit #:template commit #:model-set fast)
(review agent #:agent second-opinion)                    ; reviewers on a different agent
(budget limits #:run-usd 5 #:step-usd 2 #:day-usd 50)
```

Which tier a step uses, first match wins:

| | Source | Example |
|---|---|---|
| 1 | an override for this run | `--model-set heavy`, or `AL_MODEL_SET=heavy` |
| 2 | the step itself | `(run build … #:model-set fast)` |
| 3 | the request | a task tag `{heavy}`, or `adw: sdlc heavy` in an issue |
| 4 | the prompt | `#:model-set` on the command (default: `base` for commands, `heavy` for prompts that take a plan) |

Which model a tier means, first match wins:

| | Source | Example |
|---|---|---|
| 1 | a flag, for this run | `--model heavy=claude-fable-5-2` |
| 2 | an environment variable, for this run | `AL_MODELS="heavy=claude-fable-5-2,base=claude-opus-5-5"` |
| 3 | the local setting of this machine | `al models myapp.al --set heavy=claude-fable-5-2`, or the dashboard's Models panel |
| 4 | the layer | `(agent claude-code #:model-heavy "claude-fable-5-2")` or `#:models ((heavy …))` |
| 5 | the built-in default | `claude-opus-5-5` for `base` and `heavy` (codex: `gpt-5.4-mini`, `gpt-5.4`) |

`--max-cost 3` (or `AL_MAX_COST`) caps one run. The compiler rejects a workflow that could use a tier its
agent does not declare.

The local setting is for switching without editing `agentic.rkt` or typing flags, also for runs that
triggers start. It lives in `agents/settings/models.json`, which is never committed (`al check` reports
it as a problem if it is), and only changes which model id a tier means, never which tier a step uses
or its `#:effort`:

```console
$ al models ~/code/myapp.al --set heavy=claude-fable-5-2
base     claude-opus-5-5      (built-in default)
heavy    claude-fable-5-2     (local setting)
settings: ~/code/myapp.al/agents/settings/models.json (local to this machine, never committed); applies to runs started from now on
```

`al models LAYER` shows the table without changing anything, `--reset heavy` drops one tier's setting
and `--reset` all of them. A model id al does not know is accepted with a `note:` and passed to the
agent as is; a tier no agent declares is an error. The dashboard's Now view has the same switch: a
Models panel with one row per tier, its current model and where it comes from, a selector of the known
models (or a custom id) and a Reset button. It writes the same file in the same format.

Runs in flight keep the models they started with; fan-out candidates use their parent's. Every run
records which model each tier meant and why: the `tiers:` line of its summary, `models` in its
`state.json` and its `run.start` event, and the run's page in the dashboard.

A run that goes over a limit stops and hands over like any other stopped run: a `[needs human]` pull
request with the reason `budget:run: spent $X of $Y`, and the work so far kept. When today's limit is
reached, new runs refuse to start (exit code 3).

## Step 10 — Hand out work without typing commands

**A task file.** Add lines to `tasks.md` in the layer folder:

```markdown
## readme-note
- [ ] Add a note to the README about running the tests {sdlc}
- [>] Mention the lint command in that note {sdlc}
```

`[ ]` is ready, `[>]` waits for the task above it, and the tag in braces names the workflow. Then:

```bash
uv run adws/triggers/trigger_cron.py --once     # one pass
uv run adws/triggers/trigger_cron.py            # keep polling
```

Each task is claimed, run, and marked `[x]` with the run id, or `[!]` with the reason.
`uv run adws/triggers/trigger_cron.py --once --dry-run` rehearses this with the stand-in agent and charges nothing.

**Issues on a forge.** Declare `(forge github #:repo "you/myapp")` or
`(forge forgejo #:host "https://…" #:repo "you/myapp")`, a `(prompt-input issue)`, and either
`(trigger webhook)` (run `adws/triggers/trigger_webhook.py` and point the forge's webhook at it, with
the secret in the environment variable the layer names) or `(trigger forge-actions)`, which generates a
CI workflow file under `forge/` for you to copy into your repository. Runs comment on the issue when
they start and finish.

**Voice.** Declare `(voice kokoro)`, `(prompt-input voice)` and `(trigger voice)`, then run
`adws/triggers/trigger_voice.py`. Speech is recognised and spoken locally.

## Step 11 — Add proof

What a reviewer, human or agent, can judge depends on the evidence. Declare what your checks produce
and collect it on the check that produces it:

```racket
(artefact junit  #:glob "reports/*.xml")
(artefact image  #:id screenshots #:glob "e2e-results/*.png")
(artefact video  #:id recording   #:glob "e2e-results/*.webm" #:keyframes 8)
…
(check e2e #:cmd e2e #:resolve resolve-test #:collect (log screenshots recording))
```

For anything a user can see, add an end-to-end command to your codebase that drives the real
application and saves screenshots or a video; the two demo applications show one way
([`tests/webdemo/app/e2e`](../tests/webdemo/app/e2e)). On Forgejo the pictures are attached to the pull request.

What each kind of proof looks like, with real recordings you can play: [ARTEFACTS.md](ARTEFACTS.md).

## Step 12 — Let small things ship by themselves

Only after a streak of runs you merged without changes. A workflow may ship without you when its review is
`auto`, and the compiler insists on the safety net:

```racket
(review auto #:id zero-touch #:reviewers 2
        #:artefacts (log screenshots recording)
        #:require-checks (lint test e2e)          ; re-run on the final result
        #:allowed-classes (chore bug)             ; features still go to you
        #:max-diff-lines 300
        #:ship merge)
```

The workflow needs at least one check before its review, and a container in its environment. A change
then ships only if both reviewers approve **and** every rule holds; anything else becomes a pull
request for you, with the reasons listed. Before turning this on:

- [ ] `al verify` passes for every command in `#:require-checks`.
- [ ] Protected paths cover CI, migrations, permissions code, lock files and anything you would never
      want changed unseen.
- [ ] Your tests would fail if the thing you care most about broke. (For a multi-customer system:
      a test that proves one customer cannot see another's data.)
- [ ] You have looked at what the reviewers say on a few human-reviewed runs.

## Step 13 — Run several at once

Every run already has its own copy of the code. For more:

```racket
(concurrency slots #:max-runs 4 #:max-agents 2 #:max-checks 2)        ; shared by everything that starts runs
(service container #:id db #:image "docker.io/library/postgres:16-alpine"
         #:env ("POSTGRES_PASSWORD=dev") #:ready-cmd "pg_isready -U postgres")
(environment podman … #:services (db) #:publish (8000))               ; a database per run; port 8000 reachable from the host
```

Inside its container each run binds its usual ports; nothing collides. To race several agents on one
task and keep the best result, see `(fan-out …)` in the language reference.

## Step 14 — Change the layer later

You will want to change how agents work on your codebase: a new way to hand out work, a rule every
plan should follow, a stricter review. There are three ways, from most to least automatic. In all of
them the rule is the same: **change the definition or a template, never a generated file.**

### Ask for the change in words

```bash
al improve ~/code/myapp.al "Every plan must end with a 'Rollback' section that says exactly how to undo the change."
al improve ~/code/myapp.al "I want to queue work in a text file: add a task-file input and a poller." --dry-run
```

An agent plans the smallest change that satisfies the request, makes it in a private copy of the layer,
and six checks must pass: the definition compiles; it regenerates, and regenerating twice changes
nothing; only layer files changed; `al check` passes; every workflow still completes a dry run; and
any codebase command that changed is verified for real. The result is a pull request (or a branch and
`PR.md` without a forge) written for someone who does not read the layer language: the request, "What
changes for you", each affected block explained before and after, and the generated prompts that changed.

A person always decides. `--review agent` adds an agent reviewer before you; there is no automatic
mode for layer changes, and asking for one is refused. The layer must be committed on its default
branch first. `--dry-run` rehearses the mechanics with a stand-in agent.

After you merge: if the layer lives in your repository (see below), you are done, because the pull
request includes the regenerated files. Otherwise run `al gen ~/code/myapp.al`.

### Edit a prompt template yourself

```bash
al eject ~/code/myapp.al --list                    # every template, where it comes from, and whether it drifted
al eject ~/code/myapp.al command plan-chore        # copy that command's template into the layer
$EDITOR ~/code/myapp.al/templates/commands/plan-chore.md
al gen ~/code/myapp.al
```

A template in the layer's `templates/` folder wins over the built-in one and survives every
regeneration. Keep its `{{…}}` placeholders; they are how your commands and paths get filled in.
`--list` tells you when the built-in version has changed since you took your copy. For a one-line
instruction, a `#:focus` line on the command in `agentic.rkt` is simpler than a template. A focus line
now takes precedence over the format a template prescribes (a commit message, a pull request body, a
plan's sections). It cannot change what the tool itself reads back, such as a path or a JSON answer; for
anything beyond that, eject the template.

### If you edited a generated file by mistake

`al gen` will not silently throw your edit away. It stops, writes nothing, and lists each file that
was changed by hand together with the command that makes the change permanent:

```text
al gen: 1 generated file(s) in ~/code/myapp.al were edited by hand since the last `al gen`.
Generating now would overwrite those edits, so nothing was written:

  commands/plan-chore.md   al eject ~/code/myapp.al command plan-chore   (copies its template to templates/commands/plan-chore.md)

Two ways forward:
  1. Keep the change: make it in a layer-local template, which every `al gen` uses from then on. …
  2. Throw the edit away:  al gen ~/code/myapp.al --discard-edits
```

Either eject the template and move your edit into it, or discard the edit with
`al gen ~/code/myapp.al --discard-edits`. `al check` reports the same thing.

### Run a script you already have

An existing workflow script does not have to be rewritten. Declare it as a codebase command and call it
as a step:

```racket
(cmd legacy-release-notes "uv run scripts/release_notes.py")
…
(script notes #:cmd legacy-release-notes #:args ("$prompt"))
```

It runs in the run's private copy (and container, if there is one), receives the run's context in
`ADW_ID`, `ADW_PROMPT`, `ADW_CLASS` and `ADW_PLAN`, and fails the run if it exits non-zero. It is not a
check: the shipping rules never count it as verification.

### Keep the layer in your repository

Up to here the layer has been a folder beside your repository. Most teams will want it versioned with
the code, so that changes to it go through the same pull requests:

```bash
al init ~/code/myapp --in-repo            # creates ~/code/myapp/agentic
```

In `agentic.rkt`, the codebase root is then `".."` and the layer says where it lives:

```racket
(codebase #:root ".." …)
(layer in-repo #:dir "agentic")
```

Then `al gen ~/code/myapp/agentic`, commit the `agentic` folder, and run workflows from the repository:
`uv run agentic/adws/adw_sdlc.py "…"`. What is committed: the definition, your templates and the
generated prompts and workflows. What is ignored: everything a run produces (working copies, logs,
proof, the task list). Runs leave `git status` clean.

One rule comes with this. An ordinary workflow that changes anything under `agentic/` is stopped and
handed to a person, whatever its review setting: agents do not get to rewrite their own instructions
on the way past. Layer changes go through `al improve`.

## When something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| `al gen` says a template uses an unknown placeholder | a hand-written template has a typo in `{{…}}` | fix the name; use only the placeholder names that appear in the kit's templates (`kit/templates/`) |
| a run waits with `waiting for a free runs slot` | the concurrency limit is reached | wait, or raise `#:max-runs` |
| `status: failed` and a `[needs human]` pull request | the run stopped and handed over | read "Why this stopped" in it |
| `git-integrity` failure | something in the container altered git metadata it should not | inspect `trees/<run id>`; nothing was shipped |
| `al gen` stops with "edited by hand" | a generated file was changed directly | see step 14: eject the template, or `--discard-edits` |
| the agent cannot edit files or run commands | permission mode or allowed tools too narrow | adjust `#:permission-mode` / `#:allowed-tools` on the agent |
| leftover branches or copies after experiments | runs you interrupted | `git -C ~/code/myapp worktree prune`, then delete the branches |
| a run refuses to start with exit code 3 | today's spending limit is reached | raise `#:day-usd`, or wait until tomorrow |
| `al adopt` refuses to write | a file it wrote earlier was edited by hand | move your change into `agentic.rkt`, or `--force` |
| a container image is stale after you changed the recipe | images are built once and reused | `podman rmi al-<layer name>:latest`, then run again |

Every run leaves `agents/<run id>/`: `state.json` (what happened), `run.log`, each step's exact prompt
and raw agent output, and each check's log. Start there.

## What it costs

From the real runs in this repository (all measured with Claude Opus 5.5 on every step, before the
default for planning, building and review became Fable 5.2): a small change with tests costs about
$0.50 to $0.70 and takes two to three minutes; a feature with a recorded video and two reviewers costs
$1.30 to $2.40; a change that is rejected, fixed and reviewed again about $3. Every agent step costs
roughly $0.08 to $0.16 before it does anything, for reading its prompt, so a workflow with five agent
steps starts at about $0.60. On this repository itself, with five test suites as checks, one small
change took 12½ minutes and $1.01. Dry runs, `al verify` and `al check` cost nothing.
