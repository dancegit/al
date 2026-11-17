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

You should see the path of the new folder, `~/code/myapp.al`, containing three files (four with
`--in-repo`: a `.gitignore` as well):

- `LLM.txt`: a draft description of your codebase, made by a script from facts it can prove (manifests,
  lock files, CI config). Lines it could not establish are marked `UNVERIFIED`.
- `scout.json`: the same facts as data (commands, forge, languages, existing agent setup), without any
  absolute path, so the layer can be committed. Step 2 adds the measured commands; step 4 builds the
  author's preferences from it.
- `AL-DSL.llms.txt`: the complete reference of the layer language, for the LLM that will write your layer.

The command also prints the relative path from the layer to your codebase. You need it in step 4.

The draft records your repository's web address, without any user name or token, and the forge behind
it: `forgejo` when the repository tracks a `.forgejo/` or `.gitea/` folder (also on a self-hosted server
whose name does not say so) or the host is a Forgejo, Gitea or Codeberg one; `github` for a GitHub host;
`gitlab` for a `.gitlab-ci.yml`; `unknown` for a web address without such evidence; `none` without a
remote. `## Forge` in `LLM.txt` shows the host and the evidence that decided it.

**Steps 1 to 6 in one command.** `al create ~/code/myapp` runs the whole sequence the dashboard's Create
runs (see "Work with several repositories from the dashboard"): `init`, `scout` (step 2's measuring and
scout agent), `adopt` (a no-op when there is nothing to adopt), `author` (step 4), `gen`, `check`, and a
result card. `al adopt-all ~/code/myapp` is the same for a repository with an agent setup of its own; it
refuses one without (`use al create`). Each step prints `== STEP` first; the first that fails stops the
sequence and keeps what the steps before it wrote. `al create ~/code/myapp --choices` prints the choices
step 4 will make (with `--json`: as data) and writes nothing; `--choice KEY=VALUE` changes one (step 4).
The steps below are what it does, one at a time.

## Step 2 — Describe the codebase

The draft is not good enough to build on: it has guesses and gaps. First measure what the script found:

```bash
al scout ~/code/myapp --into ~/code/myapp.al
```

It runs each detected command once, in a throwaway worktree of your repository's `HEAD` under
`~/code/myapp.al/trees/` (removed afterwards), with a time limit (`--timeout S`, default 600 s), and
records its exit code and duration in `scout.json` and in the `LLM.txt` draft's Commands table. A command
that needs the network is not run unless you add `--network`. Your checkout is never changed:

```text
scout: running 3 detected command(s) once in a throwaway worktree (600s each at most)
  skip  install  not run: needs network (--network runs it)
  ok    build  ok  0.1s
  ok    test  ok  0.1s
```

A command that failed, timed out (`timed out after 600s`) or did not run stays `UNVERIFIED`, with the
reason; its output is in `~/code/myapp.al/agents/scout/<name>.log`. Then the same command hands
`meta/commands/scout-codebase.md` to the agent CLI (`claude`), which reads your CI files for work classes
and artefacts and completes `LLM.txt` (it runs without your personal setup and may write only in the layer
folder: see step 4). `--agent mock` skips that part. `LLM.txt` is rewritten only while it is still the
untouched draft; one you or the agent completed is kept.

To complete it by hand instead, an LLM reads your repository, and must cite a file for every command it
lists. Open your coding agent **in the `al` repository** (`cd ~/tools/al && claude`) and give it this, with your paths:

```text
Follow meta/commands/scout-codebase.md with
$1 = ~/code/myapp
$2 = ~/code/myapp.al/LLM.txt
$3 = ~/code/myapp.al/scout.json
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
contradicts a built-in prompt, it shows both texts and leaves the choice to you. Run it before step 4, as
`al create` does: `al author` then tells the agent to include what was adopted. A repository with nothing
to adopt gets one line and no `adopted/` folder:

```text
nothing to adopt: no existing agent setup found in /home/you/code/myapp
```

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

**`## Environment`** says what your repository already declares about where it runs, read from tracked
files and never executed: a flake's dev shells, `shell.nix` or devenv, a Containerfile's stages, a dev
container, compose services, versions in `.tool-versions`, `mise.toml`, `.nvmrc`, `.python-version`,
`rust-toolchain`, `pyproject.toml`, `package.json` and `go.mod`, lockfiles, browser test dependencies, the
packages and services your CI installs, and the variables a run needs (names only). Where two files
disagree it lists a conflict. It ends with a proposed declaration, every line citing its file:

```racket
(cmd prepare-deps "bun install --frozen-lockfile" #:description "dependencies resolve") ; from bun.lock

(environment worktree #:prepare (prepare-deps)) ; from bun.lock
(environment nix-devshell #:flake "." #:shell "default" #:browser #t) ; from flake.nix:14, package.json
```

The first line goes inside `(codebase …)`, the others at the top level. Check the conflicts: each
`;; conflict:` line is a decision for you (which Node version does the run use?). `al scout --json` has
the same data under `environment`.

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

`al author ~/code/myapp.al` does the same hand-off with the agent CLI (`claude`): it gives the agent
`meta/commands/author-layer.md` (inlined, with the layer's own `AL-DSL.llms.txt`) and the layer's
`LLM.txt`, then compiles what it wrote. The preferences come from the facts in `scout.json`, not from a
fixed sentence: the forge the remote and CI folders show (with `#:host` and `#:repo`), issues as input
and webhook and manual triggers when there is a forge (a task list, cron and manual without one), podman
when it is found on this machine, the delivery preset the history shows. These choices win over the
decision table of `author-layer.md`. Every choice the facts are silent on falls back to a default, and
`al author` names each one:

```text
default: environment = worktree (facts are silent)
default: review = human (facts are silent)
default: delivery = none (facts are silent)
set FORGEJO_TOKEN in the environment (or /home/you/code/myapp.al/local.env) before a run talks to the forge; al never reads or stores it
```

A token is never read: the forge clause names the variable (`FORGEJO_TOKEN`, `GITHUB_TOKEN`) that will
hold it. A GitLab repository gets `(forge none)` with the reason (GitLab is planned in the language).
`--choice KEY=VALUE` (repeatable) changes one choice: `forge` (none, github, forgejo), `host`, `repo`,
`input` (tasklist, issue), `trigger` and `environment` (comma-separated lists), `review` (human, agent),
`delivery` (a preset, or none); an unknown key or value stops it with the list of what is allowed. Each
decision, with its fact or its default, is written to `agents/author/choices.json`, and the whole prompt
to `agents/author/prompt.txt`. `--preferences "…"` replaces the preferences with your own text. It never
overwrites an existing `agentic.rkt`.

The agents al starts (the scout agent of step 2, the author, and the workflow `al improve` runs) work
without your account's personal setup: no user settings, hooks, `CLAUDE.md`, plugins or MCP servers (a
clean `CLAUDE_CONFIG_DIR` that holds only a link to your credentials file, `--setting-sources project`,
`--strict-mcp-config`), and the scout and author agents may edit and write only inside the layer folder.
To give them your personal setup back, set `AL_AGENT_PERSONAL=1` in the environment,
`~/code/myapp.al/local.env` or `~/.config/al/tools.env`; the write limit stays. Any other agent al starts
in the future gets the same isolation (`agent_isolation` in `tools/al_tools.py`). It does not keep out
project-level `CLAUDE.md` files in the folders above the layer. It checks first that `racket` and the agent CLI are there, so a
missing one costs no agent time. `al author ~/code/myapp.al --accept-existing` takes an `agentic.rkt` that
is already there (say, one an earlier attempt wrote before the compiler could be found) when it compiles,
and asks no agent; one that does not compile is reported with the repair hint below.

While the agent works, `al author` prints one line per message, tool call and error (`  · read LLM.txt`,
`  ! Bash failed`, `  = 14 turns, 3m 04s, $0.42`), never the hooks' output, and at the end the agent's
report once, followed by its **Decisions** and **Left out and why** lists. `al author … --raw` prints
Claude Code's event stream itself, line by line. A dashboard job runs the author step (and the scout step) with `--raw` and
shows the same condensed view on its page ("Work with several repositories from the dashboard" below).

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

### Reuse the environment the repository already describes

If your repository already has a flake dev shell, a Containerfile or a dev container, the layer uses it
instead of a retyped copy (the proposal in `LLM.txt` does this for you):

