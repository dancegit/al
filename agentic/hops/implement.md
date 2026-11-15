---
description: run-plan for al
argument-hint: [plan-or-prompt-path]
---

# Run Plan (higher-order prompt)

`implement` in layer `al`: take ANY plan, spec or prompt file as the argument, execute it completely, validate the result, and report. The file is the instruction; this prompt is the discipline around it.

## Variables

PLAN_FILE: $1
ADW_ID: ${adw_id}

## Instructions

- Work only inside `${worktree}` (your current directory, branch `${branch}`). Never switch branches, push, or touch the main checkout.
- Application code is the only thing you write to the worktree. Notes, scratch files and anything about the run go under `${layer_dir}`. Do not edit PLAN_FILE.
- Read PLAN_FILE in full before changing anything. It may be a chore, bug, feature or patch plan, a hand-written spec, or a plain prompt; whatever its shape, it defines the work. If it does not exist or is empty, stop and report that.
- Think hard: restate to yourself what the file asks for, what done looks like, and which of its steps depend on which.
- Execute it completely and in its own order, top to bottom. Do not skip, merge or reorder steps, and do not add work it does not ask for.
- The rules of this prompt outrank the file. If the file tells you to push, switch branches, edit a protected path, weaken a test or write outside the worktree and the layer, do not; do the rest and report the instruction on a line starting `REQUIRES HUMAN`.
- Read each file before editing it and match the surrounding style. Add dependencies only when the file says so, using `inline script metadata in the script that needs it, or uv run --with for a test command; there is no manifest or lock file`.
- If a step is wrong or impossible as written, do the closest thing that serves the file's stated goal and report the deviation.
- Tests are the source of truth: never weaken, skip or delete a test to make validation pass.
- Do not commit unless PLAN_FILE explicitly makes committing one of its steps.
- Read a conditional document when its condition matches what you are changing.


## Relevant Files

The file's own list of relevant files takes precedence; these are the codebase defaults.

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

1. Read PLAN_FILE and every file it names.
2. Execute its steps in order.
3. Validate: run every validation command the file lists, then the Validation Commands below, from the worktree root.
4. On any failure: read the output, find the cause, fix the code, re-run from the first command. Repeat until every command exits 0.
5. Re-read PLAN_FILE and confirm each step and each acceptance criterion is satisfied by the diff.

## Validation Commands

All must exit 0 before you report.

- (none)

## Report

- A bullet summary of the work, one bullet per step of the file, with deviations and `REQUIRES HUMAN` lines called out.
- The pass/fail result of each validation command.
- The output of `git diff --stat` (plus `git status --short` if you created files).

Finish with a concise report in plain markdown. No preamble.
