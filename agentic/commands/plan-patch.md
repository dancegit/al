---
description: plan-patch for al
argument-hint: [change-request] [original-plan-path]
---

# Plan Patch

`plan-patch` in layer `al`: write the smallest plan that resolves one change request or review blocker on work already built. Plan only; implement nothing.

## Variables

CHANGE_REQUEST: $1
ORIGINAL_PLAN: $2
ADW_ID: ${adw_id}
PATCH_FILE: ${specs_dir}/patch-${adw_id}-<descriptive-name>.md

## Instructions

- You are in `${worktree}`, a git worktree of the application that already contains the work being patched. Read it, do not modify it. The only file you create is PATCH_FILE, in the layer.
- Never switch branches, push, or touch the main checkout.
- Read ORIGINAL_PLAN when it is given: it defines what was asked for. The patch closes the gap between that and what exists; it does not reopen the design.
- Read `git diff main...HEAD` and any uncommitted changes to see what was built, then locate the exact lines CHANGE_REQUEST is about.
- Think hard about the minimal edit. One issue, one fix. No refactoring, no extras, no changes outside the issue's blast radius.
- If CHANGE_REQUEST cites an artefact (screenshot, video frame, report, log), open it and describe what it shows in Issue Summary.
- If CHANGE_REQUEST contradicts ORIGINAL_PLAN, follow CHANGE_REQUEST and record the contradiction in Issue Summary.
- If the fix affects behaviour covered by a test, plan the test change; if nothing covers it, plan the smallest test that would have caught the issue.
- Never plan a change under a protected path; if unavoidable, write a line starting `REQUIRES HUMAN` in Solution naming the path and the reason, and plan nothing under it.
- Write PATCH_FILE in the Plan Format (unless a codebase-specific instruction below says otherwise). `<descriptive-name>` is 3-6 lowercase hyphenated words.

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

Protected paths (do not plan changes here):

- `racket/al/private`
- `kit/runtime/adw_modules/review.py`
- `kit/runtime/adw_modules/ship.py`
- `kit/runtime/adw_modules/gitsafe.py`
- `kit/runtime/adw_modules/envs.py`
- `docs/AL-DSL.llms.txt`
- `schemas/agentic-layer-v1.schema.json`
- `docs/demo`
- `tests/authored`
- `tests/integration/testbeds.toml`



## Plan Format

Codebase-specific instructions below take precedence over the defaults in this section.

```md
# Patch: <patch name>

## Issue Summary
<the issue in one or two sentences; original plan path; evidence (file:line, artefact path)>

## Solution
<the minimal change and why it resolves the issue>

## Steps
IMPORTANT: execute every step in order, top to bottom.

1. <file, symbol, exact change>
2. <test added or updated, with path>
3. Run every command under Validation and fix until all pass.

## Validation
Execute every command from the worktree root; all must exit 0.

- `PLTCOLLECTS="$PWD/racket:" raco test racket/al/tests/compiler-test.rkt` — The language rules: the compiler accepts the examples and rejects each invalid definition with the documented error
- `(cd kit/runtime && env -u AL_APP_ROOT uv run --with pytest pytest -q)` — The runtime unit tests pass, including the shipping gate, the sandbox and attempts to escape the container
- `env -u AL_APP_ROOT uv run --with pytest --with pyyaml --with jsonschema pytest tests/tools -q` — The al command itself works: eject, in-repo layers, al improve, dashboard generation
- `(cd kit/dashboard && uv run --with pytest pytest -q tests)` — The dashboard's model and HTTP server work: unit and HTTP tests, no browser
<the one command or check that shows this specific issue is gone>
```

## Codebase-specific instructions

The layer's own instructions for this command (`#:focus` in `agentic.rkt`), if any. Where one says otherwise than a default above, it wins. They never change where the plan is written, the `REQUIRES HUMAN` rule or the Report section: the runtime reads those, so they are fixed.



## Validation Commands

Do not run these now. They are the commands the patch plan embeds.

- `PLTCOLLECTS="$PWD/racket:" raco test racket/al/tests/compiler-test.rkt` — The language rules: the compiler accepts the examples and rejects each invalid definition with the documented error
- `(cd kit/runtime && env -u AL_APP_ROOT uv run --with pytest pytest -q)` — The runtime unit tests pass, including the shipping gate, the sandbox and attempts to escape the container
- `env -u AL_APP_ROOT uv run --with pytest --with pyyaml --with jsonschema pytest tests/tools -q` — The al command itself works: eject, in-repo layers, al improve, dashboard generation
- `(cd kit/dashboard && uv run --with pytest pytest -q tests)` — The dashboard's model and HTTP server work: unit and HTTP tests, no browser

## Report

Your final message must be ONLY the path of the file you created, on a single line, with no other text.
