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

al finds each of these programs in the same order everywhere: a setting that names it (`AL_RACKET`,
`AL_RACO`, `AL_UV`, `AL_GIT`, `AL_CLAUDE`, `AL_CODEX`, an absolute path), then `PATH`, then the usual
per-user install folders (`~/.nix-profile/bin`, `~/.local/state/nix/profile/bin`,
`/etc/profiles/per-user/$USER/bin`, `/nix/var/nix/profiles/default/bin`, `~/.local/bin`, `~/racket/bin`,
`/usr/local/racket/bin`, `/opt/racket/bin`). A setting is read from the environment, else from the layer's
`local.env`, `~/.config/al/<layer name>.env` or, for every layer, `~/.config/al/tools.env`:

```bash
mkdir -p ~/.config/al && echo "AL_RACKET=$HOME/racket/bin/racket" >> ~/.config/al/tools.env
```

A command whose prerequisite is missing stops before it does any work, with exit code 4 and one sentence
per missing tool (see [When al cannot find a tool](#when-al-cannot-find-a-tool-al-doctor)).

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
al adopt ~/code/myapp.al --app ~/code/myapp --dry-run --diff   # … and the unified diff of every file it would write
al adopt ~/code/myapp.al --app ~/code/myapp
```

Rule files become documents every agent must read; your prompts become commands of the layer, as they
are; your workflow scripts become proposed workflows that a person reviews. It never changes your
repository. Read `adopted/REPORT.md`, especially "For a person to decide": where an existing rule
contradicts a built-in prompt, it shows both texts and leaves the choice to you. If you run `al adopt`
before `agentic.rkt` exists, run it again after step 4.

**If your `adws/` (or `adw/`) scripts form a hand-built suite** (at least two of them import the same
package beside them, such as `adws/adw_modules/`), `LLM.txt` adds an `### Existing ADW suite` section
inside `## Existing agent setup`. It describes the suite as one unit: the shared runtime package and its
guards, the entry script and its subcommands, where the suite keeps its own run journal (and which
files it writes there), its model routing, wrappers and systemd units, and per script its title, usage
lines, options (`--adw-id`, `--resume`, `--no-wait`), numbered steps (code or LLM), the prompts it runs
(resolved to `.claude/commands/` files, or listed as unresolved), their schemas and its exit codes.
Each fact says how it was found (file, ast, docstring, comment, text scan, or "inferred from
prose"); what could not be found is listed under "unknown". Scout only reads the files: nothing is
imported or run. `al scout --json` has the same data under `existing_adw_suite`.

`al adopt` then proposes one runnable workflow per subcommand of the entry script (and one per
script no subcommand reaches), for example `legacy-adw-orbit-watch` for `adws/adw_orbit.py watch`.
Prompts that only the suite's own runtime reads stay where they are instead of being copied. The
report gains "Run an existing workflow" (the exact command, the resume command and what a person must
decide) and "Model routing" (the suite's tiers beside the layer's; al does not change them).

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

`al author ~/code/myapp.al --preferences "…"` does the same hand-off with the agent CLI (`claude`): it gives
the agent `meta/commands/author-layer.md` with the layer's `LLM.txt`, then compiles what it wrote. Without
`--preferences` it uses the conservative answers above and the codebase root beside the layer. It never
overwrites an existing `agentic.rkt`. It checks first that `racket` and the agent CLI are there, so a
missing one costs no agent time. `al author ~/code/myapp.al --accept-existing` takes an `agentic.rkt` that
is already there (say, one an earlier attempt wrote before the compiler could be found) when it compiles,
and asks no agent; one that does not compile is reported with the repair hint below.

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
"died", which is different from "failed". The page cannot stop or change a run. It makes two changes,
both guarded: the Models panel (see [Choose models and limit spending](#choose-models-and-limit-spending))
changes the layer's local model setting, and the Layers view starts work (see the next section). Start
it with `--read-only` to turn both off, or `--no-start` to turn off starting only.

A run's **Documents** tab lists the plan, `PR.md`, review reports and patch plans and other text the run
wrote, as soon as each exists, with the step that produced it. **View** shows it in the page (Markdown
rendered, or raw), **Download** saves the file, **Download all** saves them as one zip. To get the plan
while the run is still building: open the run, then Documents, then **Download** next to the plan (or
open the `plan-feature` step and follow its document link).

To watch several codebases at once: `al dashboard ~/code` (finds every layer under that folder;
`al dashboard ~/code --read-only` for a page that changes nothing). To run it in its own locked-down
container, declare `(dashboard podman)` in `agentic.rkt`; to turn it off, `(dashboard none)`. It listens
on your own machine only; exposing it elsewhere requires naming a token variable with `#:token-env`.

## Start work from the dashboard or the command line

The dashboard's **Layers** view has a **Start work** panel. Type what you want done, pick what runs it,
and press **Start** (or Ctrl+Enter); the page then opens the new run. What runs it is either:

- **a workflow** of the layer: any `adw` in `agentic.rkt`, adopted ones included; or
- **a single command**: one command or hop of the layer (`plan-chore`, `implement`, an adopted prompt),
  run as one step in the layer's normal environment (that of its first workflow). It is recorded like any
  run (state, events, transcript, documents, cost) and **never ships**: no review, no merge, no pull
  request. Use it to get only a plan, or to implement one plan you already have.

Pick it from the list, or type `/` at the start of the text: `/plan-c` completes to `/plan-chore`, and
`/plan-chore tidy the readme` starts that command with "tidy the readme" as its prompt. When a workflow
and a command share a name, `/NAME` is the workflow and `/command:NAME` the command. The panel says in one
line what the choice does (e.g. "plans, builds, checks test, two reviewers, merges without a person"). You
may fix the class (workflows only), put every step on one tier, and give a model id per tier for this run.

A workflow that ships without a person (an `auto` review with `#:ship merge` or `deploy`) asks for a
confirmation first. So does the same request (same workflow or command, same text) started again within
five minutes. Each form carries a request key: a double click or a retried request starts one run, not
two.

When the layer has a forge, the request can also be **logged as an issue**:

- **log it as an issue and start now**: the issue is created with a hidden marker
  (`<!-- al:claimed run=<id> -->`) and the run starts at once. The layer's poller and webhook ignore an
  issue with that marker, so it is never started a second time, whichever comes first.
- **create the issue and let the trigger start it** (offered for a workflow a cron or webhook trigger
  starts from issues): the issue gets the issue input's labels and an `adw: <workflow>` line, and nothing
  starts now; the layer's own trigger starts it once.

A start can be refused: when the layer's day limit or subscription usage threshold is reached (as for
any run), or when the dashboard runs with `--read-only` or `--no-start` (the panel says which). A
start when every run slot is taken is accepted and says it waits for a slot. A layer generated before
this feature shows "run `al gen`" instead of the panel. In the podman dashboard starting is off.

The same from a terminal, through the same code (the layer's `adws/adw_modules/launch.py`):

```bash
$ al run myapp.al /tidy remove the unused import --dry-run      # a single command, attached
run 93f3f743 started (layer: myapp, command tidy)
[adw 93f3f743] command:tidy start: class=chore dry_run=True …
…
=== ADW summary ===
adw:       command:tidy 93f3f743
status:    passed
…
plan:      myapp.al/specs/plan-93f3f743-tidy.md
…
ship:      none
…

$ al run myapp.al /change add a search command --detach         # a workflow, in the background
run 66ce8c25 started (layer: myapp, workflow change)

$ al run myapp.al /nope x
al run: no workflow or command named nope; known: /change, /plan, /tidy, /commit, /implement, /new-adw

$ al run myapp.al /tidy remove the unused import --dry-run      # again, within five minutes
al run: the same request to command tidy started run 93f3f743 6 s ago; pass --yes to start it anyway
```

The prompt is the rest of the line (or stdin with `-`). `--class`, `--model-set`, `--model TIER=ID` and
`--dry-run` work as for a workflow script; `--yes` confirms a shipping workflow or a repeated request;
`--request-key K` makes a retry safe. Exit codes: the run's own (0 with `--detach`), 2 for an unknown
name, 3 refused by a limit, 4 needs `--yes`. The prompt is written to `agents/<run id>/request.md` and
never put on a command line.

## Work with several repositories from the dashboard

If you keep your repositories side by side in one folder (`~/code/myapp`, `~/code/orbit`, `~/code/fern`),
the dashboard lists all of them. Start it for one layer:

```bash
al dashboard ~/code/myapp.al --open
```

and press `4` (**Repos**). Every git repository in the folder that holds `myapp` is listed with its
branch, uncommitted changes, last activity and its al state:

| State | Means |
|---|---|
| `layer` | it has a layer: an `agentic.rkt` in the repository, or a sibling `NAME.al/agentic.rkt` |
| `setup` | no layer, but an agent setup al can adopt: rule files, prompts, workflow scripts or a hand-built suite |
| `none` | neither |

The states come from the same detection as `al scout`, read-only: tracked files only, git run without
the repository's hooks or fsmonitor, nothing outside the folder (symlinks are not followed). The list is
rescanned every minute and with **refresh**. A layer that is generated is watched like any other, so
its workflows and runs appear in the Layers and Runs views and the repository's panel links to them.

Choose a repository (the address keeps it: `#/repo/orbit`). Its **al** panel offers the actions that fit
its state, each with the exact command line it runs:

| Action | For | Runs |
|---|---|---|
| scout | any | `al scout ~/code/orbit` (read-only; its output is the job's `LLM.txt`) |
| create | `none`, `setup` | `al init ~/code/fern` → `al author ~/code/fern.al` → `al gen ~/code/fern.al` → `al check ~/code/fern.al` |
| adopt | `setup` | first `al adopt ~/code/orbit.al --app ~/code/orbit --dry-run --diff`, shown as a diff; on Confirm `al init` → `al author` → `al adopt` → `al gen` → `al check` |
| gen | `layer` | `al gen LAYER` |
| check | `layer` | `al check LAYER` (read-only) |
| verify | `layer` | `al verify LAYER --json` (writes the report and a throwaway worktree) |
| explain | `layer` | `al explain LAYER --md …` (read-only; its output is the job's `EXPLAIN.md`) |

An action that writes asks first: it shows the command lines and the paths it writes (for adopt, the dry
run's diff) and runs only after **Confirm**; if the repository changed in between, it asks again. Create
and adopt never overwrite an `agentic.rkt`, wherever it is. Each action is a **job**: its steps with their
exit codes, the live output, the files of the layer it changed and its documents. One action runs per
repository at a time. A job keeps running when you restart the dashboard; a job whose process is gone
is shown as `interrupted`, never as running. Jobs are kept in `~/.local/state/al/dashboard-jobs`
(`$XDG_STATE_HOME`), outside every repository.

- `--workspace ~/code` (repeatable) lists that folder instead; `--workspace-depth 2` looks one level deeper;
  `--no-workspace` lists none.
- `--no-actions` lists the repositories but runs nothing; `--read-only` turns off every write.
- The actions run the al tool of the checkout the dashboard comes from (or `al` on `PATH`). Without one,
  every action is disabled and the panel says where it looked. The create and adopt actions run
  `al author`, which needs the agent CLI (`claude`).
- The panel's tools table is what `al doctor` sees on this machine: each tool, where it was found (or the
  sentence that says where al looked), and its version. A job runs with the folders of those tools in front
  of the dashboard's `PATH`, and checks its prerequisites (`al doctor LAYER --for …`) before its first step:
  a missing tool fails the job within a second, before any agent is asked.

When a job fails, the failed step shows why (the `al: …` lines it printed, or its exit code), and so does
the job. A failed or interrupted job offers **Retry from STEP**: after you confirm, the steps from the one
that failed onwards run again in the same job; the steps that passed, their times and the output stay
(the output gets a `--- retry from STEP ---` line). A retried `author` step runs with `--accept-existing`,
so an `agentic.rkt` the first attempt wrote is taken when it compiles instead of asking the agent again.

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

## How work is delivered

Without anything declared, every run starts from `(codebase #:default-branch …)` and ships into it (or hands a
branch to you). If your team collects work somewhere else first, a sprint branch, `develop`, a release branch,
say so with one line. The run's private copy then starts from that branch, and its ship step delivers into it.
`al scout` proposes the line from the history it finds (see the end of this section).

Each **stage** is a branch (or a branch pattern) where work collects: the stage it is cut from, the stage it
goes into once accepted, which kinds of work it takes, whether agents may merge into it, and how changes enter
it. A **preset** writes the stages for you. These are the five, with what each stands for:

**Trunk** — everything goes straight into `main`:

```racket
(delivery trunk)
;; stands for:
(stage main #:branch "main" #:entry squash)
```

**Sprints** — work collects in a branch per sprint; you accept the sprint into `main`:

```racket
(delivery sprint #:length-days 14)
;; stands for:
(cycle sprint #:stage sprint #:length-days 14 #:name "{year}-s{week}")
(stage sprint #:branch "sprint/{cycle}" #:from main #:accepts (feature bug chore patch) #:entry merge)
(stage main #:branch "main" #:accepts () #:agents-may-merge #f)
```

`al explain` says:

```
How work is delivered
  Work is delivered the sprint way through 2 stages: sprint/{cycle} → main. The sprint stage repeats
  every 14 days (cycles named {year}-s{week}): al cycle LAYER start opens the next branch, al cycle
  LAYER close closes one. Agents may not merge into main: a person takes the work there.
```

and for each workflow, where it delivers: `It delivers into sprint (sprint/{cycle}).`

A sprint branch is opened, and closed, with `al cycle`. Opening one creates the branch from `main` with
`git branch`; nothing is checked out and nothing is pushed unless you add `--push`:

```
$ al cycle ../shop.al start
cycle: sprint 2026-s47 opened: branch sprint/2026-s47 from main at e745d25
$ al cycle ../shop.al status
cycle: sprint 2026-s47 open since 2026-11-16 (14 days, 14 left)
branch: sprint/2026-s47 (from main at e745d25)
ahead of main: 0 commits · behind main: 0 commits
$ al cycle ../shop.al close
cycle: sprint 2026-s47 closed (not merged into main; a person accepts it)
next: a person merges sprint/2026-s47 into main (git merge --no-ff sprint/2026-s47)
```

`al cycle … start` while a sprint is open closes it first. Closing never merges: accepting a sprint is your
step. A run started while no sprint is open is refused before it does anything
(`refused: delivery:no-open-cycle`).

**Gitflow** — work collects in `develop`; release branches per version, hotfix branches from `main`:

```racket
(delivery gitflow)
;; stands for:
(cycle release #:stage release #:name "{version}")
(stage develop #:branch "develop" #:from main #:accepts (feature bug chore patch) #:entry merge)
(stage release #:branch "release/{version}" #:from develop #:into main #:accepts () #:agents-may-merge #f)
(stage hotfix #:branch "hotfix/{date}" #:from main #:into main #:accepts () #:agents-may-merge #f)
(stage main #:branch "main" #:accepts () #:agents-may-merge #f)
```

A release branch is opened with `al cycle LAYER start --version 1.4`.

**Release train** — a train branch that leaves on a schedule (`#:length-days 0`: one per version):

```racket
(delivery release-train)
;; stands for:
(cycle sprint #:stage train #:length-days 14 #:name "{date}")
(stage train #:branch "train/{cycle}" #:from main #:accepts (feature bug chore patch) #:entry merge)
(stage main #:branch "main" #:accepts () #:agents-may-merge #f)
```

**Phase gates** — work enters the first phase; you promote it through the others:

```racket
(delivery phase-gate)
;; stands for:
(stage build #:branch "phase/build" #:from main #:into verify #:accepts (feature bug chore patch))
(stage verify #:branch "phase/verify" #:from main #:into accept #:accepts () #:agents-may-merge #f)
(stage accept #:branch "phase/accept" #:from main #:into main #:accepts () #:agents-may-merge #f)
(stage main #:branch "main" #:accepts () #:agents-may-merge #f)
```

### Add or change a stage

A `(stage NAME …)` with the name of one of the preset's stages replaces it; any other name adds one. An
urgent route for bugs, which a person merges:

```racket
(delivery sprint #:length-days 14)
(stage hotfix #:branch "hotfix/{date}" #:from main #:into main #:accepts (bug) #:agents-may-merge #f
       #:environment-name "production")

(adw hotfix #:description "Urgent bugs: straight to a hotfix branch, a person merges"
     #:review human #:classes (bug) #:deliver-to hotfix
  (steps (compose plan-build) (check test #:cmd test) (run commit #:command commit) (review) (ship)))
```

A workflow delivers to its `#:deliver-to` stage, else to the first stage that takes its class, else to the
final stage. A run whose class changes on the way (the classify step says `bug`, which goes elsewhere) is
handed to you instead of delivered to the wrong branch.

### What the compiler refuses

- A zero-touch workflow (`review auto` with `#:ship merge` or `deploy`) whose work would go to a stage only
  people may change:
  `(adw zte): review `zero-touch` ships without a human, but class feature is delivered to stage `main` (branch main), where agents may not merge; deliver to a stage with #:agents-may-merge #t (e.g. #:deliver-to sprint) or use a human or agent review`.
- Stages that do not lead to one final stage, two final stages, a final stage whose branch is not
  `#:default-branch`, two stages on one branch, an invalid branch name or an unknown placeholder.
- `#:entry fast-forward` with `(forge github …)`, which cannot merge a pull request that way.
- `#:deliver-to` without any stage, or to a stage that does not take the workflow's classes.

### When the stage moved meanwhile

Before shipping, the run looks at the stage branch again. If others delivered into it in the meantime, their
work is merged into the run's copy and checked with it (`delivery.stale` in the run's events). If it
conflicts, the review's patch commands get one round to resolve it, the checks run again and the reviewers
look again; without `#:patch`/`#:implement` on the review step, or if the conflict stays, the run is handed
to you (`delivery:conflict`).

Without a forge, changes enter a stage by merge; a stage with `#:entry squash`, `rebase` or `fast-forward`
is handed to you until shipping supports it (`delivery:entry-unsupported`). Through a forge, the pull request
is merged with the stage's method.

### Let `al scout` propose it

`al scout` reads the branch names, the merges on the default branch, tags, CI triggers and the contribution
docs, and writes a `## Delivery` section into the `LLM.txt` draft:

```
## Delivery

- Style: sprint
- Proposed preset: `(delivery sprint #:length-days 14)`
- Default branch: main
- What speaks for it:
  - 7 sprint branches: sprint/2026-s01, sprint/2026-s02, …
  - cut every 14 days from main (median gap)
```

With no signal it proposes `(delivery trunk)`, marked UNVERIFIED. `al scout APP --layer LAYER` also asks the
layer's forge which branches are protected. It only reads: nothing in the repository changes.

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

A script that may have to wait for something outside (a review queue, a deploy) can say so with an exit
code, and a script that keeps its own run journal can have it followed. This is what `al adopt` proposes
for the watch command of a hand-built suite:

```racket
(cmd legacy-adw-orbit-watch "uv run adws/adw_orbit.py watch ${ADW_RESUME:+--resume \"$ADW_ID\"}")
…
(script run #:cmd legacy-adw-orbit-watch #:args ("--adw-id" "$adw_id")
            #:waiting-exit (75)
            #:journal ".work/adw/{adw_id}")
```

- `#:waiting-exit (75)`: when the script exits 75 the run ends **waiting**, not failed. It exits 75
  itself, keeps its worktree, sends no failure notification and prints how to resume it:
  `uv run adws/adw_legacy-adw-orbit-watch.py --resume --adw-id <id>`. The resumed run runs that step
  again with `ADW_RESUME=1` in its environment (empty otherwise), so the command line above passes
  `--resume <id>` on to the script. An exit 75 that is not declared still fails the run.
- `#:journal ".work/adw/{adw_id}"`: a folder, relative to the run's worktree, where the script writes
  its own journal (`{adw_id}` is the run id). While the script runs, each line of its `events.jsonl` is
  mirrored into the run's events, and each inner step's `prompt.md` and output files are copied into
  the run as documents. A missing folder is not an error.

On the dashboard the run shows as `waiting` (⏸) with its resume command, and the script step lists the
journal's inner steps with their time, tokens, cost and documents.

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

## When al cannot find a tool: `al doctor`

`al doctor` shows where al finds each program it runs, and its version:

```bash
al doctor ~/code/myapp.al
```

```
tool    found where                              version
racket  ~/racket/bin/racket (~/racket/bin)       Welcome to Racket v9.0 [cs].
raco    ~/racket/bin/raco (beside racket)
uv      ~/.local/bin/uv (PATH)                   uv 0.9.2
git     /usr/bin/git (PATH)                      git version 2.47.0
claude  ~/.local/bin/claude (PATH)               2.1.0 (Claude Code)
```

With a layer its settings count too (`local.env`, `~/.config/al/<name>.env`) and the agent CLIs are the
ones its workflows use; without one, `claude`. `--json` prints the same rows as a document
(`{"ok": …, "tools": [{"tool", "found", "path", "source", "version", "looked", "sentence"}]}`).
`--for gen,author --agent mock` checks only what those subcommands need and prints `ok`, or one sentence
per missing tool. It exits 4 when a tool is missing; every other subcommand that finds a prerequisite
missing stops the same way, before any work:

```
al: author: racket is missing: al looked at AL_RACKET (not set), PATH, ~/.nix-profile/bin, ~/.local/state/nix/profile/bin, /etc/profiles/per-user/$USER/bin, /nix/var/nix/profiles/default/bin, ~/.local/bin, ~/racket/bin, /usr/local/racket/bin, /opt/racket/bin; install Racket (https://racket-lang.org) or set AL_RACKET=/path/to/racket in the layer's local.env or ~/.config/al/tools.env.
```

A setting that names something that is not an executable file is reported as such; al never falls back
to another copy behind it.

**On a deployed host** (`al deploy`), the same search runs on the host: a setting in
`~/.config/al/tools.env` (or `<name>.env`) there, the login `PATH`, the per-user folders. The units'
`Environment=PATH=` holds the folder of each tool it found, then the login `PATH`, so a unit finds `uv`
and the agent CLI even when only an interactive shell would. Racket is required on the host only when the
dashboard unit is installed and the host has an al tool (the dashboard's actions compile layers there);
otherwise it is `not needed`. A missing tool fails the deploy with its preflight row, before anything is
changed. The paths are recorded in `~/.config/al/<name>-tools.json` on the host, and
`al deploy LAYER --to HOST --status` lists the tools now and at the deploy, with a line such as
`drift: uv moved: was …, now …; run al deploy again so the units' PATH follows` or
`drift: claude vanished: was …`.

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
| an `al` command exits 4 with `… is missing: al looked at …` | a prerequisite (racket, uv, git, the agent CLI) is not where al looks | install it, or name it in `~/.config/al/tools.env`; `al doctor` shows what al sees |
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
