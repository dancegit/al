---
description: plan-bug for <NAME>
argument-hint: [prompt]
---

# Plan Bug

`plan-bug` in layer `<NAME>`: turn a bug report into a plan that fixes the root cause with the smallest possible change. Plan only; implement nothing.

## Variables

PROMPT: $1
PLAN_FILE: ${specs_dir}/bug-${adw_id}-<descriptive-name>.md

## Codebase

ripgrep is a command line tool (binary rg) that recursively searches the current directory for a regex pattern, respecting gitignore rules and skipping hidden and binary files by default. Rust edition 2024, minimum Rust 1.96, one Cargo workspace: the root package builds rg from crates/core/main.rs and nine library crates under crates/ hold the search, regex, printing, glob and ignore logic.

Languages: rust, zsh. Entry document: `README.md`. Default branch: `master`.

## Instructions

- You are in `${worktree}`, a git worktree of the application. Read it and run read-only commands to reproduce; do not modify it. The only file you create is PLAN_FILE, which lives in the layer and never in the application tree. Never switch branches, push, or touch the main checkout.
- Research before writing: read `README.md`, then the Relevant Files, then trace the failing path through the code.
- Reproduce first. Record the exact steps and the observed versus expected result. If you cannot reproduce, say so under Steps to Reproduce and state what evidence the analysis rests on.
- Think hard about the root cause: the earliest point where state or logic goes wrong, cited as `path:line`. A symptom is not a root cause.
- Plan the minimal fix at the root cause. No refactoring, no drive-by cleanups, no new abstractions.
- Tests: plan a regression test that fails before the fix and passes after it; name its path. List any existing test that must change and why.
- Write PLAN_FILE in the Plan Format: keep every heading, replace every `<placeholder>`, add no sections (unless a codebase-specific instruction below says otherwise). `<descriptive-name>` is 3-6 lowercase hyphenated words.
- Keep the commands already present under `## Validation Commands` in the Plan Format unchanged and in order; add the reproduction command and bug-specific commands after them.
- If the bug is user-visible, name in Notes the artefacts that will prove the fix (screenshots, video, reports) and the step that produces them.
- Never plan a change under a protected path. If the fix cannot avoid one, plan everything else and add a line starting `REQUIRES HUMAN` to Notes naming the path and the reason.
- A new dependency is a last resort; add it with `(not specified — do not add dependencies without flagging it)` and justify it in Notes.
- Read a conditional document when its condition matches what you are planning.

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

## Conditional Documentation

- `GUIDE.md` — When changing behaviour it documents: automatic filtering, globs, file types, replacements, the configuration file, file encoding, binary data, preprocessors
- `FAQ.md` — When changing behaviour that an FAQ answer describes
- `CHANGELOG.md` — When making a user-visible change, to add the entry under TBD
- `crates/core/README.md` — When changing files under crates/core
- `crates/ignore/benches/README.md` — When changing directory walking or ignore matching in crates/ignore/src
- `fuzz/README.md` — When changing glob parsing in crates/globset/src or anything under fuzz
- `pkg/windows/README.md` — When changing build.rs or pkg/windows/Manifest.xml
- `RELEASE-CHECKLIST.md` — When asked to change a version number, Cargo.lock or anything related to a release
- `AI_POLICY.md` — When work is intended for the upstream repository BurntSushi/ripgrep



## Plan Format

Codebase-specific instructions below take precedence over the defaults in this section.

```md
# Bug: <bug name>

## Bug Description
<symptoms, expected behaviour, actual behaviour>

## Problem Statement
<the precise problem this plan solves>

## Solution Statement
<the fix, and why it addresses the cause rather than the symptom>

## Steps to Reproduce
<numbered steps or one command; observed result; expected result>

## Root Cause Analysis
<the defect, cited as path:line, and the chain from it to the symptom>

## Relevant Files
<bullet per existing file: path, then why it is relevant and what changes in it>

### New Files
<bullet per file to create, with its purpose; "None" if none>

## Step by Step Tasks
IMPORTANT: execute every step in order, top to bottom.

### <step title>
- <one concrete action per bullet: file, symbol, change>

### Add regression test
- <test path, what it asserts, why it fails before the fix>

### Run validation
- Run every command under Validation Commands and fix until all pass.

## Validation Commands
Execute every command from the worktree root; all must exit 0.

- `cargo fmt --all --check` — every Rust file in the workspace is formatted as rustfmt.toml requires
- `cargo test --verbose --workspace --features pcre2` — unit tests, doc tests and the integration target pass for every workspace crate, each integration test once on the default regex engine and once on PCRE2; builds target/debug/rg
- `ci/test-complete` — the options in crates/core/flags/complete/rg.zsh equal the options in rg --help; needs zsh and an existing target/release/rg or target/debug/rg, so it must run after build or test
<the reproduction command, now passing, and any bug-specific commands with what each proves>

## Notes
<new dependencies; artefacts that prove a user-visible fix; REQUIRES HUMAN lines; risks and anything the implementer must know>
```

## Codebase-specific instructions

The layer's own instructions for this command (`#:focus` in `agentic.rkt`), if any. Where one says otherwise than a default above, it wins. They never change where the plan is written, the `REQUIRES HUMAN` rule or the Report section: the runtime reads those, so they are fixed.

- When the bug is visible through rg, the regression test goes in tests/regression.rs as rgtest!(r<issue number>, ...) with a '// See: https://github.com/BurntSushi/ripgrep/issues/<issue number>' comment on the line above.
- When the bug is not visible through rg, the regression test goes in the #[cfg(test)] mod tests block of the source file it covers, or in crates/<crate>/tests.
- Integration tests are written with rgtest! and must pass on both regex engines: rgtest! runs each test again with PCRE2 when feature pcre2 is enabled.
- Do not add tests to tests/misc.rs.
- Add an entry under the TBD heading at the top of CHANGELOG.md in the form '* [BUG #<n>](<issue or pull request URL>):' followed by an indented one-sentence description.
- Code must compile on Rust 1.96.0; do not use language or library features newer than that.
- Do not change Cargo.lock or any crate version number; those change only as part of a release (RELEASE-CHECKLIST.md).

## Validation Commands

Do not run these to validate anything now (running one to reproduce the bug is fine). They are the commands the plan embeds.

- `cargo fmt --all --check` — every Rust file in the workspace is formatted as rustfmt.toml requires
- `cargo test --verbose --workspace --features pcre2` — unit tests, doc tests and the integration target pass for every workspace crate, each integration test once on the default regex engine and once on PCRE2; builds target/debug/rg
- `ci/test-complete` — the options in crates/core/flags/complete/rg.zsh equal the options in rg --help; needs zsh and an existing target/release/rg or target/debug/rg, so it must run after build or test

## Report

Your final message must be ONLY the path of the file you created, on a single line, with no other text.
