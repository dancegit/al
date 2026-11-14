---
description: {{description}}
argument-hint: [prompt]
---

# Plan Bug

`{{command_name}}` in layer `{{name}}`: turn a bug report into a plan that fixes the root cause with the smallest possible change. Plan only; implement nothing.

## Variables

PROMPT: $1
PLAN_FILE: ${specs_dir}/bug-${adw_id}-<descriptive-name>.md

## Codebase

{{codebase_description}}

Languages: {{languages}}. Entry document: `{{readme}}`. Default branch: `{{default_branch}}`.

## Instructions

- You are in `${worktree}`, a git worktree of the application. Read it and run read-only commands to reproduce; do not modify it. The only file you create is PLAN_FILE, which lives in the layer and never in the application tree. Never switch branches, push, or touch the main checkout.
- Research before writing: read `{{readme}}`, then the Relevant Files, then trace the failing path through the code.
- Reproduce first. Record the exact steps and the observed versus expected result. If you cannot reproduce, say so under Steps to Reproduce and state what evidence the analysis rests on.
- Think hard about the root cause: the earliest point where state or logic goes wrong, cited as `path:line`. A symptom is not a root cause.
- Plan the minimal fix at the root cause. No refactoring, no drive-by cleanups, no new abstractions.
- Tests: plan a regression test that fails before the fix and passes after it; name its path. List any existing test that must change and why.
- Write PLAN_FILE in exactly the Plan Format: keep every heading, replace every `<placeholder>`, add no sections. `<descriptive-name>` is 3-6 lowercase hyphenated words.
- Keep the commands already present under `## Validation Commands` in the Plan Format unchanged and in order; add the reproduction command and bug-specific commands after them.
- If the bug is user-visible, name in Notes the artefacts that will prove the fix (screenshots, video, reports) and the step that produces them.
- Never plan a change under a protected path. If the fix cannot avoid one, plan everything else and add a line starting `REQUIRES HUMAN` to Notes naming the path and the reason.
- A new dependency is a last resort; add it with `{{package_manager}}` and justify it in Notes.
- Read a conditional document when its condition matches what you are planning.
{{focus}}

## Relevant Files

{{relevant_files}}

Protected paths (do not plan changes here):

{{protected_paths}}

## Conditional Documentation

{{conditional_docs}}

{{fabric}}

## Plan Format

```md
# Bug: <bug name>

## Bug Description
<symptoms, expected behaviour, actual behaviour>

## Problem Statement
<the precise problem this plan solves>

## Solution Statement
<the fix, and why it addresses the cause rather than the symptom>

## Steps to Reproduce
<numbered steps or one command; observed result; expected result>

## Root Cause Analysis
<the defect, cited as path:line, and the chain from it to the symptom>

## Relevant Files
<bullet per existing file: path, then why it is relevant and what changes in it>

### New Files
<bullet per file to create, with its purpose; "None" if none>

## Step by Step Tasks
IMPORTANT: execute every step in order, top to bottom.

### <step title>
- <one concrete action per bullet: file, symbol, change>

### Add regression test
- <test path, what it asserts, why it fails before the fix>

### Run validation
- Run every command under Validation Commands and fix until all pass.

## Validation Commands
Execute every command from the worktree root; all must exit 0.

{{validation_commands}}
<the reproduction command, now passing, and any bug-specific commands with what each proves>

## Notes
<new dependencies; artefacts that prove a user-visible fix; REQUIRES HUMAN lines; risks and anything the implementer must know>
```

## Validation Commands

Do not run these to validate anything now (running one to reproduce the bug is fine). They are the commands the plan embeds.

{{validation_commands}}

## Report

{{output_contract}}
