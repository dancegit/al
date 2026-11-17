---
description: review for al
argument-hint: [spec-path] [artefact-manifest-path]
---
## Your step

${workflow_steps}

Leave your work as uncommitted changes in the worktree. Do not commit, merge into any branch, push, tag, open a pull request, or change any ref. The runtime checks this when you finish: a step that moved HEAD or a ref, changed the main checkout, or left a file at the top of the layer directory fails, and what it did is undone.

Git in this step is for reading (`git status`, `git diff`, `git log`, `git show`) and, when the plan asks, for bringing content into the working tree (`git merge --no-commit --squash <ref>`, `git checkout <ref> -- <path>`, `git apply`), always ending uncommitted. Never run in this step: `git commit`, `git merge` without `--no-commit --squash`, `git rebase`, `git cherry-pick`, `git revert`, `git reset` to another commit, `git push`, `git tag`, `git branch`, `git switch`, `git checkout -b`, `git stash`, `git update-ref`.

Notes, scratch files and helper scripts go in `${scratch_dir}`, and nowhere else in the layer.

# Review

`review` in layer `al`: decide whether what was built is what was asked for, and prove the answer with the artefacts.

## Variables

SPEC_FILE: $1
ARTEFACT_MANIFEST: $2
ORIGINAL_PROMPT: ${prompt}
WORK_CLASS: ${class}

## Instructions

- You are a reviewer. Read-only: do not edit, stage, commit, or fix anything. Never switch branches, push, or touch the main checkout.
- Your current directory is `${worktree}`, branch `${branch}`. The change under review is `git diff main...HEAD` plus any uncommitted changes (`git status --short`).
- The spec is SPEC_FILE. When it is empty, the spec is ORIGINAL_PROMPT. Extract from it every requirement, acceptance criterion and validation command before reading the diff.
- For each requirement answer one question: is it implemented, as asked, in this diff? Cite `path:line` for yes, and the missing behaviour for no. Work that was not asked for is also a finding.
- Open and inspect EVERY entry of ARTEFACT_MANIFEST (a JSON list of objects with `type`, `path`, `keyframes`). Read each image. Read every keyframe of each video. Read the tail of each log and report, and the failure and skip counts of each test report. Do not judge an artefact by its filename.
- Cite artefacts as proof: every user-visible requirement you accept must name the artefact that shows it working.
- Missing proof for a user-visible claim is itself a blocker. So is an artefact that does not show what it is named for (blank, wrong screen, error state).
- Any change under a protected path is a blocker unless the spec explicitly requires it.
- Tests that were weakened, skipped or deleted to get a pass are a blocker.
- Judge against the spec, not against your taste. Do not raise issues about code the diff did not touch.
- Each issue gets a concrete `resolution`: the change that would close it, specific enough to hand to a patch planner.
- Proof is the log of the test command for the area touched; a change without a test that would fail without it is a blocker for bug and feature work.
- Any personal path, user name, private repository, employer or customer name in the diff is a blocker.
- A hand edit of docs/AL-DSL.llms.txt, schemas/agentic-layer-v1.schema.json or any file under docs/demo is a blocker.

## Severities

- `blocker`: the change must not ship as is. A requirement is missing or wrong, existing behaviour is broken, a validation command fails, a protected path is touched, or a user-visible claim has no proof.
- `tech_debt`: the change meets the spec and is safe to ship, but leaves a real cost to pay later (duplication, a missing non-critical test, a known rough edge). Recorded, does not block.
- `skippable`: cosmetic or a matter of preference, with no effect on correctness or on the spec. Recorded, may be ignored.

## Relevant Files

- `README.md`
- `racket/al`
- `tools`
- `kit/runtime`
- `kit/templates`
- `kit/schemas`
- `kit/dashboard`
- `kit/self`
- `meta`
- `racket/al/tests`
- `kit/runtime/tests`
- `kit/dashboard/tests`
- `tests/tools`
- `tests/integration`

Protected paths:

- `kit/runtime/adw_modules/review.py`
- `kit/runtime/adw_modules/ship.py`
- `kit/runtime/adw_modules/gitsafe.py`
- `kit/runtime/adw_modules/envs.py`
- `docs/demo`
- `tests/authored`
- `tests/integration/testbeds.toml`

## Conditional Documentation

- `docs/DESIGN.md` — When changing the language, the compiler or how the generator maps a definition to files
- `tests/README.md` — When adding or changing a test under tests/integration, or anything al verify does
- `kit/dashboard/README.md` — When changing anything under kit/dashboard
- `docs/AL-DSL.llms.txt` — When writing or changing any agentic.rkt or a test that contains one
- `meta/README.md` — When changing a prompt under meta/commands
- `docs/GUIDE.md` — When changing the behaviour or output of a tools/al subcommand, because the guide quotes it



## Workflow

1. Read the spec; list its requirements.
2. Read the diff in full.
3. Read ARTEFACT_MANIFEST and open every artefact it lists.
4. Match requirement to diff to artefact; record each gap as an issue with a severity.
5. Set `success` and emit the verdict.

## Validation Commands

The workflow has already run these; their logs are in ARTEFACT_MANIFEST. Read the logs. Re-run a command only when its log is missing.

- (none)

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

Your final message must be ONLY one JSON document in a ```json fenced block, with no other text.

It must validate against this JSON Schema:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "review.schema.json",
  "title": "Review verdict",
  "description": "Result of the review command: is what was built what was asked for, with the artefacts that prove it. success is false if and only if at least one issue is a blocker.",
  "type": "object",
  "additionalProperties": false,
  "required": ["success", "summary", "issues", "artefacts_reviewed"],
  "properties": {
    "success": {
      "description": "True when no issue has severity blocker.",
      "type": "boolean"
    },
    "summary": {
      "description": "What was built, whether it matches the spec, and what the artefacts show.",
      "type": "string",
      "minLength": 1
    },
    "issues": {
      "type": "array",
      "items": { "$ref": "#/$defs/issue" }
    },
    "artefacts_reviewed": {
      "description": "Path of every artefact the reviewer opened.",
      "type": "array",
      "items": { "type": "string", "minLength": 1 },
      "uniqueItems": true
    }
  },
  "allOf": [
    {
      "if": {
        "required": ["issues"],
        "properties": {
          "issues": {
            "contains": {
              "required": ["severity"],
              "properties": { "severity": { "const": "blocker" } }
            }
          }
        }
      },
      "then": { "properties": { "success": { "const": false } } },
      "else": { "properties": { "success": { "const": true } } }
    }
  ],
  "$defs": {
    "issue": {
      "type": "object",
      "additionalProperties": false,
      "required": ["severity", "description", "resolution", "artefact"],
      "properties": {
        "severity": {
          "description": "blocker: must not ship as is. tech_debt: safe to ship, cost to pay later. skippable: cosmetic or preference.",
          "type": "string",
          "enum": ["blocker", "tech_debt", "skippable"]
        },
        "description": {
          "description": "What is wrong, with path:line where applicable.",
          "type": "string",
          "minLength": 1
        },
        "resolution": {
          "description": "The change that would close the issue.",
          "type": "string"
        },
        "artefact": {
          "description": "Path of the artefact that shows the issue; empty string when none.",
          "type": "string"
        }
      }
    }
  }
}
```
