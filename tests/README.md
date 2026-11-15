# Tests

| Tier | Where | Run | Needs |
|---|---|---|---|
| Compiler | `racket/al/tests` | `raco test racket/al/tests` | racket |
| Runtime units | `kit/runtime/tests` | `(cd kit/runtime && uv run --with pytest pytest -q)` | uv |
| Integration | `tests/integration` | see below | racket, uv, git; podman / nix / network per marker |

`tests/acceptance.sh` is the older end-to-end script. It works on the git-ignored `layers/` directory
and leaves branches and worktrees behind in the testbeds; `tests/integration` supersedes it and covers
everything it did (sections C and D) reproducibly.

## Integration suite

Run from the repository root:

```bash
uv run --with pytest --with pyyaml --with jsonschema pytest tests/integration -q            # default tier
uv run --with pytest --with pyyaml --with jsonschema pytest tests/integration -q -rsx       # … and list skips / xfails with reasons
```

The default tier is everything not marked `slow` or `llm` (about three minutes). It is LLM-free:
every agent step runs on the mock agent, either through `--dry-run` or because the fixture copy of
the layer declares `(agent mock)`. It needs no network when `testbeds/<name>` holds the pinned
commits (see below); without that local cache every session fetches the five pins from GitHub.

A `-m` on the command line replaces the default selection:

| Command suffix | Runs |
|---|---|
| *(none)* | default tier: pipeline, dry runs, golden snapshots, tasklist + cron, concurrency, killed run, gate scenarios, fast `al verify` |
| `-m "not slow and not llm and not podman"` | default tier without the gate scenarios |
| `-m podman` | only the zero-touch gate scenarios |
| `-m slow` | real codebase commands through `al verify`: `test_verify` (flask) and `test_real_checks` (one per testbed) |
| `-m "slow and nix"` | only `test_verify`: flask `typecheck` + `test` in the layer's nix dev shell |
| `-m slow -k "real_checks and gin"` | one testbed's real check |
| `--update-golden` | rewrite `tests/integration/golden/` from the freshly generated layers (review the diff, commit it) |

Markers (registered in `tests/integration/pytest.ini`):

| Marker | Meaning | Skipped automatically when |
|---|---|---|
| `slow` | runs real codebase commands (minutes to tens of minutes, toolchains download on first use) | never selected by default |
| `podman` | needs rootless podman | `podman` is not on `PATH`; the gate tests also skip when `docker.io/library/python:3.12-slim` is not in local storage (`podman pull` it once) |
| `nix` | needs nix | `nix` is not on `PATH` |
| `network` | needs network | `github.com:443` is unreachable |
| `llm` | drives a real agent | never selected by default; no test carries it yet |

`test_real_checks` additionally skips a testbed whose toolchain is missing: `go` + `make` (gin),
`node` + `npm` (express), `nix` (flask), `podman` (ripgrep and jq, whose layers run checks in a
container that carries cargo / autoconf + make).

### What the tests cover

| Test | Testbeds | Proves |
|---|---|---|
| `test_pipeline` | all five | compile → gen → `al check` ok; the IR validates against `schemas/agentic-layer-v1.schema.json`; no `{{…}}` left in any command or HOP |
| `test_plan_build_dry_run` | all five | the `plan-build` ADW passes on the mock agent, state and plan land under the layer, nothing in the testbed |
| `test_golden_commands` | all five | rendered `commands/*.md` and `hops/*.md` equal the snapshots in `golden/<name>/` |
| `test_tasklist_cron` | flask | `tasks.md` + `trigger_cron.py --once`: open task done with its `adw:<id>`, blocked task runs on the next poll |
| `test_concurrency_soak` | flask | 8 simultaneous runs under `#:max-runs 2`: never more than 2 hold a slot; no port lease, worktree or branch left |
| `test_killed_run_leaks_nothing_permanent` | flask | a run SIGKILLed inside a check holds no slot afterwards: the next runs start at once and pass |
| `test_killed_run_port_lease_is_reclaimed` | flask | the killed run's port lease is dropped when the next run leases its ports |
| `test_gate.py` (6 scenarios) | flask | the zero-touch gate: clean change → `merge` on `al/ship/main`; protected path, oversized diff, review blocker, disallowed class → `pr` with the reason; failing check → run fails, nothing shipped |
| `test_verify_reports_true_and_false`, `test_verify_timeout_kills_the_cmd` | flask | `al verify` exit code, report, logs, skip rule, `--cmd`, `--json`, `--timeout`, and that nothing is left behind |
| `test_verify` (`slow`, `nix`, `network`) | flask | the authored flask layer's `typecheck` and `test` cmds are true |
| `test_real_checks` (`slow`, `network`) | all five | each authored layer's `test` cmd (`build` for jq) is true |

### Setup and teardown guarantees

Testbeds are pinned (URL, commit, default branch) in `tests/integration/testbeds.toml`.

**The suite never works in `testbeds/<name>`.** Every pytest session makes its own clone of each
testbed it needs, under the session's temp directory (`<basetemp>/testbeds0/<name>`), and every
layer a test builds is rooted at that private clone: worktrees, branches, `al/ship/*` refs and
everything else a run creates land there and disappear with the temp directory.

