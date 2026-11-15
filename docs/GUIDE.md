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

You should see the path of the new folder, `~/code/myapp.al`, containing two files:

- `LLM.txt`: a draft description of your codebase, made by a script from facts it can prove (manifests,
  lock files, CI config). Lines it could not establish are marked `UNVERIFIED`.
- `AL-DSL.llms.txt`: the complete reference of the layer language, for the LLM that will write your layer.

The command also prints the relative path from the layer to your codebase. You need it in step 3.

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
- **Protected paths:** CI config, migrations, lock files, licence, security-sensitive code. Add what the agent missed.
- **Conventions:** the rules you would tell a new colleague. One line each.
- **`UNVERIFIED` lines:** fill in what you know. Leaving a duration unknown is fine.

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
env/        the container recipe (and the Nix file, if you chose Nix)
tasks.md    the task list (if you chose it)
README.md   a table of your workflows and how to run them
```

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

## Step 8 — Check, then rehearse for free

```bash
al check ~/code/myapp.al                      # the layer is complete and your repository is untouched
cd ~/code/myapp.al
uv run adws/adw_plan-build.py "Add a note to the README about running the tests" --dry-run
```

`--dry-run` uses a stand-in instead of a real agent: no model is called and nothing is charged. You
should see each step start and pass, ending with `status: passed`. It proves the plumbing: the private
copy of your repository is created, the steps run in order, state and logs land in `agents/<run id>/`.
The workflow names are in the layer's `README.md`; yours may differ from `plan-build`.

## Step 9 — The first real run

Pick something small and well defined: a missing test, a documentation fix. Write the request as you
would for a colleague, with acceptance criteria if there is any room for interpretation.

```bash
cd ~/code/myapp.al
uv run adws/adw_sdlc.py "Add a test that <specific behaviour>. Acceptance: <what must be true>."
```

Watch the step lines. A typical run takes two to five minutes. At the end:

- **With a forge:** a pull request, with the plan, the check results and any proof attached.
- **Without one:** a branch in your repository and `agents/<run id>/PR.md` with the same text.
  Look at the change with `git -C ~/code/myapp log -p <branch>`.

Your checkout is exactly as you left it (`git -C ~/code/myapp status`). Merge the branch or the pull
request if you are happy.

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

<!-- CUSTOMISE -->

## When something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| `al gen` says a template uses an unknown placeholder | a hand-written template has a typo in `{{…}}` | fix the name; use only the placeholder names that appear in the kit's templates (`kit/templates/`) |
| a run waits with `waiting for a free runs slot` | the concurrency limit is reached | wait, or raise `#:max-runs` |
| `status: failed` and a `[needs human]` pull request | the run stopped and handed over | read "Why this stopped" in it |
| `git-integrity` failure | something in the container altered git metadata it should not | inspect `trees/<run id>`; nothing was shipped |
| the agent cannot edit files or run commands | permission mode or allowed tools too narrow | adjust `#:permission-mode` / `#:allowed-tools` on the agent |
| leftover branches or copies after experiments | runs you interrupted | `git -C ~/code/myapp worktree prune`, then delete the branches |
| a container image is stale after you changed the recipe | images are built once and reused | `podman rmi al-<layer name>:latest`, then run again |

Every run leaves `agents/<run id>/`: `state.json` (what happened), `run.log`, each step's exact prompt
and raw agent output, and each check's log. Start there.

## What it costs

From the real runs in this repository, with Claude Opus 5.5: a small change with tests costs about
$0.50 to $0.70 and takes two to three minutes; a feature with a recorded video and two reviewers costs
$1.30 to $2.40; a change that is rejected, fixed and reviewed again about $3. Dry runs, `al verify`
and `al check` cost nothing.
