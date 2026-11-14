---
description: resolve-test for <NAME>
argument-hint: [failing-command] [output-tail]
---

# Resolve Failing Check

`resolve-test` in layer `<NAME>`: make one failing check pass by fixing its root cause in the application code, with the smallest possible change.

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
- Fix the cause with the minimal change. No refactoring, no unrelated cleanups, no dependency changes unless the failure is a missing or wrong dependency (then use `(not specified — do not add dependencies without flagging it)`).
- Tests are the source of truth. Never weaken an assertion, mark a test skipped or expected-to-fail, delete a test, loosen a lint or type rule, or edit a check's configuration to make it pass.
- If the test itself is wrong (it contradicts the plan, or asserts behaviour that was deliberately changed), do not edit it on your own judgement: say which test, why it is wrong, what the correct assertion would be, and stop.
- Do not modify anything under a protected path. If the fix lies there, stop and report it on a line starting `REQUIRES HUMAN`.
- Fix only this failure. Other failures you notice go in the report, not in the diff.
- Do not commit.
- C code must compile with -Wextra -Wall -Wno-unused-parameter -Wno-unused-function -Woverlength-strings -Wstrict-prototypes.
- Failing test output is in test-suite.log and tests/*.log.
- Never fix a failure by editing tests/man.test or tests/manonig.test; they are generated from docs/content/manual/dev/manual.yml.

## Relevant Files

- `README.md`
- `src`
- `Makefile.am`
- `configure.ac`
- `config/m4`
- `m4`
- `scripts`
- `tests`

Protected paths:

- `.github`
- `Dockerfile`
- `COPYING`
- `SECURITY.md`
- `KEYS`
- `sig`
- `scripts/version`
- `jq.spec`
- `vendor`
- `.gitmodules`
- `src/lexer.c`
- `src/lexer.h`
- `src/parser.c`
- `src/parser.h`
- `tests/man.test`
- `tests/manonig.test`
- `jq.1.prebuilt`
- `docs/Pipfile.lock`
- `docs/public/CNAME`

## Conditional Documentation

- `docs/README.md` — When changing anything under docs/, or regenerating jq.1.prebuilt, tests/man.test or tests/manonig.test
- `docs/content/manual/dev/manual.yml` — When adding or changing a builtin, operator or command-line option
- `docs/manual_schema.yml` — When editing any manual.yml
- `tests/setup` — When adding or changing a test driver script under tests/
- `.github/workflows/ci.yml` — When changing configure options or cross-compilation behaviour
- `NEWS.md` — When a change is user-visible and belongs in release notes
- `SECURITY.md` — When the task concerns a security vulnerability



## Workflow

1. Reproduce: run FAILING_COMMAND.
2. Isolate: narrow to the single failing test or message; re-run just that if the tool allows it.
3. Diagnose: state the root cause as `path:line` and one sentence.
4. Fix: make the minimal change.
5. Verify: re-run FAILING_COMMAND, exactly as given. Loop to step 2 until it exits 0 or you hit a stop condition above.

## Validation Commands

FAILING_COMMAND itself is the validation. For reference, every command of the codebase:

- **install**: `git submodule update --init && autoreconf -i && ./configure --disable-docs --with-oniguruma=builtin --enable-static` — The Oniguruma submodule is fetched and the build system is generated and configured; must run once in a fresh checkout before build or test
- **build**: `make -j"$(nproc)"` — libjq and the jq binary compile and link with -Wextra -Wall
- **test**: `make check VERBOSE=yes` — Every driver in TESTS of Makefile.am passes against the built ./jq; writes test-suite.log and tests/*.log
- **docs**: `(cd docs && pipenv sync && pipenv run python3 build_website.py)` — The website renders from docs/content into docs/output/

## Report

- Root cause: `path:line` and one sentence.
- Fix: what changed, one bullet per file.
- Result: the exit code of the final run of FAILING_COMMAND, or the stop condition you hit (not reproducible, test is wrong, `REQUIRES HUMAN`).
- The output of `git diff --stat`.

Finish with a concise report in plain markdown. No preamble.
