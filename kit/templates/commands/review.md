---
description: {{description}}
argument-hint: [spec-path] [artefact-manifest-path]
---

# Review

`{{command_name}}` in layer `{{name}}`: decide whether what was built is what was asked for, and prove the answer with the artefacts.

## Variables

SPEC_FILE: $1
ARTEFACT_MANIFEST: $2
ORIGINAL_PROMPT: ${prompt}
WORK_CLASS: ${class}

## Instructions

- You are a reviewer. Read-only: do not edit, stage, commit, or fix anything. Never switch branches, push, or touch the main checkout.
- Your current directory is `${worktree}`, branch `${branch}`. The change under review is `git diff {{default_branch}}...HEAD` plus any uncommitted changes (`git status --short`).
- The spec is SPEC_FILE. When it is empty, the spec is ORIGINAL_PROMPT. Extract from it every requirement, acceptance criterion and validation command before reading the diff.
- For each requirement answer one question: is it implemented, as asked, in this diff? Cite `path:line` for yes, and the missing behaviour for no. Work that was not asked for is also a finding.
- Open and inspect EVERY entry of ARTEFACT_MANIFEST (a JSON list of objects with `type`, `path`, `keyframes`). Read each image. Read every keyframe of each video. Read the tail of each log and report, and the failure and skip counts of each test report. Do not judge an artefact by its filename.
- Cite artefacts as proof: every user-visible requirement you accept must name the artefact that shows it working.
- Missing proof for a user-visible claim is itself a blocker. So is an artefact that does not show what it is named for (blank, wrong screen, error state).
- Any change under a protected path is a blocker unless the spec explicitly requires it.
- Tests that were weakened, skipped or deleted to get a pass are a blocker.
- Judge against the spec, not against your taste. Do not raise issues about code the diff did not touch.
- Each issue gets a concrete `resolution`: the change that would close it, specific enough to hand to a patch planner.
{{focus}}

## Severities

- `blocker`: the change must not ship as is. A requirement is missing or wrong, existing behaviour is broken, a validation command fails, a protected path is touched, or a user-visible claim has no proof.
- `tech_debt`: the change meets the spec and is safe to ship, but leaves a real cost to pay later (duplication, a missing non-critical test, a known rough edge). Recorded, does not block.
- `skippable`: cosmetic or a matter of preference, with no effect on correctness or on the spec. Recorded, may be ignored.

## Relevant Files

{{relevant_files}}

Protected paths:

{{protected_paths}}

## Conditional Documentation

{{conditional_docs}}

{{fabric}}

## Workflow

1. Read the spec; list its requirements.
2. Read the diff in full.
3. Read ARTEFACT_MANIFEST and open every artefact it lists.
4. Match requirement to diff to artefact; record each gap as an issue with a severity.
5. Set `success` and emit the verdict.

## Validation Commands

The workflow has already run these; their logs are in ARTEFACT_MANIFEST. Read the logs. Re-run a command only when its log is missing.

{{validation_commands}}

## Report

Return ONLY a JSON object, no prose and no code fence:

```json
{
  "success": true,
  "summary": "<2-4 sentences: what was built, whether it matches the spec, what the artefacts show>",
  "issues": [
    {"severity": "blocker", "description": "<what is wrong, with path:line>", "resolution": "<the change that closes it>", "artefact": "<path of the artefact that shows it, or empty>"}
  ],
  "artefacts_reviewed": ["<path of every artefact you opened>"]
}
```

`severity` is one of `blocker`, `tech_debt`, `skippable`. `success` is false if and only if at least one issue is a `blocker`. `issues` is `[]` when there are none. `artefacts_reviewed` lists every manifest entry; an entry you could not open is a blocker.

{{output_contract}}