| You have | Declare | What `al gen` writes |
|---|---|---|
| `flake.nix` with `devShells` | `(environment nix-devshell #:flake "." #:shell "default")` | `env/flake.nix`, which wraps your dev shell (`inputsFrom`) and adds only `#:packages` and `#:browser` |
| a `Containerfile` / `Dockerfile` | `(environment podman #:containerfile "Containerfile" #:target "dev" #:context ".")` | a thin `env/Containerfile` on top of your image: the run user's home, `#:packages`, `#:setup`, a browser, the agent CLI when it runs inside; the entrypoint is reset |
| `.devcontainer/devcontainer.json` | `(environment podman #:devcontainer ".devcontainer/devcontainer.json")` | the same thin layer on its `image` or `build`; known features as packages, `containerEnv`, `forwardPorts` as published ports, `postCreateCommand` as the prepare step |

Your files are read from the default branch and never edited. Each run builds your Containerfile from
its own worktree (`localhost/al-<layer>-base`) when it changed, and the thin layer on top as
`al-<layer>:<recipe hash>`. That hash also covers your Containerfile and its context on the default
branch (or the dev container's image), so a change there gives a new image at the next run; old ones
are pruned by `al sandbox LAYER prune`. A flake is refreshed at the start of every run, so a run uses the
default branch as it is then. `env/SOURCE.md` says what was reused and what was not (dev container
feature versions, `remoteUser`, `mounts`, `runArgs`, lifecycle commands other than `postCreateCommand`,
`customizations`); `al gen` prints the same as `note:` lines. A dev container made of compose services is
refused: use `#:containerfile` with its Dockerfile, or `#:base-image`. Compose services are proposed as
`(service container …)` declarations; al never starts them from your compose file.

Dependency installs get a place of their own: `(environment worktree #:prepare (install))` runs the
`install` cmd once per run, after the worktree exists and before the first step, in the run's environment
(a container's caches are mounted). The dashboard shows it as a `setup` step; if it fails, the run ends
at once with its output. `#:browser #t` on a nix-devshell or podman option adds a headless Chromium and
sets `AL_BROWSER` and `CHROME_BIN`.

Limits: `al sandbox LAYER build` and `push` need the base image a run or `al verify LAYER` built first
(they say so when it is missing); the flake wrapper carries your shell's inputs and `shellHook` but not plain attributes set
on the shell derivation.

## Step 7 — Prove the commands are true

The compiler proves the layer is consistent. It cannot know whether `make test` really works in the
container the layer describes. This step can:

```bash
al verify ~/code/myapp.al              # every command, on a clean throwaway copy, in the layer's environment
al verify ~/code/myapp.al --cmd test   # one command
al verify ~/code/myapp.al --workflow sdlc   # only in the environment stack of workflow sdlc
```

Workflows can run in different environments (one on the host's worktree, another in a container). By
default every distinct environment stack is verified once, on its own clean copy, and with more than one
each result line ends with the stack it ran in (`ok    test  exit 0  3.1s  [worktree → box]`).

The clean copy is made from your default branch (`#:default-branch` in `agentic.rkt`, `main` unless you
say otherwise), not from whatever you have checked out. Commit and merge what you want checked first.

Expect the first run to be slow: it builds the container image and downloads your dependencies.
You should end with `verify: N/N ok`. Do not skip this step. On the example repositories it found
mistakes in layers that compiled perfectly.

Just before that line verify says what the layer's checks declare to cover (see "Run only the
checks a change needs" under Step 12). A layer that declares nothing prints one line:

```text
coverage: no check declares #:covers, so every check runs for every change
verify: 1/1 ok; report ~/code/myapp.al/agents/verify/report.json
```

One that does lists the checks without a declaration and the source paths (`(paths #:source …)`) no
check covers, or says that nothing is left over:

```text
coverage: 1 check declares no #:covers and runs for every change: lint
coverage: source path lib is covered by no check: a change there runs every check
```

```text
coverage: every check declares #:covers and every source path is covered
```

These lines never change the exit code, and with `--json` they go to standard error.

*If it goes wrong:*

| Symptom | Usual cause | Fix |
|---|---|---|
| `command not found` in the container | the image lacks a tool | with a toolchain, add its nixpkgs name to the toolchain's `#:packages` or use a `#:features` bundle; without one, add it to the podman option's `#:packages` or `#:setup` in `agentic.rkt`; then `al gen`; `al sandbox LAYER doctor` lists every missing tool at once |
| cannot write to `.git`, or `git submodule` fails | the container sees your repository's git data read-only, by design | change the command so it does not need to write there |
| a test cannot bind a port that is busy on your machine | it ran on the host | run checks in a container: each run has its own network |
| needs the network but has none | the podman option has `#:network none` | remove it, or add a `#:caches` entry and warm the cache |
| Nix takes minutes or stalls | fetching nixpkgs | set `#:nixpkgs` to a local or pinned flake reference |
| a command from your README fails on the clean copy although it works for you | your checkout has files the clean copy lacks (build output, caches), or the documented command was never run as written | find the command that works, put it in `LLM.txt` with its citation, and correct the document |
| a command passes one time and fails the next | a test that depends on timing | do not use it as a check: an agent would be sent to "fix" it. Declare a deterministic variant, and fix the test |

### The container: `al sandbox`

With a podman environment, the layer's image is named after its recipe: `al-<layer>:<hash>`, where the
hash covers the generated `env/Containerfile`, the files it copies and the base image. Change a package
or a `#:setup` line, run `al gen`, and the next run builds the new image by itself; with no change
nothing is built. Every run also checks its container before its first step (the *preflight*) and stops
at once, with a table of what is missing, instead of an hour later inside a step. `al sandbox` does the
same on demand. For a small layer named `notes` (a podman option `box`, a `test` command that needs
pytest):

```text
$ al sandbox notes.al status
box  recipe f79af0fccebd  not built         -     -  al-notes:f79af0fccebd

$ al sandbox notes.al build
[sandbox] building al-notes:f79af0fccebd (recipe f79af0fccebd)
STEP 1/5: FROM docker.io/library/python:3.12-slim
STEP 2/5: RUN apt-get update && apt-get install -y --no-install-recommends git curl
...
built al-notes:f79af0fccebd in 0s, 312 MB

$ al sandbox notes.al doctor
doctor: workflow change, stack worktree → box, image al-notes:f79af0fccebd
check        | result | detail                                    | fix
command:test | miss   | pytest not found on PATH                  | add the package that provides `pytest` to (environment podman #:id box #:packages …) or #:setup, or change (cmd test …)
command:lint | ok     | /usr/local/bin/ruff                       |
git          | ok     | rev-parse and status work in the worktree |
init         | ok     | catatonit                                 |
cache:uv     | ok     | /caches/uv is writable                    |
disk         | ok     | 41210 MB free                             |
agent        | ok     | /usr/local/bin/claude (on this machine)   |
image        | ok     | al-notes:f79af0fccebd, 312 MB             |
doctor: 7 ok, 1 missed, 0 skipped
```

- `status` — the recipe hash and image of every podman environment (they share one image), and the
  old images, marked `(in use)` while a run or a container still uses them.
- `doctor [--env ID] [--workflow NAME]` — starts a throwaway container exactly as a run starts it (same
  mounts, sidecars, init) on a clean copy of your default branch, checks what the workflow needs (each
  command's program, the agent CLI and its credential when the agent runs inside, git, caches, disk, a
  browser when the workflow collects screenshots or videos, the sidecars) and exits 1 if anything is
  missing. Credential values are never printed. Before its last line (`doctor: N ok, N missed, N skipped`)
  it prints the same `coverage:` lines as `al verify` (checks without `#:covers`, source paths no check
  covers); they do not change the exit code.
- `shell [--env ID]` — a shell in such a container, to try a command the way a run would; the copy and
  the container are removed when you leave it.
- `build [--no-cache]` — build now, with the output as it happens (otherwise the next run does it).
  `--no-cache` also picks up a newer base image under the same tag.
- `update` — with a toolchain: re-pin nixpkgs in `env/flake.lock` and show what changed (see
  [Choose where the tools come from](#choose-where-the-tools-come-from)).
- `prune` — remove every old image nothing uses. Runs keep the two newest old images and remove those
  older than 14 days by themselves.

A workflow that collects no screenshots does not need a browser; one whose environment has no sidecars
skips that check. To turn the browser or sidecar check off for an environment anyway, give the podman
option a `preflight` list without it (see `docs/DESIGN.md`, "Sandbox image").

The compiler also warns (`warning: …` from `al compile`, `al gen` and `al check`) about declarations that
have no effect: an environment no workflow uses, `#:credentials` without `#:agent-inside #t`,
`#:publish`/`#:services` with `#:scope command`, and a nix dev shell whose packages the container of the
same workflow does not provide.

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
"died", which is different from "failed". The page cannot stop or change a run. It makes three changes,
all guarded: the Models panel (see [Choose models and limit spending](#choose-models-and-limit-spending))
changes the layer's local model setting, the Layers view starts work (see
[Start work](#start-work-from-the-dashboard-or-the-command-line)), and a run page can settle a finished
run (see the next section). Start it with `--read-only` to turn them all off, or `--no-start` to turn off
starting only.

A run's **Documents** tab lists the plan, `PR.md`, review reports and patch plans and other text the run
wrote, as soon as each exists, with the step that produced it. **View** shows it in the page (Markdown
rendered, or raw), **Download** saves the file, **Download all** saves them as one zip. To get the plan
while the run is still building: open the run, then Documents, then **Download** next to the plan (or
open the `plan-feature` step and follow its document link).

To watch several codebases at once: `al dashboard ~/code` (finds every layer under that folder;
`al dashboard ~/code --read-only` for a page that changes nothing). To run it in its own locked-down
container, declare `(dashboard podman)` in `agentic.rkt`; to turn it off, `(dashboard none)`. It listens
on your own machine only; exposing it elsewhere requires naming a token variable with `#:token-env`.

## Settle what is done: `al runs` and `al settle`

The dashboard's **Runs** view opens on **Needs you**: only the runs that still need someone, each with
why and what to do next. The "N need you" in its header, and the count on the Runs link, are that list.
A run leaves it when its work is done, even when nobody told al:

- **Landed.** A handed-over run whose work is on its target branch: its branch was merged, squash-merged,
  or brought in with other changes (every file it changed is the same on the target). The dashboard
  works this out from git, read only and without fetching, and says how ("landed in 1a2b3c4 (patch-id)").
  If you merged on the forge and have not fetched, `al runs … --forge` asks the forge as well.
- **Superseded.** A later run that landed and named the run's branch or id in its request, or contains its
  branch, settles it as "superseded by" that run.
- **Replaced.** A failure that was started again ("Start again" on the run page, **Start all again** on
  a group of failures with the same cause, `al run … --replaces OLD`, or the task reopened in a task list
  or issue) reads "replaced by" the new run.
- **Settled by you.** Anything else you decide needs nothing more: the run page's **Settle this run**
  card, or `al settle`.

The chips **Running**, **Landed**, **Settled** and **All** show the rest, each with what landed or settled
it. Nothing is deleted. The same list in a terminal:

```bash
$ al runs orchard.al
6 need you (5 running, 8 landed, 7 settled; --view all for every run)
a0000001  waiting      plan-build      Order new rootstock once the budget i…  waiting at approve
          → uv run adws/adw_plan-build.py --resume --adw-id a0000001
b000000e  handed-over  plan-build      Track the fig trees, part 14            review-rejected
          → review PR #7
b000000d  handed-over  plan-build      Track the medlar trees, part 13         review-rejected
          → merge al/b000000d into main, or al settle orchard.al b000000d --as …
b000000c  handed-over  plan-build      Track the quince trees, part 12         review-rejected
          → merge al/b000000c into main, or al settle orchard.al b000000c --as …
b000000b  handed-over  plan-build      Track the cherry trees, part 11         review-rejected
          → merge al/b000000b into main, or al settle orchard.al b000000b --as …
a0000002  failed       plan-build      Draw the orchard map                    build: the map renderer needs a page size
          → al run orchard.al /plan-build --replaces a0000002 < orchard.al/agents/a0000002/request.md

$ al settle orchard.al b000000c --as abandoned --note "the map work replaces it" --who Ada
settled b000000c as abandoned by Ada: agents/b000000c/settlement.json

$ al runs orchard.al --view settled
8 settled (5 need you, 5 running, 8 landed, 8 settled)
d0000005  failed       plan-build      Add the pruning calendar, part 5        replaced by e0000005 (running)
d0000004  failed       plan-build      Add the pruning calendar, part 4        replaced by e0000004 (running)
d0000003  failed       plan-build      Add the pruning calendar, part 3        replaced by e0000003 (running)
d0000002  failed       plan-build      Add the pruning calendar, part 2        replaced by e0000002 (running)
d0000001  failed       plan-build      Add the pruning calendar, part 1        replaced by e0000001 (running)
b000000c  handed-over  plan-build      Track the quince trees, part 12         abandoned by Ada: the map work replaces it
b000000a  handed-over  plan-build      Track the plum trees, part 10           superseded by c0000001
b0000009  handed-over  plan-build      Track the pear trees, part 9            superseded by c0000001
```

`--view needs-you|running|landed|settled|all` picks the list and `--json` prints it as JSON. Several
failures at the same step with the same reason, within half an hour of each other, show as one line
("5 runs: preflight, same reason") followed by their ids. `al settle LAYER RUN --as
landed|superseded|abandoned` takes `--by RUN|COMMIT` (a run of the layer or a commit of the application),
`--note TEXT` (at most 500 characters) and `--who NAME` (default: your `git config user.name` in the
application). It writes `agents/<run id>/settlement.json` and nothing else; settling again keeps the
earlier record under `previous`. Exit codes: 0 written, 2 bad use, 3 refused (the run is still running, or
`--by` names neither a run nor a commit), 4 no such run.

Finished runs leave worktrees, containers and branches behind. `al runs LAYER prune --landed --older-than
DAYS` removes them for runs in the Landed or Settled list that ended more than DAYS ago, and keeps the
runs' records (`agents/<id>`, `artefacts/<id>`); see first with `--dry-run`:

```bash
$ al runs orchard.al prune --landed --older-than 3 --dry-run
b000000c  settled: abandoned by Ada: the map work replaces it
  would remove branch al/b000000c (was a320f39)
b000000a  settled: superseded by c0000001
  would remove branch al/b000000a (was a852ef4)
…
b0000001  landed: landed in fc68848 (ancestor) on main, 2026-11-02
  would remove branch al/b0000001 (was 738b20d)
would prune 11 runs; agents/<id> and artefacts/<id> are kept
```

It never deletes the target branch, a stage branch, the default branch, or a branch checked out in
another worktree, and it notes what it removed in the run's `settlement.json`.

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

A single command, like `plan-build` and any workflow without a ship step, never commits or ships. Its
work is not thrown away when the run ends: before the worktree is removed, what the agents left in it is
committed on the run's branch (`work of run <id>, kept because the workflow has no ship step`) and copied
into the run's artefacts. The summary of a run that is not a `--dry-run` says so:

```
kept:      branch chore/5e2a01c4-remove-unused-import (1 files)
delivery:  none (no ship step); work kept on branch chore/5e2a01c4-remove-unused-import
```

or `delivery:  nothing changed` when the run left nothing (its branch is deleted as before). On the
dashboard, the run page's **Kept on a branch** block links each kept file (`artefacts/<id>/kept-files/…`)
and the whole change (`artefacts/<id>/kept-diff/changes.diff`), so they stay readable after the branch is
gone. Files over 1 MB are on the branch only and listed as too large. `al check` warns about such
workflows, and `al explain` says where their result goes.

The prompt is the rest of the line (or stdin with `-`). `--class`, `--model-set`, `--model TIER=ID` and
`--dry-run` work as for a workflow script; `--yes` confirms a shipping workflow or a repeated request;
`--request-key K` makes a retry safe. Exit codes: the run's own (0 with `--detach`), 2 for an unknown
name, 3 refused by a limit, 4 needs `--yes`. The prompt is written to `agents/<run id>/request.md` and
never put on a command line. To start a failed run again under a new id, use `al run … --replaces OLD`, or
**Start again** on its run page (which reuses its recorded request): the new run records that it replaces
OLD, and OLD leaves the runs that need you. "Start again" of a run that came from an issue restarts from
the recorded prompt, not from the issue.

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
| create | `none`, `setup` | first `al create ~/code/fern --choices --json` (the detected choices); on Confirm `al init ~/code/fern` → `al scout ~/code/fern --into ~/code/fern.al` → `al adopt ~/code/fern.al --app ~/code/fern` → `al author ~/code/fern.al` → `al gen` → `al check` → `al result ~/code/fern.al --json` |
| adopt | `setup` | first `al adopt ~/code/orbit.al --app ~/code/orbit --dry-run --diff`, shown as a diff, and the detected choices; on Confirm the same seven steps as create |
| gen | `layer` | `al gen LAYER` |
| check | `layer` | `al check LAYER` (read-only) |
| verify | `layer` | `al verify LAYER --json` (writes the report and a throwaway worktree) |
| explain | `layer` | `al explain LAYER --md …` (read-only; its output is the job's `EXPLAIN.md`) |

Create and adopt are `al create` and `al adopt-all` (step 1): the same steps in the same order, so the
author sees what was adopted. An action that writes asks first: it shows the command lines and the paths
it writes (for adopt, the dry run's diff) and runs only after **Confirm**; if the repository changed in
between, it asks again. The start form of create and adopt shows "detecting choices…" and then each
choice step 4 will make (forge, forge host, repository, where work comes from, what starts it,
environment, review, delivery preset) with the fact that supports it, or "default: facts are silent".
Change any of them before you confirm: a changed one is marked "changed", and the job's `author` step
gets it as `--choice KEY=VALUE`. Create and adopt never overwrite an `agentic.rkt`, wherever it is. Each
action is a **job**. Its page shows, in this order: the outcome once it ended — for a passed create or
adopt, the **result card**: the layer, its forge clause, its workflows, what was adopted (rule files,
commands, suite workflows) and left behind, what is unverified, the commands that failed or did not run,
and what to do next (the token variable to set for a forge, `al verify`, the adoption report; `al result
~/code/fern.al` prints the same card); for a failed job the failing step with its reason in one
sentence, **Retry from STEP** and **Download log**; the steps, the running one saying what it is doing
right now ("reading the CI files", "compiling, attempt 2"); the selected step's activity (an agent
step's messages — the scout's or the author's, the author by default — one line per tool call, errors in
red; hooks and bookkeeping only with **show everything**; a plain step's own output), with a search box
and its log, raw events and journal to download; the author's **Decisions** and **Left out and why**; the
files of the layer it changed and its documents; and, collapsed, the full output of every step. On the
command line, `al author` and `al scout --into` print the same condensed view (progress lines, then the
report); `--raw` prints the agent's event stream instead. One action runs per
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
and what it asks you to do. A strong model can stand in for you at that moment and propose (or take)
the next step: see [Let a stand-in decide when a run stops](#let-a-stand-in-decide-when-a-run-stops).

*Where does the agent itself run?* By default on your machine, in the private copy, with permission to
edit files and run commands there. To put the agent inside the container as well, the image must
contain the agent CLI. The simplest way is a toolchain with the `agent-cli` feature (next section):

```racket
(toolchain nix #:id dev #:packages ("python312" "uv" "git") #:features (agent-cli))
(agent claude-code #:id claude-sandboxed #:permission-mode bypassPermissions)
(environment podman #:toolchain dev #:agent-inside #t #:credentials (claude))
```

and the workflow names that agent with `#:agent claude-sandboxed`. The compiler only accepts an agent
with all permissions when it runs inside a container. The Debian form still works if you prefer it; then
you install the CLI yourself with a `#:setup` line, e.g. the vendor's installer:

```racket
(environment podman #:base-image "docker.io/library/python:3.12-slim"
             #:packages ("git" "curl" "ca-certificates")
             #:setup ("HOME=/opt/claude bash -c 'curl -fsSL https://claude.ai/install.sh | bash' && chmod -R a+rX /opt/claude && ln -s /opt/claude/.local/bin/claude /usr/local/bin/claude")
             #:agent-inside #t #:credentials (claude))
```

### Choose where the tools come from

An environment gets its tools in one of three ways:

| You want | Declare | Notes |
|---|---|---|
| the agent and checks on your machine, with a reproducible set of tools | `(environment nix-devshell #:toolchain dev)` (or `#:packages (…)`) | needs Nix on your machine; no container |
| a container whose tools are pinned and match the dev shell | `(toolchain nix #:id dev …)` and `(environment podman #:toolchain dev)` | Nix on the host is optional: without it the image is built in a builder container |
| a container from a Debian image | `(environment podman #:packages ("git" …) #:setup (…))` | apt is not pinned; you write the install lines |

A toolchain is declared once, and the dev shell, the container and the Home Manager module that name it
all get exactly that set, from one generated file `env/toolchain-dev.nix`:

```racket
(toolchain nix #:id dev
  #:packages ("python312" "uv" "git")          ; nixpkgs attribute names
  #:features (browser agent-cli)              ; ready-made bundles, see AL-DSL.llms.txt "Toolchain features"
  #:env (("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD" "1")))
(environment nix-devshell #:toolchain dev)
(environment podman #:id sandbox #:toolchain dev #:setup ("true"))   ; #:setup lines become a last layer
(environment home-manager #:toolchain dev)
```

`browser` adds Chromium with fonts and the variables that make Playwright and Puppeteer use it;
`agent-cli` adds the CLI of every agent that runs inside an environment using the toolchain; `video`,
`build-essentials`, `docker-cli` and `podman-cli` add what their names say. `al gen` pins nixpkgs in
`env/flake.lock`, which is committed:

```
generated 61 files in /home/you/code/myapp.al (61 changed)
lock: env/flake.lock written (no Nix on this host: built in the builder container docker.io/nixos/nix@sha256:b49cb33f…, store volume al-nix-store-b49cb33f3a7e)
```

The image is the layer's one image, named after its recipe like every sandbox image
(see [The container: `al sandbox`](#the-container-al-sandbox)): `al-<layer>:<hash>`. With a toolchain the
hash also covers `env/flake.lock` and the toolchain's declaration, so a changed lock or declaration is
built at the next run and an unchanged one never is. `al sandbox LAYER build` builds it now, says how it
built and what each feature costs:

```
$ al sandbox ~/code/myapp.al build
sandbox: al-myapp:4be1c07a92d3 from toolchain `dev` (built with Nix on this host)
  base        412.3 MiB
  browser     604.8 MiB
  agent-cli   221.0 MiB
  image       1.1 GiB
report: agents/sandbox/dev.json
$ al sandbox ~/code/myapp.al build
sandbox: al-myapp:4be1c07a92d3 is up to date (toolchain dev, lock and declaration unchanged)
```

`al verify` and every run's preflight build it too, before anything runs in the container; `doctor`,
`shell`, `status`, `prune`, `push` and `pull` work as for any sandbox image, and the doctor's `fix`
column names the toolchain. `#:nested-engine #t` needs the `#:packages` form and cannot be combined with
`#:toolchain`. To move to newer packages, re-pin and rebuild:

```
$ al sandbox ~/code/myapp.al update
sandbox: env/flake.lock re-pinned (built with Nix on this host)
  nixpkgs_dev: 3f0b4e1 (2026-09-30) → 9c2d7a4 (2026-11-12)
run `al sandbox /home/you/code/myapp.al build` to rebuild
```

Without Nix on your machine both commands run Nix in a builder container pinned by digest, with its
store in a podman volume named after that digest (`al-nix-store-…`). It sees only the layer's `env/`
directory. The volume is never cleaned up for you: `podman volume rm al-nix-store-<…>` frees the space.
`AL_SANDBOX_BUILD=nix` or `=container` forces one way.

The Home Manager module evaluates the same toolchain against your own nixpkgs: the same package names, at
your pin's versions. Claude Code is unfree; the module's header says what to allow.

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
        #:require-checks (lint test e2e)          ; proven on the final result (a pass on the same tree is reused)
        #:allowed-classes (chore bug)             ; features still go to you
        #:max-diff-lines 300
        #:ship merge)
```

The workflow needs at least one check before its review, and a container in its environment. A change
then ships only if both reviewers approve **and** every rule holds; anything else becomes a pull
request for you, with the reasons listed.

A review whose reviewers refuse in every round ends `rejected`
(`step review (review) rejected: fail after N round(s): …`), and the run still reaches its ship step,
where the gate hands the work to you. Such a run, or any run whose gate fell back, ends
`status: handed-over` with exit 0. Its summary prints the review line
(`review:    rejected (auto) after 3 round(s), 2 blocker(s)`) and a `handoff:` line with the reasons.
It sends `needs-human` notifications, and the dashboard shows it in amber, not as passed.

Before turning this on:

- [ ] `al verify` passes for every command in `#:require-checks`.
- [ ] Protected paths cover CI, migrations, permissions code, lock files and anything you would never
      want changed unseen.
- [ ] Your tests would fail if the thing you care most about broke. (For a multi-customer system:
      a test that proves one customer cannot see another's data.)
- [ ] You have looked at what the reviewers say on a few human-reviewed runs.

### Checks that need a container engine

Integration tests that build or start containers skip inside an ordinary sandbox: it has no
container engine. The check still exits 0 and the gate would count it as green. Two things fix that.

**Give the sandbox an engine of its own**, and make a skip for that reason fail the check:

```racket
(environment podman #:id box #:packages ("git" "python3-pytest") #:nested-engine #t)
(adw zte #:environment (worktree box) #:review zero-touch
  (steps (run plan #:command plan #:args ("$prompt"))
         (check test #:cmd test)
         (check engine-test #:cmd engine-test #:forbid-skip-reason "no podman")
         (review) (ship)))
```

`al gen` adds the engine to the image and writes its configuration next to the Containerfile:

```text
$ cat notes.al/env/Containerfile
# Generated by al from agentic.rkt — do not edit.
FROM docker.io/library/debian:stable-slim
RUN apt-get update && apt-get install -y --no-install-recommends git python3-pytest podman fuse-overlayfs && rm -rf /var/lib/apt/lists/*
COPY al-engine/ /etc/containers/
ENV HOME=/tmp/home
RUN mkdir -p /tmp/home && chmod 1777 /tmp/home
WORKDIR /work
```

The inner engine stores everything in a volume of its own per run, cannot see your containers, images
or socket, and everything it starts stays inside the run's network and memory, CPU and process
limits. The container gets `/dev/fuse` and that volume, nothing else: every capability stays dropped.
It has one uid inside, so an inner image whose process switches user (postgres, for one) does not
run. `docs/DESIGN.md`, "An engine for the sandbox", lists every flag and why.

`al sandbox notes.al doctor --env box` and every run's preflight check the engine (`engine:*` rows).
Until a person has applied the one-line change to the protected `envs.py` that passes the engine's
flags (see `docs/DESIGN.md`), the run's container has no `/dev/fuse`, and a run is refused before its
first step. The preflight then fails on the `engine:fuse` row, and its fix reads: "the protected kit/runtime/adw_modules/envs.py does not pass the nested-engine flags yet: a person applies the change in docs/DESIGN.md "An engine for the sandbox"; until then this environment cannot run".

An `engine:userns` miss means the host does not let the container create a user namespace
(`user.max_user_namespaces`, or `kernel.apparmor_restrict_unprivileged_userns=1` with a container
profile that does not allow it). al never turns AppArmor or seccomp off to get around it.

**Or run one check somewhere else.** `(check engine-test #:cmd engine-test #:in engine)` runs that
check in the run's worktree inside another podman option (its own container, e.g. one with
`#:nested-engine #t`) or a nix-devshell option (on your machine), instead of the workflow's own
stack. The gate's re-run of the same command runs there too. For a workflow that ships without you a
check `#:in` a nix-devshell option runs outside the sandbox, and the compiler says so:

```text
$ al compile notes.al > /dev/null
al: warning: workflow zte ships without a person, but check engine-test runs #:in nix-devshell (a nix-devshell environment), outside its podman sandbox
```

**Skipped tests are counted.** Every check records how many tests were skipped and why, from the output
of pytest (`-rs` prints the reasons), `go test -v`, `cargo test` or mocha, or from the JUnit report it
collects. The run summary shows them next to the exit code, the dashboard puts an `N skipped` badge
next to it, and the reviewers get them in their artefact list:

```text
checks:    test=0 (3 skipped: no podman ×2, needs network ×1)
```

`#:max-skipped N` fails a check when more than N tests were skipped; `#:forbid-skip-reason "TEXT"` when
a test was skipped for a reason that contains TEXT. `al explain` says so
(`check engine-test: run the engine-test command; fails when a test is skipped because of "no podman"`).
A broken rule turns the check's exit 0 into 1 and ends its log with the reason, which the resolve agent,
the gate's re-run and you all see:

```text
SKIPPED [1] test_engine.py:3: no podman
SKIPPED [1] test_engine.py:7: no podman
SKIPPED [1] test_engine.py:11: needs network
========================= 1 passed, 3 skipped in 0.02s =========================

al: check test fails: 2 tests skipped because "no podman", forbidden by #:forbid-skip-reason "no podman"
```

With a rule set, output al cannot read fails the check too, with a line that says what to add
(`pytest -rs`, `go test -v`, or `#:collect` a JUnit report): a gate never passes on tests it could not count.

### Run only the checks a change needs

A workflow runs every check it lists for every change: in its check steps, again in each review
round, and again at the gate. For a change to a README that is an hour of suites nothing in the
change can affect. Two things bring the cost down to what the change needs.

**The same check never runs twice on the same tree.** This needs nothing from you. Every result is
recorded with a hash of the worktree's content; when the same command is asked for again on an
identical tree (the gate after a passing check step, a review round that changed nothing), the
pass is used again and shown as `reused from STEP, same tree HASH`. A command whose answer can
differ between two runs on the same files says so, and is then always run:

```racket
(cmd load-test "./scripts/load-test.sh" #:reuse #f)
```

**A command says what it covers, and a workflow asks for the affected checks only.**

```racket
(codebase #:root "../myapp" #:languages (python)
  (cmd suite-a "sh a/test.sh"    #:covers ("a") #:always #f)
  (cmd suite-b "sh b/test.sh"    #:covers ("b") #:always #f)
  (cmd docs    "sh check-docs.sh" #:covers ("README.md" "docs") #:always #f)
  (cmd lint    "ruff check"      #:covers ("a" "b")))          ; #:always #t is the default
```

`#:covers` lists directories, files or globs (`"*.md"`, `"docs/**"`) relative to the repository
root. `#:always #f` means: where affected checks are asked for, run this one only when a changed
path is under what it covers. Without `#:always #f` a command runs for every change, so a layer
that declares nothing behaves as before. A workflow asks in one of three places:

```racket
(check tests #:cmds affected #:resolve resolve-test)   ; one check step per cmd with #:covers, named after it
(check e2e #:cmd e2e #:when-affected #t)                ; one step
(review auto #:id zero-touch #:require-checks (suite-a suite-b docs) #:require affected #:ship merge)
```

The runner compares the paths the run changed (against the branch it started from; in a review
round, since the check last passed) with the declared coverage. A check it does not run is
recorded as **not affected**, with the reason and the paths compared, never as passed. A run that
changed `a/x.txt` only:

```text
[adw afec0001] step suite-a (check) passed: exit 0, log agents/afec0001/checks/suite-a.1.log
[adw afec0001] check suite-b not run: not affected (no covered path changed; 1 paths compared)
[adw afec0001] step suite-b (check) not affected: not affected: no covered path changed (1 paths compared)
…
checks:    docs=not affected (no covered path changed; 1 paths compared), suite-a=0, suite-b=not affected (no covered path changed; 1 paths compared)
gate:      suite-a=reused from suite-a, same tree 428c44f0, suite-b=not affected (no covered path changed; 1 paths compared), docs=not affected (no covered path changed; 1 paths compared)
```

It is careful where it cannot know. Every check runs, and the record of each names the path that
caused it, when

- the change touches the agentic layer itself (`the change touches the agentic layer (agentic/commands/plan.md)`),
- a changed path is covered by no check (`other/x.txt is covered by no check`),
- a changed file is declared by two or more `#:always #f` checks (`shared/util.py is declared by several checks (suite-a, suite-b)`),
- or the changed paths cannot be computed.

**Making a documentation change cheap.** Because a path no check covers runs everything, a README
change is not made cheap by leaving the README out. Give the documentation paths a cheap check of
their own, as `docs` above (a link checker, a spelling check, `true` if you have nothing better).
A run that only reorganised `README.md` then runs that one command, once, and ships:

```text
checks:    docs=0, suite-a=not affected (no covered path changed; 1 paths compared), suite-b=not affected (no covered path changed; 1 paths compared)
gate:      suite-a=not affected (no covered path changed; 1 paths compared), suite-b=not affected (no covered path changed; 1 paths compared), docs=reused from docs, same tree a4003e51
review:    pass (auto) after 1 round(s)
ship:      merge -> main (shipped=True)
```

**The gate.** By default (`#:require all`) the gate accepts nothing less than a passing result for
the exact tree that ships: it reuses a pass recorded for that very tree, and runs every other
required command, also the ones a check step recorded as not affected. A pass on an earlier tree
is never used, even when nothing the command covers changed since. `#:require affected` lets the
gate record a required `#:always #f` command as not affected instead, which is what makes the
README change above ship without a code suite. It is your decision, and the compiler says what it
costs when the workflow merges without a person:

```text
al: warning: review `zero-touch` has #:require affected and #:ship merge: a required check no changed path touches is not run before the change ships without a person; a wrong #:covers then ships unproven code
```

**Where you read it.** In the run summary, as above. In the pull request: a section is appended to
`PR.md`, or posted as one comment on the run's pull request:

```markdown
## What the checks proved

| Check | Where | Result | Evidence |
| --- | --- | --- | --- |
| `suite-a` | suite-a | not affected (no covered path changed; 1 paths compared) | 1 paths compared, tree `a4003e51` |
| `suite-a` | gate | not affected (no covered path changed; 1 paths compared) | 1 paths compared, tree `a4003e51` |
| `suite-b` | suite-b | not affected (no covered path changed; 1 paths compared) | 1 paths compared, tree `a4003e51` |
| `suite-b` | gate | not affected (no covered path changed; 1 paths compared) | 1 paths compared, tree `a4003e51` |
| `docs` | docs | passed | log `agents/afec0005/checks/docs.1.log`, tree `a4003e51` |
| `docs` | gate | reused from docs, same tree a4003e51 | log `agents/afec0005/checks/docs.1.log` |

Paths compared:

- `README.md`
```

(The "Checks" list in the pull request body itself still prints `exit 0` and a
`NAME.not-affected.log` for a check that was not run; the table is what to read.) On the
dashboard's run page a step that was not run is shown as `not affected`, and the outcome has the
same table under "What the checks proved". `al explain` says what each command covers and which
steps run only when affected; `al verify` and `al sandbox doctor` list the checks that declare no
`#:covers` and the source paths no check covers (Step 7).

*If it goes wrong:*

| Symptom | Usual cause | Fix |
|---|---|---|
| `#:always #f needs #:covers: without declared paths the command would never run` | a selective command without paths | add `#:covers`, or drop `#:always #f` |
| `#:cmds affected, but no codebase cmd declares #:covers` | the step stands for nothing | declare `#:covers` on the commands it should run |
| `step name … used twice` | `#:cmds affected` names its steps after the commands, and a step of that name exists | rename the other step |
| every check runs although the change was small | a changed path no check covers, or one two selective checks declare; the summary and the log of each check name it | cover the path with one check, or make one of the two `#:always #t` |
| a check always runs again at the gate | it changed files in the worktree while it ran (its result then belongs to no tree), or it is `#:reuse #f` | write reports to an ignored directory |

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
(stage hotfix #:branch "hotfix/{date}" #:from main #:into main #:accepts (bug) #:agents-may-merge #f)
(stage main #:branch "main" #:accepts () #:agents-may-merge #f)
```

Bugs still go to `develop` (declared first) unless a workflow says `#:deliver-to hotfix`.

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
final stage. With `(delivery gitflow)` the hotfix stage already takes bugs, so only the
`(adw hotfix … #:deliver-to hotfix)` is needed. A run whose class changes on the way (the classify step says `bug`, which goes elsewhere) is
handed to you instead of delivered to the wrong branch.

### What the compiler refuses

- A zero-touch workflow (`review auto` with `#:ship merge` or `deploy`) whose work would go to a stage only
  people may change:
  `(adw zte): review `zero-touch` ships without a human, but class feature is delivered to stage `main` (branch main), where agents may not merge; deliver to a stage with #:agents-may-merge #t (e.g. #:deliver-to sprint) or use a human or agent review`.
- Stages that do not lead to one final stage, two final stages, a final stage whose branch is not
  `#:default-branch`, two stages on one branch, an invalid branch name or an unknown placeholder.
- `#:entry fast-forward` with any forge (`github`, `forgejo`): a pull request is merged by merge, squash or
  rebase.
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
## Promote a stage

Work that collects in a stage (a sprint, `develop`, a release or hotfix branch, a phase) moves into the next
stage when people accept it. `al promote` does that move: it shows them the evidence, records who accepted
what, takes out what they refused, and then merges, versions, tags, deploys and merges back.

### Declare how a stage is promoted

Every stage that goes into another has a promotion: the one you declare, or one with the defaults (one
person approves; the stage's checks must pass; every change is accepted or refused, or the whole is
approved; merged by merge; no version, no tag):

```racket
(delivery sprint)
(promotion sprint -> main #:approval human #:approvers 1 #:version (bump minor) #:tag "v{version}")
(promotion hotfix -> main #:approvers 2)                ; an urgent fix needs two people
(promotion develop -> main #:after (deploy production) #:back-merge (sprint))
```

`#:method` is `merge`, `squash`, `fast-forward` or `cherry-pick`; `#:version` is `(bump major|minor|patch)` or
`(keep)` and is written where the repository keeps it (`VERSION`, `pyproject.toml`, `package.json`,
`Cargo.toml`; else the latest `vX.Y.Z` tag), with a changelog entry in the style `CHANGELOG.md` already has
(Keep a Changelog, or the format of its last version heading). `#:present` chooses the sections of the
presentation pack. `al explain` says each promotion in words.

### Run it

```
$ al promote LAYER sprint main
promotion 3f680cec: sprint/2026-s47 -> main (merge; 1 human approval)
changes: 3 delivered since the cycle opened (3 to decide)
checks on sprint/2026-s47 at cd991ef: test=0
pack: artefacts/3f680cec/pack/index.html
approval item: promotions/3f680cec/approval.md
waiting: 1 human approval missing; 3 changes undecided
decide: al decide LAYER sprint main accept|refuse|approve [CHANGE …] --by NAME [--comment TEXT]
then:   al promote LAYER sprint main
```

(The run's own progress lines and its summary are left out here.) The changes are the runs that delivered
into the sprint (or whose pull request into it a person merged) and are not in `main` yet. The checks of
both stages run on the sprint's head. The command ends with exit 75: the promotion is a run like any other
(`agents/<id>/`, the dashboard's run page) and it now **waits** for people.

The **presentation pack**, `artefacts/<id>/pack/index.html`, is one page to open in a browser or show in a
review meeting: a plain summary; per change the request, what was done, the files, risk notes the reviewers
raised, and each acceptance criterion of its plan with the proof that covers it (the checks, screenshots,
playable recordings and their keyframes, the reviewer's verdict; a criterion nothing covers is marked);
the checks on the stage head; release notes and the changelog entry; and what is not included. Beside it
are `summary.md`, `release-notes.md`, `changelog-entry.md` and `acceptance.md`.

### Decide

```
$ al decide LAYER sprint main refuse 0000aaa2 --then revert --by "Ada Example" --comment "not this sprint"
recorded: Ada Example refuses 0000aaa2 (then revert): "not this sprint"
waiting: 1 human approval missing; 2 changes undecided
$ al decide LAYER sprint main approve --by "Ada Example"
recorded: Ada Example approves all
ready: run `al promote LAYER sprint main` to continue
```

Accept or refuse single changes, or `approve` the whole (which accepts what is undecided). `--by`
defaults to the application's `git config user.name`. The same decisions can be made on the dashboard
(the promotion card) and, with a forge, on the pull request: an approving review, or a comment
`/accept ID…`, `/refuse ID… [revert|patch] why` or `/approve`. Each decision is one line in
`promotions/<id>/acceptance.jsonl`, rendered as `promotions/<id>/sign-off.md` (who, when, what, comment).
`promotions/` is not ignored by git: commit it, it is the audit trail.

### Continue

```
$ al promote LAYER sprint main
continuing promotion 3f680cec (opened 2026-11-16)
excluded: 0000aaa2 reverted out of sprint/2026-s47 (refused by Ada Example: "not this sprint")
approved: by Ada Example
version: 0.1.0 -> 0.2.0 (VERSION); CHANGELOG.md: keepachangelog entry with 2 changes
merged: sprint/2026-s47 into main by merge at 4adce8c
tagged: v0.2.0
closed: cycle sprint 2026-s47 (accepted)
```

A refused change is reverted out of the stage first (an agent round, with the review step's
`#:patch`/`#:implement`, resolves a revert that does not apply cleanly), the checks run again and the pack
is rebuilt; `--then patch` also starts a patch run of the change's workflow with your comment. Then the
version and changelog commit, the merge, the tag, `#:after (deploy …)` and the back-merges. Nothing is ever
checked out in your working tree: a target branch you have checked out gets `al/promote/<branch>` and a note
(`git merge --ff-only al/promote/main`). All changes refused: nothing is merged and nothing tagged.

`al promote LAYER sprint main --dry-run` builds the pack and says what would happen (`would merge …`,
`would set version …`, `would tag …`) without changing a branch, a tag or a record. A stage whose
`{date}` branch pattern matches several open branches needs `--branch`.

### A forge

With `(forge github …)` or `(forge forgejo …)` the request is a pull request from the stage branch into the
next one, whose body is the pack's summary; with Forgejo the pack's pictures and the page itself are
attached. Approvals and `/accept` / `/refuse` / `/approve` comments are read each time the promotion
continues, and the pull request is merged through the forge (`merge` or `squash`).

### Hotfixes

A hotfix stage (cut from `main` and going straight back into it, as in `(delivery gitflow)`) is promoted
under its own gate: `#:approval auto` is refused, and without `#:back-merge` the fix is merged back into
every other stage that takes work (`develop`, the sprint). A back-merge that conflicts gets one agent round;
a stage agents may not merge into gets the back-merge as a branch `al/back-merge/<id>-<stage>` (and a pull
request with a forge) instead.

### Freezes

```racket
(stage sprint #:branch "sprint/{cycle}" #:from main #:frozen ("2026-12-21..2027-01-03") #:frozen-while-promoting #t)
```

A run that would deliver into a frozen stage ends `waiting` at its `deliver` step and says until when
(`delivery.frozen` in its events); resume it afterwards with the command it prints. With
`#:frozen-while-promoting #t` the stage takes no delivery while a promotion out of it is open; when the
promotion closes, its summary lists the runs that waited, with their resume commands.

### Agents as approvers

Between intermediate stages agents may approve: `(promotion dev -> qa #:approval agent #:approvers 2)` runs
two independent reviewer agents on the whole stage and the pack; both must pass. The compiler (and the
runtime again) refuses `#:approval agent` or `auto` into a stage where agents may not merge.

### What the compiler refuses

- A promotion without stages, between stages that are not FROM and FROM's `#:into` (default `#:from`), or
  two promotions of one stage.
- `#:approval agent|auto` into a stage with `#:agents-may-merge #f`; `#:approvers` outside 1–9; `auto` on a
  hotfix promotion.
- A `#:tag` with a placeholder other than `{version}` `{cycle}` `{date}`, that is not a valid tag name,
  with `{version}` but no `#:version`, or with `{cycle}` on a stage that is not the cycle's.
- `#:after` other than `(deploy)` / `(deploy ENV)`, a deploy into a stage without `#:deploy-cmd`, or an ENV
  other than its `#:environment-name`; `#:back-merge` naming FROM or TO.
- A `#:frozen` period that is not `"YYYY-MM-DD..YYYY-MM-DD"` with real dates, the first not after the second.

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
An ejected command or hop template keeps the line `{{step_block}}` at its top, right after the front
matter: it becomes the "Your step" block that tells the agent which step it is and what it must leave undone.
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

## Running a layer on another machine

A layer that is committed with its repository (`(layer in-repo …)`) can run on a server. Say in
`agentic.rkt` what that machine must have, once per kind of machine:

```racket
(registry #:address-env "AL_REGISTRY")
(host #:id server #:provision script #:units (cron dashboard) #:dashboard-port 8770 #:images pull)
(host #:id nix #:provision home-manager #:images load)
```

`al gen` then writes three files for each host under `env/hosts/<id>/`, and you commit them with the
layer: `home-manager.nix` (a Home Manager module), `nixos.nix` (a NixOS module) and `provision.sh` (a
shell script for Debian/Ubuntu, Fedora and Arch). None of them names a host, a user, an address or a
secret. `#:provision` picks the one `al deploy` uses:

- `script`: the shell script installs the missing distribution packages (podman and its rootless
  helpers, git, curl), adds the user's subuid/subgid ranges and turns on lingering, each through
  `sudo -n`. Without privilege it installs uv and the agent CLI at pinned versions into
  `~/.local/bin`, after checking their sha256. `sh provision.sh --check` shows the plan and changes
  nothing.
- `home-manager`: `al deploy` writes `~/.config/al/<name>/home.nix` on the host, a small file that
  imports the module from the checkout and sets the checkout's path, then runs `home-manager switch`.
  You import that file from your Home Manager configuration once; the module then owns the units.
- `nixos`: the same with `~/.config/al/<name>/nixos.nix`, and `sudo -n nixos-rebuild switch`.

`al deploy LAYER --to HOST --as ID --provision` reads what the host has, prints the plan with every
privileged command, asks once (`--yes` skips the question; without a terminal it stops with exit 4)
and applies it. Then it checks the host again and deploys. The units' PATH comes from the tools that
second check finds, so it holds what provisioning installed (`~/.local/bin` for the script style), and
those paths are what `~/.config/al/<name>-tools.json` records. Without `--provision`, deploy installs
nothing and prints the command that would. On a Debian host that lacks podman, uv and the agent CLI:

```
provisioning plan for notes (host server):
  packages (debian): missing podman uidmap slirp4netns
    privileged: sudo -n apt-get install -y --no-install-recommends podman uidmap slirp4netns
  subids: ok
  uv 9.9.1: missing; installs into ~/.local/bin (sha256 checked)
  claude 9.9.2: missing; installs into ~/.local/bin (sha256 checked)
  linger: off (the units would stop when alhostuser logs out)
    privileged: sudo -n loginctl enable-linger alhostuser
provisioning: 4 item(s) to do; nothing was changed (--check)
installed uv 9.9.1 into ~/.local/bin
installed claude 9.9.2 into ~/.local/bin
+ sudo -n apt-get install -y --no-install-recommends podman uidmap slirp4netns
+ sudo -n loginctl enable-linger alhostuser
provisioning: done
preflight after provisioning:
…
image: pulled notes:recipe-8061e87a059a, recipe 8061e87a059a checked, tagged al-notes:8061e87a059a and al-notes:latest
units:
  unit                        enabled  active
  al-notes-cron.service       enabled  active
  al-notes-dashboard.service  enabled  active
  linger: yes
```

A second run finds nothing to do and says `provisioning: nothing to do`. When the host refuses
privilege, deploy stops before it touches the repository or the units, and prints the privileged
commands for a person to run.

`al deploy … --as ID --status` adds a drift table: what the declaration wants next to what the host
has. It changes nothing:

```
provisioning (host server, #:provision script):
  fact             wanted        on host
  podman rootless  yes           yes                        ok
  subids           yes           yes                        ok
  uv               9.9.1         uv 9.9.1 (fake)            ok
  claude           9.9.2         9.9.2 (Claude Code, fake)  ok
  linger           yes           no                         drift
  units            2 active      2 active                   ok
  image recipe     8061e87a059a  8061e87a059a               ok
1 fact(s) drifted: al deploy …/notes/agentic --to deploy-host.example --as server --provision
```

**The sandbox image.** By default (`#:images build`) every host builds the image from
`env/Containerfile` at its first run. With a `(registry …)`, one machine builds and tests the image,
and every host uses exactly that one. It is the image a run asks for, `al-<name>:<recipe hash>`
(see `al sandbox LAYER status`): the hash covers `env/Containerfile`, the files it copies from `env/`
and the base image, and the image carries it as its label `al.recipe`:

```
$ al sandbox agentic push
image: al-notes:8061e87a059a is missing; building it from env/Containerfile
…
pushed al-notes:8061e87a059a to the registry as notes:recipe-8061e87a059a (recipe 8061e87a059a)
```

The registry declaration holds only names of variables. Put the address (and, with
`#:username-env` / `#:password-env`, the login) in the environment or in `local.env`, and on the host
in `~/.config/al/<name>.env`. A host with `#:images pull` pulls the image of the layer's recipe. With
`#:images load`, `al deploy` sends the image over the ssh connection and no registry is needed.
Either way the host checks the label first and then tags the image `al-<name>:<hash>` and
`al-<name>:latest`. If the label differs, the host untags both and builds its own at the first run:

```
image: recipe eeeeeeeeeeee on the host, 8061e87a059a in the layer: it will be built on the host at the first run
```

With `#:fallback-build #f` the deploy stops instead.

A deploy also works while runs are in flight on the host. The checkout is updated through a
temporary repository, so commits that live only in a run's private object store never get in the way.
## Let a stand-in decide when a run stops

Many stops need only a small decision: run a flaky check again, build again with one more
instruction, or give up. The **stand-in decider** makes that decision for you, or proposes it. When a
run stops and is handed to you, it makes one more agent call, on the heavy tier of the workflow's
agent (so `al models --set heavy=…` applies). That call reads why the run stopped and what the agents
asked for, and chooses one of four actions:

- `resume`: run again from the step that stopped, unchanged;
- `resume-with`: run again from a step it names, with instructions added to the run's request;
- `abandon`: stop here; the hand-off stays as it is;
- `ask-human`: it cannot decide.

It has two modes. With **suggest**, nothing happens until you answer: the decision goes into
`agents/<run id>/decision.md` and into the run's `needs-human` message (e-mail or command notifier),
whose subject ends in `[decide <run id>-<code>]`. With **auto**, a resume starts at once, at most twice
per run. After that, and whenever the decider says `abandon` or `ask-human`, the run comes to you as
before. A round that resumes sends no `needs-human` message, because the resumed run reports its own
outcome. The default is **off**, which changes nothing.

The mode is a setting of this machine, like `al models`. It lives in `agents/settings/decider.json`
and is never committed (`al check` fails if it is):

```console
$ al decider ~/code/myapp.al --set suggest
mode: suggest (local setting)
modes: off (a person reads every hand-off), suggest (the decider proposes, a person answers with `al answer`), auto (the decider resumes by itself, at most 2 times per run)
settings: ~/code/myapp.al/agents/settings/decider.json (local to this machine, never committed); applies to runs that stop from now on; one run: --decider MODE
```

`al decider LAYER` alone shows the mode and where it comes from, and `--reset` turns it off again.
`AL_DECIDER=auto` in the environment wins over the setting, and `--decider MODE` on one run wins over
both (`uv run adws/adw_change.py "…" --decider auto`).

A suggestion, as `decision.md` records it:

```text
# Decision for run dec1de0a

- mode: suggest
- action: resume
- from step: test
- stopped at: test
- status: suggested
- round: 0

## Summary

The test check looks flaky: the build is fine, run it again.
```

**Answer it** with one word. `go` applies the suggestion, `alter TEXT` applies it with your changes
added (a suggestion to abandon or ask a human then becomes a resume from the step that stopped,
with your text as the instructions), `explain` sends the decider's full explanation through the
same notifiers, and `reject` leaves the run with you:

```console
$ al answer ~/code/myapp.al dec1de0a go
run dec1de0a: go: resuming (from step test)
$ al answer ~/code/myapp.al dec1de0a go
al answer: run dec1de0a has no suggestion waiting for an answer (decision status: resuming)
```

The resumed run starts in the background, in the same worktree and on the same branch. Its request
now ends with a `## Decision` section holding the instructions, so every later step sees them.

**Answer by e-mail.** Reply to the `needs-human` message with one line (`go`, `yes`, `alter: use the
v2 API`, `yes but keep the old column`, `explain more`, `reject`, `no`) and keep the subject, because it
carries the code. To have replies read, give the layer a mailbox in `local.env`:

```text
AL_DECIDE_IMAP_URL=imaps://USER:PASSWORD@imap.example.org/INBOX
```

The cron trigger reads unseen replies on every poll, and `al answer LAYER --mail` reads them once. A
reply counts only if it carries the code and comes from an address in the e-mail notifier's `#:to-env`
variable. Messages without a `[decide …]` token are left unread. Plain `imap://` is accepted only
to this machine.

**Answer by webhook.** A layer with a webhook trigger that enables `payload` events accepts:

```json
{"decision": {"run": "dec1de0a", "code": "<the code from the subject>", "answer": "go"}}
```

It is signed like every other payload. The reply is 200 when the answer was applied, 409 when it was
refused (wrong code, or nothing waiting), and 400 for a bad payload.

**What the decider never does.** It never merges, deploys, approves a pull request or gets around the
gate: a resumed run passes every check, the review and the shipping gate again. It is never used for
a workflow with `#:changes layer` (a person always approves those), for a run that a spending or
usage limit stopped (money is your decision), for a waiting run or for a fan-out candidate. If its
own call fails or answers something invalid, the run and its hand-off stay exactly as they were. The
dashboard shows the decision on the run page as a **Decision** block. It does not take answers.

## Where it runs: platforms

`al doctor --platform` says what this machine can run. It asks only podman and Nix about themselves and
changes nothing:

```
$ al doctor --platform
al doctor --platform: what this host can run

  os                 Linux x86_64
  podman             5.2.1, rootless
  nix                2.24.9, flakes enabled
  host browser       chromium
  environment kinds  worktree, nix-devshell, podman
  browser feature    nix-devshell, podman
  zero-touch         yes

zero-touch: a workflow that ships without a person can run here (it runs in podman)

notes:
  - podman runs natively; rootless podman is what al expects
  - the browser feature of a nix-devshell option (#:browser #t) works: Chromium from nixpkgs
```

| Host | worktree | nix-devshell | podman | zero-touch |
|---|---|---|---|---|
| Linux x86-64 / arm64 | yes | yes (browser feature: yes) | yes, rootless | yes |
| macOS | yes | yes (browser feature: no, use podman) | through `podman machine`: bind mounts slower, only shared folders (home) mountable, ports forwarded to 127.0.0.1 | yes, with a running machine |
| Windows | inside WSL | inside WSL | through `podman machine` (WSL); keep app and layer in the WSL filesystem | yes, with a running machine |
| neither podman nor Nix | yes | no | no | no (compiler rule) |

On arm64, the images you use must exist for `linux/arm64`. `--json` prints the same facts as JSON.

## When something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| a step fails with `step-discipline: the build step committed …` (or `created ref …`, `left validate-x at the top of the layer directory`) | the agent did another step's job: only the commit step commits, and nothing but the run's own state may appear at the top of the layer. This is a step-discipline violation, not a test failure | the detail lists what the runtime undid (HEAD reset with the changes kept, stray files moved to `agents/<run id>/scratch/stray/`) and what it left for you (a ref, the main checkout); delete a stray ref with `git branch -D` and run again |
| a check fails with `timed out twice (300 s, then 600 s)` | the check hit its time limit, was run once more with twice the limit (without the resolve agent), and hit that too; the detail quotes its last progress line | a slow or busy machine: raise the cmd's `#:timeout`, or run fewer runs at once (`#:max-checks`) |
| `al check` notes `template … (yours) has no "Your step" block` | a template you ejected before the block existed does not tell the agent which step it is | add a line `{{step_block}}` right after its front matter, as in the kit's version, then `al gen` |
| `al gen` says a template uses an unknown placeholder | a hand-written template has a typo in `{{…}}` | fix the name; use only the placeholder names that appear in the kit's templates (`kit/templates/`) |
| a run waits with `waiting for a free runs slot` | the concurrency limit is reached | wait, or raise `#:max-runs` |
| `status: failed` and a `[needs human]` pull request | the run stopped and handed over | read "Why this stopped" in it |
| `decision:  suggest … (suggested)` in the run summary | the stand-in decider proposes a next step | `al answer LAYER RUN go`, `alter TEXT`, `explain` or `reject` |
| `git-integrity` failure | something in the container altered git metadata it should not | inspect `trees/<run id>`; nothing was shipped |
| `al gen` stops with "edited by hand" | a generated file was changed directly | see step 14: eject the template, or `--discard-edits` |
| the agent cannot edit files or run commands | permission mode or allowed tools too narrow | adjust `#:permission-mode` / `#:allowed-tools` on the agent |
| leftover branches or copies after experiments | runs you interrupted | `git -C ~/code/myapp worktree prune`, then delete the branches |
| a run refuses to start with exit code 3 | today's spending limit is reached | raise `#:day-usd`, or wait until tomorrow |
| an `al` command exits 4 with `… is missing: al looked at …` | a prerequisite (racket, uv, git, the agent CLI) is not where al looks | install it, or name it in `~/.config/al/tools.env`; `al doctor` shows what al sees |
| `al adopt` refuses to write | a file it wrote earlier was edited by hand | move your change into `agentic.rkt`, or `--force` |
| a run fails at `preflight` with a table | its container lacks something the workflow needs | the `fix` column names the declaration to change; `al gen`, then `al sandbox LAYER doctor` until it passes |
| the image did not change although you changed `env/Containerfile` by hand | the Containerfile is generated: `al gen` overwrites it from `agentic.rkt` | change `#:packages` / `#:setup` in `agentic.rkt`; `al sandbox LAYER status` shows the recipe hash |
| the image is older than the pinned packages you expect | with a toolchain, `env/flake.lock` and the toolchain's declaration are part of the recipe: a changed lock or declaration is a new recipe | it is built at the next run or by `al sandbox LAYER build`; `al sandbox LAYER update` re-pins nixpkgs in `env/flake.lock` |
| a podman or Nix environment does not start on this machine | podman or Nix is missing, stopped or not set up for al | run `al doctor --platform`: it says what is missing and what can run here |
| a run fails at its `prepare` step | an install command of `#:prepare` (or the dev container's `postCreateCommand`) failed | read its log on the dashboard or in `agents/<run id>/scripts/prepare.1.log` |

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
