---
description: pull-request for al
argument-hint: [plan-path] [review-verdict] [artefact-manifest-path]
---
## Your step

${workflow_steps}

Leave your work as uncommitted changes in the worktree. Do not commit, merge into any branch, push, tag, open a pull request, or change any ref. The runtime checks this when you finish: a step that moved HEAD or a ref, changed the main checkout, or left a file at the top of the layer directory fails, and what it did is undone.

Git in this step is for reading (`git status`, `git diff`, `git log`, `git show`) and, when the plan asks, for bringing content into the working tree (`git merge --no-commit --squash <ref>`, `git checkout <ref> -- <path>`, `git apply`), always ending uncommitted. Never run in this step: `git commit`, `git merge` without `--no-commit --squash`, `git rebase`, `git cherry-pick`, `git revert`, `git reset` to another commit, `git push`, `git tag`, `git branch`, `git switch`, `git checkout -b`, `git stash`, `git update-ref`.

Notes, scratch files and helper scripts go in `${scratch_dir}`, and nowhere else in the layer.

# Pull Request

`pull-request` in layer `al`: write the title and body of the pull request for this run. You produce text only; the workflow opens the pull request.

## Variables

PLAN_FILE: $1
REVIEW_VERDICT: $2
ARTEFACT_MANIFEST: $3
WORK_CLASS: ${class}
ADW_ID: ${adw_id}
ORIGINAL_PROMPT: ${prompt}

## Instructions

- Read-only. Do not edit, commit, push, switch branches, call the forge, or touch the main checkout.
- Your current directory is `${worktree}`, branch `${branch}`. The change is `git diff main...HEAD`; read `git log main..HEAD --oneline` and the diff stat, then the diff.
- Read PLAN_FILE for intent (fall back to ORIGINAL_PROMPT when it is empty). Describe what the diff does; where it departs from the plan, say so under Changes.
- REVIEW_VERDICT is the review's JSON (inline or a path), possibly empty. Report it faithfully: verdict, each blocker and each tech-debt issue. Never soften or omit an issue. Empty means "not reviewed".
- ARTEFACT_MANIFEST is a JSON list of artefacts, possibly empty. List each by path with what it proves. Do not describe an artefact you have not opened.
- Validation results come from the logs in the manifest. Do not claim a command passed without a log that shows it. No log means "not run".
- Title and body follow the Body Format below.
- Write for a reviewer with five minutes: what, why, how to verify. No filler, no praise, no restating the diff line by line.
- If the diff touches a protected path, put a line starting `REQUIRES HUMAN` at the top of Summary naming the path.

## Relevant Files

Protected paths:

- `kit/runtime/adw_modules/review.py`
- `kit/runtime/adw_modules/ship.py`
- `kit/runtime/adw_modules/gitsafe.py`
- `kit/runtime/adw_modules/envs.py`
- `docs/demo`
- `tests/authored`
- `tests/integration/testbeds.toml`



## Body Format

Codebase-specific instructions below take precedence over the defaults in this section.

- Title: `<class>: <summary>`, at most 72 characters, imperative mood, where `<class>` is WORK_CLASS.
- Body:

```md
## Summary
<2-4 sentences: what this changes and why>

## Plan
<the plan's file name (never an absolute path: this text is published), then one line on what it asked for; "No plan" when empty>

## Changes
<bullets grouped by area: behaviour first, then notable implementation points and deviations from the plan>

## Validation results
<bullet per validation command: name, pass or fail or not run, the log that shows it>

## Review verdict
<pass or fail or not reviewed; then each issue as: severity, description, resolution>

## Artefacts
<bullet per artefact: its path relative to the artefacts directory, then what it proves; "None" when empty>

ADW: <adw id>
```

## Codebase-specific instructions

The layer's own instructions for this command (`#:focus` in `agentic.rkt`), if any. Where one says otherwise than a default above, it wins. They never change the read-only rule, the `REQUIRES HUMAN` line or the Report section: the runtime reads those, so they are fixed.



## Validation Commands

Do not run these. Report each from its log:

- (none)

## Report

Output only this, with no preamble and no code fence: the title on the first line, one blank line, then the body in the Body Format.

Finish with a concise report in plain markdown. No preamble.