- `testbed(name)` (session fixture) returns the private clone, with the default branch checked out
  at the pinned commit and a clean working tree.
  - When `testbeds/<name>` (git-ignored) is a git checkout that contains the pinned commit, it is
    used as an object cache: `git clone --no-hardlinks --no-checkout testbeds/<name>`, so no object
    file is shared, and the clone's `origin` is then re-pointed at the pinned URL so nothing can
    push or fetch into your checkout. Which branch is checked out there, whether its tree is dirty
    and which other branches or worktrees it has does not matter.
  - Otherwise (no checkout, or one without the pin) the pin is fetched with `--depth 1` from the
    pinned URL straight into the temp directory; when there is no network, the test is skipped and
    says so. The suite never creates or repairs `testbeds/<name>`; to have a cache, make one
    yourself: `git clone <url> testbeds/<name>` (any clone that contains the pinned commit will do).
  - `testbeds/<name>` is only ever read (`git cat-file`, `git clone` as the source, and
    `git --no-optional-locks status`, which does not refresh the index). Its fingerprint — `HEAD`,
    current branch, `git status --porcelain` and every ref with its commit — is taken before the
    first read and **asserted unchanged when the session ends**. Because the suite does not write
    there, you can keep working in `testbeds/` while it runs, with one consequence: a change you
    make there during a session (a commit, a new branch, an edited file) trips that assertion and
    is reported as an error on the session's last test.
  - Sessions do not share anything, so several can run at the same time (e.g. a `-m slow` real
    check next to the default tier).
- `layer(name, *edits)` (function fixture) builds a fresh layer per test in pytest's tmp directory
  from the committed sources `tests/authored/<name>/{LLM.txt,agentic.rkt}`: `#:root` becomes the
  absolute path of the private clone, `#:name` becomes a unique `it-<name>-<random>` (so worktree branches and
  podman container / image names cannot collide with a real layer or another test), the test's
  edits are applied (mock agent, concurrency limits, extra cmds or ADWs — helpers in
  `tests/integration/alit.py`, each of which fails loudly when the authored text it expects is
  gone), then `tools/al gen` runs. `layer(name, cmds={…})` builds a minimal mock-agent layer with
  exactly those codebase cmds instead.
- Teardown runs always, also after a failed test, in this order:
  1. kill processes whose working directory is inside the layer (orphans of killed runs);
  2. remove the podman containers, volumes and images of this test's layers (`al-<layer name>…`)
     that did not exist before the test, and new `al-cache-*` volumes that no container uses
     (nothing happens when podman is absent);
  3. `git worktree remove --force` every worktree of the private clone that lives under the layer
     directory, then `git worktree prune`;
  4. delete every branch that did not exist before the test (`refs/heads` is snapshotted in
     setup), and put a pre-existing `al/ship/*` ref back when one of the test's runs moved it;
  5. **assert** that the private clone's fingerprint — `HEAD`, current branch,
     `git status --porcelain` and the sorted `refs/heads` with their commits — equals the snapshot
     taken in setup, so the next test of the session starts from the pin again.

  A test that leaves anything behind that steps 1–4 cannot take away (a file in the clone, a moved
  `HEAD` or branch, a branch that cannot be deleted) fails in teardown, even if its body passed.
  Only the session's own tests ever use the clone, so anything found there was made by them.

The private clones and the layers live under pytest's base temp directory (usually `/tmp`). The
real checks build there (a Rust `target/`, `node_modules`, …); pass `--basetemp /var/tmp/al-it` to
put them on a disk when `/tmp` is a small tmpfs (pytest empties that directory first).

### Adding a testbed

1. Clone it into `testbeds/<name>` (your working copy, and the suite's read-only cache), run `tools/al init testbeds/<name> --layer layers/<name>` and
   author `LLM.txt` and `agentic.rkt` with the meta commands (see the top-level README).
2. Commit the authored sources as `tests/authored/<name>/LLM.txt` and
   `tests/authored/<name>/agentic.rkt`. The fixture expects the literal clauses
   `#:name "<name>"` and `#:root "../../testbeds/<name>"` in `agentic.rkt`, and an ADW called
   `plan-build`.
3. Add a table to `tests/integration/testbeds.toml`: the https clone `url`, the full `commit` SHA
   (`git -C testbeds/<name> rev-parse HEAD`) and the local `branch` the layer's `#:default-branch`
   names. Every test parametrised over `alit.BEDS` now includes it.
4. Add it to `REAL` in `tests/integration/test_verify.py` with the binaries its real check needs
   and the cmds to verify (name `install` explicitly if the check depends on it).
5. Create its snapshots:
   `uv run --with pytest --with pyyaml --with jsonschema pytest tests/integration -q -k "golden and <name>" --update-golden`
   and commit `tests/integration/golden/<name>/`.
6. Prove its commands once: `tools/al verify layers/<name>`.

## `al verify`

```bash
tools/al verify LAYER                       # every codebase cmd except deploy / start / install
tools/al verify LAYER --cmd typecheck --cmd test --timeout 1200
tools/al verify LAYER --json                # report on stdout, progress on stderr
```

The compiler proves a layer is coherent; `verify` proves its `(cmd …)` lines are true. It creates
one detached worktree of the application at the default branch under `<layer>/trees/verify-<random>`,
runs the selected cmds there in declaration order inside the environment stack of the first ADW
that has a `check` step (through the layer's own `adws/adw_modules`, so nix / podman behave exactly
as in a run), and removes the worktree. Cmds share that worktree, as the checks of one run do.
Per cmd it prints name, exit code and seconds (plus the last 15 output lines on failure), writes
`<layer>/agents/verify/<name>.log`, and records `[{name, cmd, exit, seconds, log}]` in
`<layer>/agents/verify/report.json`. Exit status is 0 only when every verified cmd exited 0.
`deploy`, `start` and `install` are skipped unless named with `--cmd`. The application's
`git status --porcelain`, `HEAD` and current branch are compared before and after; a difference is
an error.
