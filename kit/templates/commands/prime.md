---
description: {{description}}
argument-hint: (none)
---
{{step_block}}

# Prime

`{{command_name}}` in layer `{{name}}`: build an accurate working picture of the codebase before doing anything else. Read-only.

## Variables

WORKTREE: ${worktree}
LAYER_DIR: ${layer_dir}

## Codebase

{{codebase_description}}

- Languages: {{languages}}
- Entry document: `{{readme}}`
- Dependencies are added with: `{{package_manager}}`
- Default branch: `{{default_branch}}`

## Instructions

- Read only. Do not edit, create, install, build or run anything that changes state. Never switch branches, push, or touch the main checkout.
- Your current directory is WORKTREE, a git worktree of the application. The agentic layer lives outside it, in LAYER_DIR; anything that is not application code belongs there.
- Learn the structure from the repository itself, not from assumptions about the language or framework.
- Read enough of each relevant path to know what lives there and how the parts call each other. Skim; do not read every file.
- Learn the commands below by name. They are the only supported way to install, check, build and run this codebase.
- Note the protected paths. They are off limits without a human.
- Do not read conditional documents now. Remember each condition so you read the document when it applies.
{{focus}}

## Relevant Files

{{relevant_files}}

Protected paths:

{{protected_paths}}

## Conditional Documentation

{{conditional_docs}}

{{fabric}}

## Workflow

1. Run `git ls-files` and read the shape of the tree.
2. Read `{{readme}}`.
3. Read the entry points and the main modules under the Relevant Files.
4. Read one representative test to learn how tests are written and named here.
5. Run `git log --oneline -15` to see how recent work is described.

## Validation Commands

Do not run these now. These are every command of the codebase, with what each proves:

{{all_commands}}

## Report

Summarise your understanding in at most 15 bullets: what the codebase does, how it is laid out, where the entry points and tests are, which command proves what, and which paths are protected. State anything you expected to find and did not.

{{output_contract}}
