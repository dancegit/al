---
description: Validate the {{expert_name}} expertise against the code and update it
argument-hint: [check-git-diff true|false] [focus-area]
---

# Expert Self-Improve: {{expert_name}}

Keep the `{{expert_name}}` expert of layer `{{name}}` accurate: validate its expertise file against the code as it is now, correct what has drifted, and record what was learned. Finding nothing to change is a valid result.

## Variables

CHECK_GIT_DIFF: $1 (default `false`)
FOCUS_AREA: $2 (optional; empty means the whole domain)
EXPERTISE_FILE: {{expertise_path}}
MAX_LINES: {{expert_max_lines}}

## Domain

{{expert_domain}}

Codebase paths the expertise is validated against:

{{expert_paths}}

## Instructions

- You write exactly one file, EXPERTISE_FILE. Do not edit application code. Never switch branches, push, or touch the main checkout.
- Your current directory is `${worktree}`, a git worktree of the application. The code there is the source of truth; the expertise is a mental model of it.
- Record only what you verified in the code during this run. No guesses, no plans, no history of what used to be true, no generalities about the language or framework.
- Every entry carries a `path` or `path:line` reference that resolves today.
- Prefer correcting and tightening over appending. An accurate, shorter file beats a longer one.
- Keep the file's top-level keys, their order and its header comment exactly as they are. It must remain valid YAML.
- Do not invent discrepancies to have something to report. If the expertise matches the code, change nothing.
- Stay inside the domain: code outside the covered paths is recorded only as a dependency or caller, in one line.
{{focus}}

{{fabric}}

## Workflow

1. Check git diff (only if CHECK_GIT_DIFF is `true`): run `git diff {{default_branch}}...HEAD --stat` and `git status --short`, restricted to the covered paths. If nothing under them changed, and FOCUS_AREA is empty, report "no changes in domain" and stop. Otherwise the changed files are where validation starts.
2. Read expertise: read EXPERTISE_FILE in full.
3. Validate against code: open the files each entry references (all of them, or those touching FOCUS_AREA or the diff) and compare claim to code: paths, line references, names, flows, test locations.
4. Identify discrepancies: list what is wrong, stale, missing or no longer exists. Also list what the code now does that the expertise does not mention and that a future change would need.
5. Update the file: fix wrong entries, remove dead ones, add missing ones, refresh line references.
6. Enforce the line limit: count the lines. Above MAX_LINES, cut in this order until under: duplicated facts, detail that restates the code, the least important `key_files`, resolved `known_issues`. Never cut by truncating the file.
7. Validation checks: run the checks below; repair and repeat until they pass.
8. Report.

## Validation Commands

- The file parses as YAML: use any YAML parser available on this machine; if none is, re-read the file and check indentation, quoting and list syntax by hand.
- `wc -l` on EXPERTISE_FILE is at most MAX_LINES.
- The top-level keys and the header comment are unchanged.
- Spot-check five references from the updated file: each path exists and each line reference points at what the entry claims.

## Report

- Result: updated, or no changes needed, or no changes in domain.
- Discrepancies found and fixed, one bullet each: entry, what was wrong, what it says now.
- New knowledge added, one bullet each.
- Line count before and after, against MAX_LINES.

{{output_contract}}
