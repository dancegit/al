# Tests

| Tier | Where | Run | Needs |
|---|---|---|---|
| Compiler | `racket/al/tests` | `raco test racket/al/tests` | racket |
| Runtime units | `kit/runtime/tests` | `(cd kit/runtime && uv run --with pytest pytest -q)` | uv |
| Integration | `tests/integration` | see below | racket, uv, git; podman / nix / network per marker |
| Public readiness | `tests/tools/test_publish_ready.py` | `uv run --with pytest --with pyyaml --with jsonschema pytest tests/tools/test_publish_ready.py -q` | uv, git: the licence, the CI workflow, no tracked video and the media manifest, the tree size, the wording of `docs/DESIGN.md`, the flask sentence against its saved log, the README's box and Numbers section, the quick-start script, fake credentials and the allow-list, the spelling PITER |
| `al kpis` | `tests/tools/test_al_kpis.py` | `uv run --with pytest --with pyyaml --with jsonschema pytest tests/tools/test_al_kpis.py -q` | racket, uv, git: the numbers of `al kpis` on fixture run records, and that `docs/KPIS.md` is the marked sample or real numbers |
| Pre-publish scan | `tests/tools/test_prepublish_check.py` | `uv run --with pytest --with pyyaml --with jsonschema pytest tests/tools/test_prepublish_check.py -q` | uv, git: `tools/prepublish_check.sh` on small repositories built for the test (secrets, addresses, home paths, hosts, large files, the allow-list) |
| Quick start on a clean machine | `tests/quickstart/run.sh` | `tests/quickstart/run.sh` (not part of a suite; exits 3 without an engine) | podman or docker, network: the README's quick start in a container with only racket, uv and git; the log is `tests/quickstart/run.log` |

### Runtime tests against real podman

The runtime tests marked `podman` (`-m podman` in `kit/runtime`) run against real rootless podman and
skip cleanly without it. They never pull: `test_sandbox_podman.py`'s `sandbox_image` fixture derives
`localhost/al-sandbox-test` (git added) from an image already in local storage.
`test_nested_engine_podman.py` (the nested container engine, `#:nested-engine #t`) builds
`localhost/al-test-engine:1` from that image once, adding `podman fuse-overlayfs busybox-static` with
apt and the inner engine's `containers.conf` / `storage.conf` from `adw_modules/nested_engine.py`;
without network for apt it skips with the build's last lines. Inner images are `FROM scratch` with the
image's static busybox, built inside the sandbox. Its run tests need the one-line change to the
protected `envs.py` (`docs/DESIGN.md`, "An engine for the sandbox"): until a person applies it they skip
with `REQUIRES HUMAN: envs.py does not pass nested_engine.podman_flags yet`, and
`test_preflight_refuses_a_nested_run_when_envs_is_not_wired` runs instead (and skips once it is applied).
With `AL_SANDBOX_PROOF` set, a host without a usable podman fails the run instead of skipping it.

```bash
(cd kit/runtime && AL_SANDBOX_PROOF=/tmp/proof env -u AL_APP_ROOT uv run --with pytest pytest -q -rs -m podman tests/test_nested_engine_podman.py)
```

`AL_SANDBOX_PROOF` writes `nested-isolation.txt`, `nested-run-summary.txt` and `doctor-engine.txt` there.

### Affected checks and reused results

`kit/runtime/tests/test_affected.py` covers `adw_modules/affected.py` on its own: what a `#:covers`
entry matches, the selection rules and each fail-safe case, the tree hash on real repositories
(untracked files count, the index is not touched, `#:copy-files` are left out, a sandboxed run goes
through `gitsafe`), the changed paths (both sides of a rename, deleted files), the ledger and the
texts a person reads. `kit/runtime/tests/test_affected_runs.py` runs whole workflows through
`runner.main` with the mock agent on an IR shaped like `racket/al/tests/fixtures/affected-checks.rkt`;
every cmd appends its name to a counter file outside the worktree, so a test counts how often a
command really ran: a change in one directory runs one suite, a README-only change runs no code suite
and still ships, a covered-path change runs its suite once, `#:reuse #f` always runs again, and the
gate never uses a pass from another tree. `tests/tools/test_affected_checks.py` (needs racket) covers
the compile warning, the expanded steps in a generated layer, `al explain`, and the `coverage:` lines
of `al verify` and `al sandbox doctor`.

```bash
(cd kit/runtime && env -u AL_APP_ROOT uv run --with pytest pytest -q tests/test_affected.py tests/test_affected_runs.py)
(cd kit/runtime && AL_AFFECTED_PROOF=/tmp/proof env -u AL_APP_ROOT uv run --with pytest pytest -q tests/test_affected_runs.py)
```

