---
description: {{description}}
argument-hint: [plan-path]
---

# Implement

`{{command_name}}` in layer `{{name}}`: implement the plan at the given path completely, then prove it with every validation command.

## Variables

PLAN_FILE: $1

## Instructions

- Work only inside `${worktree}` (your current directory, branch `${branch}`). Never switch branches, push, or touch the main checkout.
- Application code is the only thing you write to the worktree. Notes, scratch files and anything about the run go under `${layer_dir}`. Do not edit PLAN_FILE.
- Read PLAN_FILE in full before changing anything. Think hard about how its steps fit together and what each one needs from the ones before it.
- Implement every step, in order, top to bottom. Do not skip, merge or reorder steps, and do not add work the plan does not ask for.
- Read each file before editing it and match the surrounding style, naming and error handling.
- Write the tests the plan lists. A step that names a test is not done until that test exists and passes.
- Add dependencies only when the plan says so, using `{{package_manager}}`.
- Do not modify anything under a protected path. If a step requires it, skip that step, finish the rest, and report it on a line starting `REQUIRES HUMAN`.
- If a step is wrong or impossible as written, do the closest thing that serves the plan's stated goal and report the deviation. Do not silently drop it.
- Tests are the source of truth: never weaken, skip or delete a test to make validation pass.
- Do not commit. Committing is a separate step of the workflow.
- Read a conditional document when its condition matches what you are changing.
{{focus}}

## Relevant Files

The plan's own Relevant Files section takes precedence; these are the codebase defaults.

{{relevant_files}}

Protected paths:

{{protected_paths}}

## Conditional Documentation

{{conditional_docs}}

{{fabric}}

## Workflow

1. Read PLAN_FILE and every file it names.
2. Implement the steps in order.
3. Run the plan's validation commands, then the Validation Commands below, from the worktree root.
4. On any failure: read the output, find the cause, fix the code, re-run from the first command. Repeat until every command exits 0.
5. Re-read the plan once more and confirm each step and each acceptance criterion is satisfied by the diff.

## Validation Commands

All must exit 0 before you report.

{{validation_commands}}

## Report

- A bullet summary of what you implemented, one bullet per plan step, with deviations and `REQUIRES HUMAN` lines called out.
- The pass/fail result of each validation command.
- The output of `git diff --stat` (plus `git status --short` if you created files).

{{output_contract}}
