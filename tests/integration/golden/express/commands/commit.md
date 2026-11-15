---
description: commit for <NAME>
argument-hint: [extra-context]
---

# Commit

`commit` in layer `<NAME>`: stage this run's changes in the worktree and record them as one clean, conventional commit. Nothing leaves the machine.

## Variables

EXTRA_CONTEXT: $1
WORK_CLASS: ${class}
ADW_ID: ${adw_id}
PLAN_FILE: ${plan}

## Instructions

- Work only inside `${worktree}` (your current directory, branch `${branch}`). Commit on the branch you are on. Never switch branches, push, or touch the main checkout.
- Read `git status --short` and `git diff` first. The message describes what the diff does, not what the plan hoped for.
- Stage by path. Stage the files that belong to this change; do not use a blanket add.
- Never stage: test outputs and artefacts (reports, screenshots, recordings, coverage), build output, caches, editor files, environment files, or anything that looks like a secret. If such a file is untracked and not ignored, leave it and name it in the report.
- If the diff touches a protected path, do not stage that path; commit the rest and report the path on a line starting `REQUIRES HUMAN`.
- Write the message in the Message Format below.
- One commit. Do not amend, rebase, squash or rewrite existing commits. Do not skip hooks.
- If a commit hook fails, read its output. Fix what it reports when the fix is mechanical (formatting, lint autofix), re-stage, and commit again. Otherwise stop and report the hook output.
- If there is nothing to commit, change nothing and say so.

## Message Format

Codebase-specific instructions below take precedence over the defaults in this section.

- Subject, at most 72 characters, imperative mood, no trailing period: `<class>: <summary>` where `<class>` is WORK_CLASS.
- Body, only when the subject cannot carry it: blank line, then up to 5 bullets on what changed and why. Last line: `ADW: <adw id>`.

## Codebase-specific instructions

The layer's own instructions for this command (`#:focus` in `agentic.rkt`), if any. Where one says otherwise than a default above, it wins. They never change the staging rules, the protected-path rule (`REQUIRES HUMAN`) or the Report section: those keep the change safe and are what the runtime reads, so they are fixed.



## Relevant Files

Protected paths (never staged by this command):

- `.github`
- `LICENSE`
- `.npmrc`



## Workflow

1. `git status --short` and `git diff`.
2. `git add <path> ...` for each file of the change.
3. `git diff --cached --stat` and confirm only intended files are staged.
4. `git commit` with the message.
5. `git log -1 --format='%H %s'`.

## Validation Commands

Not run here; the workflow's checks run them. The commit is valid when `git status --short` shows no remaining file of this change and `git log -1` shows your message.

- `npm run lint` — eslint reports no violation of .eslintrc.yml in any JavaScript file of the repository
- `npm run test-ci` — the whole mocha suite (test/ and test/acceptance/) passes with global-leak checking, under nyc, writing coverage/lcov.info and a text coverage table to stdout

## Report

The commit hash and subject line, then any files deliberately left unstaged with the reason.

Finish with a concise report in plain markdown. No preamble.