`AL_AFFECTED_PROOF` writes `readme-only-summary.txt`, `readme-only-PR.md` (the "What the checks
proved" section of that run), `one-suite-summary.txt` and `covered-path-events.jsonl` (the
`check.decision` events) there. Two older runtime tests changed with the feature: the gate in
`test_events.py` now reuses the check step's pass (one `check.decision`, no second run), and
`test_skips.py::test_skip_rule_applies_to_the_gate_rerun` declares `"reuse": False` on its cmd,
whose output differs each time it runs.

Targeted testing (depths, batches and misses) has tests at each layer. In the runtime,
`test_selection.py` runs every selection strategy on the tiny repositories under
`kit/runtime/tests/data/selection/` (Python, TypeScript/JavaScript, Go, Rust; plain files, copied
into `tmp_path` and `git init`ed there), `test_coverage_map.py` the coverage recipes and the stale
rule, `test_testing_policy.py` the policy, the precedence of the depth, escalation, the plan of one
check, the miss-rate fallback, the accounting text and the failing-test parsers.
`test_targeted_runs.py` and `test_batch.py` run whole workflows through `runner.main` on the shop
application of `targeted_fixtures.py` (a real pytest suite of 43 tests; the cmd appends `full`,
`smoke` or `targeted TESTS…` to a counter file): a one-file change runs two tests, escalation and a
person's choice run full, ten changes into a sprint are proven by one full run, and a regression
only a dynamic import reaches is caught by the batch, bisected to its run (only the failing test runs
per step), handed to a fix run, counted as a miss and learned. A detached start is captured, never
spawned. `tests/tools/test_targeted_testing.py` covers `al gen` (`adws/verify_batch.py`), `al run
--testing`, `al testing`, `al batch`, `al explain`, `al verify`, `al kpis` and the dashboard's
mirror of the local setting; with `AL_TESTING_PROOF=DIR` it writes the outputs `docs/GUIDE.md`
quotes. The dashboard's Testing panel has `kit/dashboard/tests/test_testing_model.py`,
`test_testing_http.py` and `test_browser_testing.py` (screenshots `testing-panel-1280.png`,
`testing-heavy-1280.png`, `run-targeted-accounting-1280.png`, `batch-failed-1280.png`,
`start-testing-1280.png` under `AL_DASHBOARD_SHOTS`). Changed with the feature:
`tests/tools/golden/no-toolchain/agentic-layer.json` and `golden-env/*/agentic-layer.json` (the new
cmd fields at their defaults), `docs/KPIS.md` (the Testing table), and the golden `build.md` and
`implement.md` of every integration repository.

```bash
(cd kit/runtime && env -u AL_APP_ROOT uv run --with pytest pytest -q tests/test_selection.py tests/test_coverage_map.py tests/test_testing_policy.py tests/test_targeted_runs.py tests/test_batch.py)
AL_TESTING_PROOF=/tmp/proof env -u AL_APP_ROOT uv run --with pytest --with pyyaml --with jsonschema pytest tests/tools/test_targeted_testing.py -q
```

### Integrating a handed-over run

`kit/runtime/tests/test_integrate.py` covers `adw_modules/integrate.py` without a run: the settings file
(`templates/integrate.json`) and what it refuses, the generated marker, eligibility and each refusal
reason, the preview (a dry merge that leaves the application's objects and refs byte-identical, and
git without `merge-tree --write-tree`), the synthetic ADW, the conflict report and the two records.
`kit/runtime/tests/test_integrate_runs.py` runs whole integrations through `integrate.main` and
`runner.main` with the mock agent on temporary repositories built by
`kit/runtime/tests/integrate_fixtures.py` (an application with `stock.py`, an `INDEX.md` generated by
`./index.sh`, tests and a protected `deploy/`; a handed-over run with a real branch; a `main` that moved):
a conflict in a source file and a generated file lands and settles the original, a clean branch needs
no agent, an undecidable conflict, left markers, a dropped test, an agent that commits and a rejected
review land nothing and both runs say why, the refusals happen before any worktree, two integrations
run one after the other (real detached processes), the approval waits and then ships, the stand-in
decider starts it only when allowed, and no git command of a run forces a push or names the original
branch. `tests/tools/test_integrate_gen.py` (needs racket) covers what `al gen`, `al check`, `al eject`
and `al explain` do with the kit workflow, and `tests/tools/test_al_integrate.py` runs `al integrate`
(landing, `--preview`, `--detach`, `--approve`, `--conflicts`, `--set`, the refusals, `--model-set`) on
the pantry scenario of `kit/dashboard/tests/integrate_fixtures.py`; the dashboard's side is in
`kit/dashboard/README.md` ("Tests").

