---
description: {{description}}
argument-hint: [prompt]
---
{{step_block}}

# Plan Chore

`{{command_name}}` in layer `{{name}}`: turn a high-level chore prompt into a surgical plan file that another agent can execute without asking a single question. Plan only; implement nothing.

## Variables

PROMPT: $1
PLAN_FILE: ${specs_dir}/chore-${adw_id}-<descriptive-name>.md

## Codebase

{{codebase_description}}

Languages: {{languages}}. Entry document: `{{readme}}`. Default branch: `{{default_branch}}`.

## Instructions

- You are in `${worktree}`, a git worktree of the application. Read it, do not modify it. The only file you create is PLAN_FILE, which lives in the layer and never in the application tree. Never switch branches, push, or touch the main checkout.
- Research before writing: read `{{readme}}`, then the Relevant Files, then follow the code until you can name every file the chore touches.
- Think hard about the smallest change that fully satisfies PROMPT. A chore changes no behaviour users rely on; if PROMPT needs a behaviour change, say so in Notes and plan only the chore part.
- Write PLAN_FILE in the Plan Format: keep every heading, replace every `<placeholder>`, add no sections (unless a codebase-specific instruction below says otherwise). `<descriptive-name>` is 3-6 lowercase hyphenated words.
- Be surgical: list only files that must change, name the function or block in each, and leave unrelated code, formatting and dependencies alone.
- Tests: name every test to add or update, with its path. If the chore needs none, state why in Notes.
- Keep the commands already present under `## Validation Commands` in the Plan Format unchanged and in order; add chore-specific commands only after them.
- If the result is user-visible, name in Notes the artefacts that will prove it (screenshots, video, reports) and the step that produces them.
- Never plan a change under a protected path. If the chore cannot be done without one, plan everything else and add a line starting `REQUIRES HUMAN` to Notes naming the path and the reason.
- A plan covers building and validating only: no step commits, merges, pushes, tags, releases or opens a pull request; delivery is the workflow's. Integration work (bringing in another branch or patch) is planned as 'apply onto the worktree, uncommitted', e.g. `git merge --no-commit --squash <ref>`, `git checkout <ref> -- <path>` or `git apply`.
- A new dependency is added with `{{package_manager}}` and listed in Notes with its justification.
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
# Chore: <chore name>

## Chore Description
<what must be done and why, in enough detail that the prompt is no longer needed>

## Relevant Files
<bullet per existing file: path, then why it is relevant and what changes in it>

## Step by Step Tasks
IMPORTANT: execute every step in order, top to bottom.

### <step title>
- <one concrete action per bullet: file, symbol, change>

### Run validation
- Run every command under Validation Commands and fix until all pass.

## Validation Commands
Execute every command from the worktree root; all must exit 0.

{{validation_commands}}
<additional chore-specific commands, each with what it proves; delete this line if none>

## Notes
<tests added or why none; new dependencies; artefacts that prove a user-visible result; REQUIRES HUMAN lines; anything the implementer must know>
```

## Codebase-specific instructions

The layer's own instructions for this command (`#:focus` in `agentic.rkt`), if any. Where one says otherwise than a default above, it wins. They never change where the plan is written, the `REQUIRES HUMAN` rule or the Report section: the runtime reads those, so they are fixed.

{{focus}}

## Validation Commands

Do not run these now. They are the commands the plan embeds; the implementer runs them.

{{validation_commands}}

## Report

{{output_contract}}
