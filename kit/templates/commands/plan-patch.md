---
description: {{description}}
argument-hint: [change-request] [original-plan-path]
---
{{step_block}}

# Plan Patch

`{{command_name}}` in layer `{{name}}`: write the smallest plan that resolves one change request or review blocker on work already built. Plan only; implement nothing.

## Variables

CHANGE_REQUEST: $1
ORIGINAL_PLAN: $2
ADW_ID: ${adw_id}
PATCH_FILE: ${specs_dir}/patch-${adw_id}-<descriptive-name>.md

## Instructions

- You are in `${worktree}`, a git worktree of the application that already contains the work being patched. Read it, do not modify it. The only file you create is PATCH_FILE, in the layer.
- Never switch branches, push, or touch the main checkout.
- Read ORIGINAL_PLAN when it is given: it defines what was asked for. The patch closes the gap between that and what exists; it does not reopen the design.
- Read `git diff {{default_branch}}...HEAD` and any uncommitted changes to see what was built, then locate the exact lines CHANGE_REQUEST is about.
- Think hard about the minimal edit. One issue, one fix. No refactoring, no extras, no changes outside the issue's blast radius.
- If CHANGE_REQUEST cites an artefact (screenshot, video frame, report, log), open it and describe what it shows under What is asked and why.
- If CHANGE_REQUEST contradicts ORIGINAL_PLAN, follow CHANGE_REQUEST and record the contradiction under What is asked and why.
- If the fix affects behaviour covered by a test, plan the test change; if nothing covers it, plan the smallest test that would have caught the issue.
- Never plan a change under a protected path; if unavoidable, write a line starting `REQUIRES HUMAN` under What is left out naming the path and the reason, and plan nothing under it.
- A plan covers building and validating only: no step commits, merges, pushes, tags, releases or opens a pull request; delivery is the workflow's. Integration work (bringing in another branch or patch) is planned as 'apply onto the worktree, uncommitted', e.g. `git merge --no-commit --squash <ref>`, `git checkout <ref> -- <path>` or `git apply`.
- Write PATCH_FILE in the Plan Format (unless a codebase-specific instruction below says otherwise). `<descriptive-name>` is 3-6 lowercase hyphenated words.

## Relevant Files

{{relevant_files}}

Protected paths (do not plan changes here):

{{protected_paths}}

{{fabric}}

## Plan Format

Codebase-specific instructions below take precedence over the defaults in this section.

```md
# Patch: <patch name>

## What is asked and why
<the issue in one or two sentences; original plan path; evidence (file:line, artefact path)>

## The change, step by step
IMPORTANT: execute every step in order, top to bottom.

<the minimal change and why it resolves the issue>

1. <file, symbol, exact change>
2. <test added or updated, with path>
3. Run every command under How it is proven and fix until all pass.

## How it is proven
Execute every command from the worktree root; all must exit 0.

{{validation_commands}}
<the one command or check that shows this specific issue is gone>

## What is left out
<REQUIRES HUMAN lines; anything the change request touches that this patch does not do; "Nothing" if none>
```

## Codebase-specific instructions

The layer's own instructions for this command (`#:focus` in `agentic.rkt`), if any. Where one says otherwise than a default above, it wins. They never change where the plan is written, the `REQUIRES HUMAN` rule or the Report section: the runtime reads those, so they are fixed.

{{focus}}

## Validation Commands

Do not run these now. They are the commands the patch plan embeds.

{{validation_commands}}

## Report

{{output_contract}}