```bash
(cd kit/runtime && env -u AL_APP_ROOT uv run --with pytest pytest -q tests/test_integrate.py tests/test_integrate_runs.py)
env -u AL_APP_ROOT uv run --with pytest --with pyyaml --with jsonschema pytest tests/tools/test_al_integrate.py tests/tools/test_integrate_gen.py -q
```

The golden snapshots of `tests/integration` hold `commands/resolve-conflicts.md` for each of the five
layers: the prompt `al gen` writes into every layer for the workflow.

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
| `-m slow` | real codebase commands through `al verify`: `test_verify` (flask) and `test_real_checks` (one per testbed); and the real sandbox image builds of `test_toolchain_image.py` |
| `-m "slow and nix"` | `test_verify` (flask `typecheck` + `test` in the layer's nix dev shell) and `test_builds_with_host_nix` |
| `-m slow test_toolchain_image.py` | the real sandbox image builds; set `AL_TOOLCHAIN_SHOTS=DIR` to keep the headless chromium screenshot `toolchain-browser.png` there. With `AL_TOOLCHAIN_SHOTS` set, the run also leaves `al-sandbox-build.txt` and `dev.json` next to `toolchain-browser.png` |
| `-m slow` | real codebase commands through `al verify`: `test_verify` (flask) and `test_real_checks` (one per testbed) |
| `-m "slow and nix"` | `test_verify` (flask `typecheck` + `test` in the layer's nix dev shell) and `test_env_reuse.py`'s flake test |
| `-m slow test_env_reuse.py` | the reused environments, built and run for real: a flake dev shell with a browser page test, a Containerfile, a dev container (each skips without its tool; nothing is downloaded) |
| `-m slow -k "real_checks and gin"` | one testbed's real check |
| `AL_TEST_REMOTE_HOST=HOST … -m slow -k remote_host` | one real run on a second machine (see `test_remote_host` below) |
| `--update-golden` | rewrite `tests/integration/golden/` (and, with `-k golden_ir_and_env`, `golden-env/`) from the freshly generated layers (review the diff, commit it) |

Markers (registered in `tests/integration/pytest.ini`):

| Marker | Meaning | Skipped automatically when |
|---|---|---|
| `slow` | runs real codebase commands (minutes to tens of minutes, toolchains download on first use) | never selected by default; `test_remote_host` also skips unless `AL_TEST_REMOTE_HOST` is set |
| `podman` | needs rootless podman | `podman` is not on `PATH`; the gate tests also skip when `docker.io/library/python:3.12-slim` is not in local storage (`podman pull` it once) |
| `nix` | needs nix | `nix` is not on `PATH` |
| `network` | needs network | `github.com:443` is unreachable |
| `llm` | drives a real agent | never selected by default; no test carries it yet |
| `browser` | needs a headless Chromium (from the nix store for `test_env_reuse.py`) | the browser is not already present; the test never downloads one |

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
| `test_golden_ir_and_env` | all five | `agentic-layer.json` and every `env/` file equal the snapshots in `golden-env/<name>/`: a layer without a toolchain generates no toolchain file and no `toolchains` key |
| `test_toolchain_image.py` (`slow`, `podman`, `network`; the host way also `nix`) | none (its own small layer) | `al sandbox LAYER build` of a toolchain with `browser` and `agent-cli` builds the recipe image (`al-<name>:<recipe hash>`, `:latest` pointing at it) without host Nix (`AL_SANDBOX_BUILD=container`) and with it; `hello`, `jq`, `git`, `whoami`, a writable home and `/usr/bin/env` work under the runtime's hardening flags; `chromium --headless` takes a screenshot; `claude --version` answers; a second build is up to date |
| `test_env_reuse.py::test_flake_proposal_runs_the_page_test` (`slow`, `nix`, `network`, `browser`) | the `lumen` fixture (`tests/tools/env_fixtures.py`) | `al scout`'s proposal for a flake dev shell with Playwright, generated and verified: a page test runs in the wrapped dev shell with `#:browser #t` |
| `test_env_reuse.py::test_containerfile_reuse_builds_and_runs` (`slow`, `podman`) | `tarn` | the repository's Containerfile is built (stage `dev`) with al's thin layer on top: its marker is in the image, its `ENTRYPOINT` is reset, and a second verify does not rebuild |
| `test_env_reuse.py::test_devcontainer_reuse_builds_and_prepares` (`slow`, `podman`) | `quill` (without features) | a dev container's image, `containerEnv` and `postCreateCommand` (the first row of the report: `prepare:postCreateCommand`) |
| `test_remote_host` (`slow`, `podman`) | its own small repository | a zero-touch run placed on a second machine with `--on`: its container ran there with the hardening flags of `envs.hardening_flags` and the clone's `.git` mounted read-only, its branch arrived at the git source, and the home gate merged |

`test_remote_host` needs a second machine and is therefore opt-in. Without the variable it skips with
"set AL_TEST_REMOTE_HOST=<ssh destination of a second machine with git, uv and rootless podman> and run
with -m slow to prove a remote run (see tests/README.md)". Set `AL_TEST_REMOTE_HOST` to an ssh
destination that logs in without a password (a second user account on this machine, or a container
with sshd) and has git, uv and rootless podman; podman must work for the account that runs the test
too, with `docker.io/library/python:3.12-slim` in local storage. `AL_TEST_REMOTE_SOURCE` may name a
directory both accounts can read and write, where the bare repository that plays the git source is
created (default: a new world-writable directory under `/tmp`). The test deploys with `al deploy`,
runs one workflow with `al run … --on remote`, looks at the run's container on the other account
while its check sleeps, and removes the deployment, the env file, the run's storage and the images
it made on both accounts afterwards. Everything about placement that needs no second machine (the
scheduler, the record stream, the push, the take-over, lost and restart) is covered by
`kit/runtime/tests/test_placement.py`, `test_recordsync.py`, `test_selfrun.py`, `test_remoterun.py`
and `tests/tools/test_placement_runs.py`, with directories that play the hosts.
`test_selfrun.py` also runs one worktree+podman run on a fake host against
`tests/tools/fake_podman.py` and checks the hardening flags, the read-only `.git` mount of the clone
and the credentials on the recorded `podman run`; the same on real podman over ssh is
`tests/integration/test_remote_host.py` (opt-in).

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
tools/al verify LAYER --workflow zte        # only in the environment stack of ADW zte
```

The compiler proves a layer is coherent; `verify` proves its `(cmd …)` lines are true. It verifies
every distinct environment stack of the layer's ADWs once (the first ADW with that stack names it),
or only the stack of `--workflow NAME` (an unknown name is an error listing the ADWs). Per stack it
creates one detached worktree of the application at the default branch under
`<layer>/trees/verify-<random>` (the stack's worktree dir), runs the selected cmds there in
declaration order inside that stack (through the layer's own `adws/adw_modules`, so nix / podman
behave exactly as in a run), and removes the worktree. Cmds share that worktree, as the checks of one
run do. Per cmd it prints name, exit code and seconds (plus the last 15 output lines on failure),
with ` [stack]` at the end of the line when more than one stack ran, writes
`<layer>/agents/verify/<name>.log` (`<name>.<stack>.log` with several stacks), and records
`[{name, cmd, exit, seconds, log, at, workflow, stack}]` in `<layer>/agents/verify/report.json`. The
report keeps the latest result of every cmd in every stack: a cmd verified now replaces its earlier
row of the same stack, the others keep theirs (`at` says when each ran; a row of an older report
without `stack` is kept as stack `""`; `--json` prints only this invocation's rows). Exit status is 0
only when every verified cmd exited 0 in every stack; the summary counts one row per cmd and stack.
`deploy`, `start` and `install` are skipped unless named with `--cmd`. The application's `HEAD`,
current branch and index are compared before and after; a difference is an error. Files edited in
the checkout meanwhile are reported as a note, not an error: verify runs every cmd in its own
worktree, so it cannot tell them from the developer's own work in progress.

Just before the final `verify: N/M ok` line it prints what the layer's checks declare to cover, from the layer's
own `adws/adw_modules/affected.py` (`coverage_report`): `coverage: no check declares #:covers, so
every check runs for every change`; or `coverage: N check(s) declare no #:covers and run for every
change: A, B` and one `coverage: source path P is covered by no check: a change there runs every
check` per `(paths #:source …)` entry; or `coverage: every check declares #:covers and every source
path is covered`. With `--workflow NAME` only that workflow's check steps count (plus every review's
required checks and every stage's checks). The lines never change the exit status, go to standard
error with `--json` (stdout stays the rows), and a layer generated before the feature prints none.
`al sandbox LAYER doctor` prints the same lines before its summary line.
