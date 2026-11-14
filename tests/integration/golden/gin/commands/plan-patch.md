---
description: plan-patch for <NAME>
argument-hint: [change-request] [original-plan-path]
---

# Plan Patch

`plan-patch` in layer `<NAME>`: write the smallest plan that resolves one change request or review blocker on work already built. Plan only; implement nothing.

## Variables

CHANGE_REQUEST: $1
ORIGINAL_PLAN: $2
ADW_ID: ${adw_id}
PATCH_FILE: ${specs_dir}/patch-${adw_id}-<descriptive-name>.md

## Instructions

- You are in `${worktree}`, a git worktree of the application that already contains the work being patched. Read it, do not modify it. The only file you create is PATCH_FILE, in the layer.
- Never switch branches, push, or touch the main checkout.
- Read ORIGINAL_PLAN when it is given: it defines what was asked for. The patch closes the gap between that and what exists; it does not reopen the design.
- Read `git diff master...HEAD` and any uncommitted changes to see what was built, then locate the exact lines CHANGE_REQUEST is about.
- Think hard about the minimal edit. One issue, one fix. No refactoring, no extras, no changes outside the issue's blast radius.
- If CHANGE_REQUEST cites an artefact (screenshot, video frame, report, log), open it and describe what it shows in Issue Summary.
- If CHANGE_REQUEST contradicts ORIGINAL_PLAN, follow CHANGE_REQUEST and record the contradiction in Issue Summary.
- If the fix affects behaviour covered by a test, plan the test change; if nothing covers it, plan the smallest test that would have caught the issue.
- Never plan a change under a protected path; if unavoidable, write a line starting `REQUIRES HUMAN` in Solution naming the path and the reason, and plan nothing under it.
- Write PATCH_FILE in exactly the Plan Format. `<descriptive-name>` is 3-6 lowercase hyphenated words.
- A code change adds or modifies tests in <name>_test.go in the same directory as the changed <name>.go.

## Relevant Files

- `README.md`
- `.`
- `binding`
- `render`
- `codec/json`
- `internal/bytesconv`
- `internal/fs`
- `ginS`
- `.`
- `binding`
- `render`
- `internal/bytesconv`
- `internal/fs`
- `ginS`
- `testdata`

Protected paths (do not plan changes here):

- `.github`
- `.goreleaser.yaml`
- `.golangci.yml`
- `codecov.yml`
- `LICENSE`
- `auth.go`
- `testdata/certificate`
- `testdata/protoexample/test.pb.go`



## Plan Format

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

- `make vet` — go vet reports nothing for all packages except /examples/
- `make test` — tests of packages gin, ginS, binding, render pass with go test -v -covermode=count; writes coverage.out
<the one command or check that shows this specific issue is gone>
```

## Validation Commands

Do not run these now. They are the commands the patch plan embeds.

- `make vet` — go vet reports nothing for all packages except /examples/
- `make test` — tests of packages gin, ginS, binding, render pass with go test -v -covermode=count; writes coverage.out

## Report

Your final message must be ONLY the path of the file you created, on a single line, with no other text.
