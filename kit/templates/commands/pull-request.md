---
description: {{description}}
argument-hint: [plan-path] [review-verdict] [artefact-manifest-path]
---

# Pull Request

`{{command_name}}` in layer `{{name}}`: write the title and body of the pull request for this run. You produce text only; the workflow opens the pull request.

## Variables

PLAN_FILE: $1
REVIEW_VERDICT: $2
ARTEFACT_MANIFEST: $3
WORK_CLASS: ${class}
ADW_ID: ${adw_id}
ORIGINAL_PROMPT: ${prompt}

## Instructions

- Read-only. Do not edit, commit, push, switch branches, call the forge, or touch the main checkout.
- Your current directory is `${worktree}`, branch `${branch}`. The change is `git diff {{default_branch}}...HEAD`; read `git log {{default_branch}}..HEAD --oneline` and the diff stat, then the diff.
- Read PLAN_FILE for intent (fall back to ORIGINAL_PROMPT when it is empty). Describe what the diff does; where it departs from the plan, say so under Changes.
- REVIEW_VERDICT is the review's JSON (inline or a path), possibly empty. Report it faithfully: verdict, each blocker and each tech-debt issue. Never soften or omit an issue. Empty means "not reviewed".
- ARTEFACT_MANIFEST is a JSON list of artefacts, possibly empty. List each by path with what it proves. Do not describe an artefact you have not opened.
- Validation results come from the logs in the manifest. Do not claim a command passed without a log that shows it. No log means "not run".
- Title and body follow the Body Format below.
- Write for a reviewer with five minutes: what, why, how to verify. No filler, no praise, no restating the diff line by line.
- If the diff touches a protected path, put a line starting `REQUIRES HUMAN` at the top of Summary naming the path.

## Relevant Files

Protected paths:

{{protected_paths}}

{{fabric}}

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

{{focus}}

## Validation Commands

Do not run these. Report each from its log:

{{validation_commands}}

## Report

Output only this, with no preamble and no code fence: the title on the first line, one blank line, then the body in the Body Format.

{{output_contract}}
