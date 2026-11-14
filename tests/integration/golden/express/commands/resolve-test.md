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
- Fix the cause with the minimal change. No refactoring, no unrelated cleanups, no dependency changes unless the failure is a missing or wrong dependency (then use `npm install`).
- Tests are the source of truth. Never weaken an assertion, mark a test skipped or expected-to-fail, delete a test, loosen a lint or type rule, or edit a check's configuration to make it pass.
- If the test itself is wrong (it contradicts the plan, or asserts behaviour that was deliberately changed), do not edit it on your own judgement: say which test, why it is wrong, what the correct assertion would be, and stop.
- Do not modify anything under a protected path. If the fix lies there, stop and report it on a line starting `REQUIRES HUMAN`.
- Fix only this failure. Other failures you notice go in the report, not in the diff.
- Do not commit.
- JavaScript is indented with 2 spaces, files end with a newline, no trailing whitespace, comparisons use === except == null, no unused variables.

## Relevant Files

- `Readme.md`
- `lib`
- `index.js`
- `test`

Protected paths:

- `.github`
- `LICENSE`
- `.npmrc`

## Conditional Documentation

- `examples/README.md` — When adding, renaming or removing a directory under examples/
- `History.md` — When a change alters behaviour that an earlier release entry describes



## Workflow

1. Reproduce: run FAILING_COMMAND.
2. Isolate: narrow to the single failing test or message; re-run just that if the tool allows it.
3. Diagnose: state the root cause as `path:line` and one sentence.
4. Fix: make the minimal change.
5. Verify: re-run FAILING_COMMAND, exactly as given. Loop to step 2 until it exits 0 or you hit a stop condition above.

## Validation Commands

FAILING_COMMAND itself is the validation. For reference, every command of the codebase:

- **install**: `npm install` — dependencies and devDependencies resolve and install into node_modules
- **lint**: `npm run lint` — eslint reports no violation of .eslintrc.yml in any JavaScript file of the repository
- **test**: `npm run test-ci` — the whole mocha suite (test/ and test/acceptance/) passes with global-leak checking, under nyc, writing coverage/lcov.info and a text coverage table to stdout
- **test-fast**: `npm test` — the whole mocha suite (test/ and test/acceptance/) passes with global-leak checking, without coverage

## Report

- Root cause: `path:line` and one sentence.
- Fix: what changed, one bullet per file.
- Result: the exit code of the final run of FAILING_COMMAND, or the stop condition you hit (not reproducible, test is wrong, `REQUIRES HUMAN`).
- The output of `git diff --stat`.

Finish with a concise report in plain markdown. No preamble.
