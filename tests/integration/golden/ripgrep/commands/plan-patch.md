---
description: plan-patch for <NAME>
argument-hint: [change-request] [original-plan-path]
---

# Plan Patch

`plan-patch` in layer `<NAME>`: write the smallest plan that resolves one change request or review blocker on work already built. Plan only; implement nothing.

## Variables

CHANGE_REQUEST: $1
ORIGINAL_PLAN: $2
ADW_ID: ${adw_id}
PATCH_FILE: ${specs_dir}/patch-${adw_id}-<descriptive-name>.md

## Instructions

- You are in `${worktree}`, a git worktree of the application that already contains the work being patched. Read it, do not modify it. The only file you create is PATCH_FILE, in the layer.
- Never switch branches, push, or touch the main checkout.
- Read ORIGINAL_PLAN when it is given: it defines what was asked for. The patch closes the gap between that and what exists; it does not reopen the design.
- Read `git diff master...HEAD` and any uncommitted changes to see what was built, then locate the exact lines CHANGE_REQUEST is about.
- Think hard about the minimal edit. One issue, one fix. No refactoring, no extras, no changes outside the issue's blast radius.
- If CHANGE_REQUEST cites an artefact (screenshot, video frame, report, log), open it and describe what it shows in Issue Summary.
- If CHANGE_REQUEST contradicts ORIGINAL_PLAN, follow CHANGE_REQUEST and record the contradiction in Issue Summary.
- If the fix affects behaviour covered by a test, plan the test change; if nothing covers it, plan the smallest test that would have caught the issue.
- Never plan a change under a protected path; if unavoidable, write a line starting `REQUIRES HUMAN` in Solution naming the path and the reason, and plan nothing under it.
- Write PATCH_FILE in exactly the Plan Format. `<descriptive-name>` is 3-6 lowercase hyphenated words.
- Do not change Cargo.lock or any crate version number; those change only as part of a release (RELEASE-CHECKLIST.md).

## Relevant Files

- `README.md`
- `crates/core`
- `crates/matcher/src`
- `crates/regex/src`
- `crates/pcre2/src`
- `crates/searcher/src`
- `crates/printer/src`
- `crates/grep`
- `crates/cli/src`
- `crates/globset/src`
- `crates/ignore/src`
- `build.rs`
- `tests`
- `crates/ignore/tests`
- `crates/matcher/tests`

Protected paths (do not plan changes here):

- `.github`
- `ci`
- `pkg`
- `HomebrewFormula`
- `Cargo.lock`
- `fuzz/Cargo.lock`
- `.cargo/config.toml`
- `RELEASE-CHECKLIST.md`
- `AI_POLICY.md`
- `CONTRIBUTING.md`
- `COPYING`
- `LICENSE-MIT`
- `UNLICENSE`



## Plan Format

```md
# Patch: <patch name>

## Issue Summary
<the issue in one or two sentences; original plan path; evidence (file:line, artefact path)>

## Solution
<the minimal change and why it resolves the issue>

## Steps
IMPORTANT: execute every step in order, top to bottom.

1. <file, symbol, exact change>
2. <test added or updated, with path>
3. Run every command under Validation and fix until all pass.

## Validation
Execute every command from the worktree root; all must exit 0.

- `cargo fmt --all --check` — every Rust file in the workspace is formatted as rustfmt.toml requires
- `cargo test --verbose --workspace --features pcre2` — unit tests, doc tests and the integration target pass for every workspace crate, each integration test once on the default regex engine and once on PCRE2; builds target/debug/rg
<the one command or check that shows this specific issue is gone>
```

## Validation Commands

Do not run these now. They are the commands the patch plan embeds.

- `cargo fmt --all --check` — every Rust file in the workspace is formatted as rustfmt.toml requires
- `cargo test --verbose --workspace --features pcre2` — unit tests, doc tests and the integration target pass for every workspace crate, each integration test once on the default regex engine and once on PCRE2; builds target/debug/rg

## Report

Your final message must be ONLY the path of the file you created, on a single line, with no other text.
