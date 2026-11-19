---
description: {{description}}
argument-hint: [task]
---
{{step_block}}

# Build

`{{command_name}}` in layer `{{name}}`: implement a small, well-defined task directly, without a plan file, then prove it with every validation command.

## Variables

TASK: $1

## Codebase

{{codebase_description}}

- Languages: {{languages}}
- Entry document: `{{readme}}`
- Dependencies are added with: `{{package_manager}}`

## Instructions

- Work only inside `${worktree}` (your current directory, branch `${branch}`). Never switch branches, push, or touch the main checkout.
- Application code is the only thing you write to the worktree.
- Read `{{readme}}` and the Relevant Files that TASK touches before changing anything. Think hard about the smallest change that fully satisfies TASK.
- Do exactly what TASK asks. No refactoring, no extra features, no reformatting of code you did not need to change.
- Read each file before editing it and match the surrounding style, naming and error handling.
- Write or extend the tests of every behaviour you change, next to the existing tests for that code: they are what the targeted checks run for this change (test files the change adds or modifies always run).
- Add a dependency only when TASK cannot be done without it, using `{{package_manager}}`.
- Do not modify anything under a protected path. If TASK requires it, do the rest and report it on a line starting `REQUIRES HUMAN`.
- If TASK turns out to need a design decision or touches more than a handful of files, stop and report that it needs a plan instead of guessing.
- Tests are the source of truth: never weaken, skip or delete a test to make validation pass.
- Read a conditional document when its condition matches what you are changing.
{{focus}}

## Relevant Files

{{relevant_files}}

Protected paths:

{{protected_paths}}

## Conditional Documentation

{{conditional_docs}}

{{fabric}}

## Workflow

1. Locate the code TASK is about and read it.
2. Make the change and its test.
3. Run the Validation Commands from the worktree root.
4. On any failure: read the output, find the cause, fix the code, re-run from the first command. Repeat until every command exits 0.

## Validation Commands

All must exit 0 before you report.

{{validation_commands}}

## Report

- A bullet summary of what you changed and why, with `REQUIRES HUMAN` lines called out.
- The test files you added or changed, one per line, or "none" with the reason.
- The pass/fail result of each validation command.
- The output of `git diff --stat` (plus `git status --short` if you created files).

{{worktree_report}}

{{output_contract}}
