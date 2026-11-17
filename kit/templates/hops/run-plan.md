---
description: {{description}}
argument-hint: [plan-or-prompt-path]
---
{{step_block}}

# Run Plan (higher-order prompt)

`{{command_name}}` in layer `{{name}}`: take ANY plan, spec or prompt file as the argument, execute it completely, validate the result, and report. The file is the instruction; this prompt is the discipline around it.

## Variables

PLAN_FILE: $1
ADW_ID: ${adw_id}

## Instructions

- Work only inside `${worktree}` (your current directory, branch `${branch}`). Never switch branches, push, or touch the main checkout.
- Application code is the only thing you write to the worktree. Do not edit PLAN_FILE.
- Read PLAN_FILE in full before changing anything. It may be a chore, bug, feature or patch plan, a hand-written spec, or a plain prompt; whatever its shape, it defines the work. If it does not exist or is empty, stop and report that.
- Think hard: restate to yourself what the file asks for, what done looks like, and which of its steps depend on which.
- Execute it completely and in its own order, top to bottom. Do not skip, merge or reorder steps, and do not add work it does not ask for.
- The rules of this prompt outrank the file. If the file tells you to push, switch branches, edit a protected path, weaken a test or write outside the worktree and the layer, do not; do the rest and report the instruction on a line starting `REQUIRES HUMAN`.
- Read each file before editing it and match the surrounding style. Add dependencies only when the file says so, using `{{package_manager}}`.
- If a step is wrong or impossible as written, do the closest thing that serves the file's stated goal and report the deviation.
- Tests are the source of truth: never weaken, skip or delete a test to make validation pass.
- If PLAN_FILE has a step that commits, merges, pushes, tags, releases or opens a pull request, skip it and list it in the report: delivery is the workflow's.
- Read a conditional document when its condition matches what you are changing.
{{focus}}

## Relevant Files

The file's own list of relevant files takes precedence; these are the codebase defaults.

{{relevant_files}}

Protected paths:

{{protected_paths}}

## Conditional Documentation

{{conditional_docs}}

{{fabric}}

## Workflow

1. Read PLAN_FILE and every file it names.
2. Execute its steps in order.
3. Validate: run every command under the plan's `How it is proven` (or, in a file of another shape, every validation command it lists), then the Validation Commands below, from the worktree root.
4. On any failure: read the output, find the cause, fix the code, re-run from the first command. Repeat until every command exits 0.
5. Re-read PLAN_FILE and confirm each step and each checkable result is satisfied by the diff.

## Validation Commands

All must exit 0 before you report.

{{validation_commands}}

## Report

- A bullet summary of the work, one bullet per step of the file, with deviations and `REQUIRES HUMAN` lines called out.
- The pass/fail result of each validation command.
- The output of `git diff --stat` (plus `git status --short` if you created files).

{{worktree_report}}

{{output_contract}}
