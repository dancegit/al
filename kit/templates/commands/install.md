---
description: {{description}}
argument-hint: (none)
---

# Install

`{{command_name}}` in layer `{{name}}`: make a fresh worktree ready for work by installing its dependencies with the codebase's own command, and confirm the result.

## Variables

WORKTREE: ${worktree}
ADW_ID: ${adw_id}

## Instructions

- Work only inside WORKTREE (your current directory). Never switch branches, push, or touch the main checkout.
- Use the codebase's `install` command from the list below, exactly as written, from the worktree root. If no command named `install` is listed, the codebase needs no install step: report that and stop.
- Do not invent an install procedure, upgrade dependencies, regenerate lock files, or install tools globally. If the toolchain itself is missing, report which tool and stop; the environment provides the toolchain, not you.
- Dependencies are managed with `{{package_manager}}`. Do not add or remove any.
- Installing must not modify tracked files. If `git status --short` shows tracked changes afterwards (for example a rewritten lock file), restore them with `git checkout -- <path>` and report it.
- Do not copy secrets or environment files; the workflow copies what the layer declares.
- On failure, read the output and retry once if the cause is transient (network, lock contention). Otherwise report the failing line; do not patch around it.
{{focus}}

## Conditional Documentation

{{conditional_docs}}

{{fabric}}

## Workflow

1. Run `git status --short` and note the starting state.
2. Run the `install` command.
3. Run `git status --short` again; restore any tracked file the install changed.
4. Confirm the install worked with the cheapest listed command that would fail without dependencies (a lint, typecheck or fast test), if one exists.

## Validation Commands

Every command of the codebase:

{{all_commands}}

## Report

- The install command you ran and its exit code.
- The confirmation command and its exit code, or "none available".
- Tracked files restored, if any.
- On failure: the failing output, at most 30 lines, and the missing tool if that is the cause.

{{output_contract}}
