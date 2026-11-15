---
description: resolve-test for al
argument-hint: [failing-command] [output-tail]
---

# Resolve Failing Check

`resolve-test` in layer `al`: make one failing check pass by fixing its root cause in the application code, with the smallest possible change.

## Variables

FAILING_COMMAND: $1
OUTPUT_TAIL: $2
PLAN_FILE: ${plan}

## Instructions

- Work only inside `${worktree}` (your current directory, branch `${branch}`). Never switch branches, push, or touch the main checkout.
- Reproduce before you touch anything: run FAILING_COMMAND from the worktree root and read the full output. OUTPUT_TAIL is a hint, not the whole story.
- If it passes on reproduction, run it once more. If it still passes, change nothing and report it as not reproducible, with both outputs summarised.
- Think hard about the root cause: the earliest wrong line, not the line that raised. Read the failing test and the code it exercises before forming a fix.
- If PLAN_FILE is set, read it: the fix must keep what the plan asked for.
- Fix the cause with the minimal change. No refactoring, no unrelated cleanups, no dependency changes unless the failure is a missing or wrong dependency (then use `inline script metadata in the script that needs it, or uv run --with for a test command; there is no manifest or lock file`).
- Tests are the source of truth. Never weaken an assertion, mark a test skipped or expected-to-fail, delete a test, loosen a lint or type rule, or edit a check's configuration to make it pass.
- If the test itself is wrong (it contradicts the plan, or asserts behaviour that was deliberately changed), do not edit it on your own judgement: say which test, why it is wrong, what the correct assertion would be, and stop.
- Do not modify anything under a protected path. If the fix lies there, stop and report it on a line starting `REQUIRES HUMAN`.
- Fix only this failure. Other failures you notice go in the report, not in the diff.
- Do not commit.
- Fix the code or the new test, never weaken or delete an existing test to make a command pass.
- A failure in tests/integration golden snapshots after a template change is resolved with --update-golden and a reviewed snapshot diff, not by editing snapshots by hand.

## Relevant Files

- `README.md`
- `racket/al`
- `tools`
- `kit/runtime`
- `kit/templates`
- `kit/schemas`
- `kit/dashboard`
- `kit/self`
- `meta`
- `racket/al/tests`
- `kit/runtime/tests`
- `kit/dashboard/tests`
- `tests/tools`
- `tests/integration`

Protected paths:

- `kit/runtime/adw_modules/review.py`
- `kit/runtime/adw_modules/ship.py`
- `kit/runtime/adw_modules/gitsafe.py`
- `kit/runtime/adw_modules/envs.py`
- `docs/demo`
- `tests/authored`
- `tests/integration/testbeds.toml`

## Conditional Documentation

- `docs/DESIGN.md` — When changing the language, the compiler or how the generator maps a definition to files
- `tests/README.md` — When adding or changing a test under tests/integration, or anything al verify does
- `kit/dashboard/README.md` — When changing anything under kit/dashboard
- `docs/AL-DSL.llms.txt` — When writing or changing any agentic.rkt or a test that contains one
- `meta/README.md` — When changing a prompt under meta/commands
- `docs/GUIDE.md` — When changing the behaviour or output of a tools/al subcommand, because the guide quotes it



## Workflow

1. Reproduce: run FAILING_COMMAND.
2. Isolate: narrow to the single failing test or message; re-run just that if the tool allows it.
3. Diagnose: state the root cause as `path:line` and one sentence.
4. Fix: make the minimal change.
5. Verify: re-run FAILING_COMMAND, exactly as given. Loop to step 2 until it exits 0 or you hit a stop condition above.

## Validation Commands

FAILING_COMMAND itself is the validation. For reference, every command of the codebase:

- **test-compiler**: `PLTCOLLECTS="$PWD/racket:" raco test racket/al/tests/compiler-test.rkt` — The language rules: the compiler accepts the examples and rejects each invalid definition with the documented error
- **test-runtime**: `(cd kit/runtime && env -u AL_APP_ROOT uv run --with pytest pytest -q)` — The runtime unit tests pass, including the shipping gate, the sandbox and attempts to escape the container
- **test-tools**: `env -u AL_APP_ROOT uv run --with pytest --with pyyaml --with jsonschema pytest tests/tools -q` — The al command itself works: eject, in-repo layers, al improve, dashboard generation
- **test-dashboard**: `(cd kit/dashboard && uv run --with pytest pytest -q tests)` — The dashboard's model and HTTP server work: unit and HTTP tests, no browser
- **test-dashboard-browser**: `(cd kit/dashboard && uv run --with pytest --with playwright pytest -q tests)` — The dashboard tests plus the real-browser tests of the page in the host's Chrome
- **test-integration**: `env -u AL_APP_ROOT uv run --with pytest --with pyyaml --with jsonschema pytest tests/integration -q` — The default tier of full runs on the five pinned example repositories passes with the mock agent, and generated prompts equal the golden snapshots

## Report

- Root cause: `path:line` and one sentence.
- Fix: what changed, one bullet per file.
- Result: the exit code of the final run of FAILING_COMMAND, or the stop condition you hit (not reproducible, test is wrong, `REQUIRES HUMAN`).
- The output of `git diff --stat`.

Finish with a concise report in plain markdown. No preamble.
