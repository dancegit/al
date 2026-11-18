---
description: {{description}}
argument-hint: [prompt]
---
{{step_block}}

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
- Reproduce first. Record the exact steps and the observed versus expected result. If you cannot reproduce, say so under How to reproduce and state what evidence the analysis rests on.
- Think hard about the cause: the earliest point where state or logic goes wrong, cited as `path:line` under Cause. A symptom is not a cause.
- Plan the minimal fix at the cause. No refactoring, no drive-by cleanups, no new abstractions.
- Tests: plan a regression test that fails before the fix and passes after it; name its path under Tests and give it a step. List any existing test that must change and why.
- Write PLAN_FILE in the Plan Format: keep every heading, replace every `<placeholder>`, add no sections (unless a codebase-specific instruction below says otherwise). `<descriptive-name>` is 3-6 lowercase hyphenated words.
- Keep the commands already present under `### Commands` in the Plan Format unchanged and in order; add the reproduction command and bug-specific commands after them. Checkable results are observable: each one maps to a test, a command or a named artefact.
- If the bug is user-visible, name under Checkable results the artefacts that will prove the fix (screenshots, video, reports) and the step that produces them.
- Never plan a change under a protected path. If the fix cannot avoid one, plan everything else and add a line starting `REQUIRES HUMAN` under What is left out naming the path and the reason.
- A plan covers building and validating only: no step commits, merges, pushes, tags, releases or opens a pull request; delivery is the workflow's. Integration work (bringing in another branch or patch) is planned as 'apply onto the worktree, uncommitted', e.g. `git merge --no-commit --squash <ref>`, `git checkout <ref> -- <path>` or `git apply`.
- A new dependency is a last resort; add it with `{{package_manager}}` and justify it under What could go wrong.
- Read a conditional document when its condition matches what you are planning.

## Relevant Files

{{relevant_files}}

Protected paths (do not plan changes here):

{{protected_paths}}

## Conditional Documentation

{{conditional_docs}}

{{fabric}}

## Plan Format

Codebase-specific instructions below take precedence over the defaults in this section.

```md
# Bug: <bug name>

## What is asked and why
<one paragraph: the symptoms, the expected and the actual behaviour, and the fix, with why it addresses the cause rather than the symptom>

## What exists today
<bullet per existing file: path, then why it matters here and what changes in it; then a bullet per file to create with its purpose, if any>

### How to reproduce
<numbered steps or one command; observed result; expected result>

### Cause
<the defect, cited as path:line, and the chain from it to the symptom>

## The change, step by step
IMPORTANT: execute every step in order, top to bottom.

### <step title>
- <one concrete action per bullet: file, symbol, change>

### Add regression test
- <test path, what it asserts, why it fails before the fix>

### Run the proof
- Run every command under How it is proven and fix until all pass.

## How it is proven

### Tests
<test path, then what it asserts; existing tests that change and why>

### Commands
Execute every command from the worktree root; all must exit 0.

{{validation_commands}}
<the reproduction command, now passing, and any bug-specific commands with what each proves>

### Checkable results
<bullet per observable result, each tied to a test, a command or a named artefact>

## What could go wrong
<risks; new dependencies with their justification; anything the implementer must know; "Nothing known" if none>

## What is left out
<REQUIRES HUMAN lines; related defects not fixed here; follow-ups; "Nothing" if none>
```

## Codebase-specific instructions

The layer's own instructions for this command (`#:focus` in `agentic.rkt`), if any. Where one says otherwise than a default above, it wins. They never change where the plan is written, the `REQUIRES HUMAN` rule or the Report section: the runtime reads those, so they are fixed.

{{focus}}

## Validation Commands

Do not run these to validate anything now (running one to reproduce the bug is fine). They are the commands the plan embeds.

{{validation_commands}}

## Report

{{output_contract}}
