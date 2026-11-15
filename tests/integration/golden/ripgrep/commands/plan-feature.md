---
description: plan-feature for <NAME>
argument-hint: [prompt]
---

# Plan Feature

`plan-feature` in layer `<NAME>`: turn a feature request into a phased plan that another agent can implement end to end without asking a single question. Plan only; implement nothing.

## Variables

PROMPT: $1
PLAN_FILE: ${specs_dir}/feature-${adw_id}-<descriptive-name>.md

## Codebase

ripgrep is a command line tool (binary rg) that recursively searches the current directory for a regex pattern, respecting gitignore rules and skipping hidden and binary files by default. Rust edition 2024, minimum Rust 1.96, one Cargo workspace: the root package builds rg from crates/core/main.rs and nine library crates under crates/ hold the search, regex, printing, glob and ignore logic.

Languages: rust, zsh. Entry document: `README.md`. Default branch: `master`.

## Instructions

- You are in `${worktree}`, a git worktree of the application. Read it, do not modify it. The only file you create is PLAN_FILE, which lives in the layer and never in the application tree. Never switch branches, push, or touch the main checkout.
- Research before writing: read `README.md`, then the Relevant Files, then the closest existing feature. Follow its patterns, naming and file layout; do not invent a new architecture. Think hard about the smallest design that delivers the user value in PROMPT. Leave out everything PROMPT does not ask for.
- Write PLAN_FILE in the Plan Format: keep every heading, replace every `<placeholder>`, add no sections (unless a codebase-specific instruction below says otherwise). `<descriptive-name>` is 3-6 lowercase hyphenated words.
- Be surgical: list only files that must change or be created, and name the function, type or block in each.
- Tests: name every test to add, with its path, under Testing Strategy, and give each a step in Step by Step Tasks. Tests come before or with the code they cover, never after validation. Acceptance Criteria are observable and checkable: each one maps to a test, a validation command or a named artefact.
- Keep the commands already present under `## Validation Commands` in the Plan Format unchanged and in order; add feature-specific commands after them.
- If the feature is user-visible, name in Notes the artefacts that will prove it (screenshots, video, reports), where they land, and the step that produces them.
- Never plan a change under a protected path. If the feature cannot avoid one, plan everything else and add a line starting `REQUIRES HUMAN` to Notes naming the path and the reason.
- A new dependency is added with `(not specified — do not add dependencies without flagging it)` and justified in Notes. Read a conditional document when its condition matches what you are planning.

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
# Feature: <feature name>

## Feature Description
<what the feature does and the value it delivers>

## User Story
As a <type of user>, I want to <action or goal>, so that <benefit>.

## Problem Statement
<the problem or opportunity this feature addresses>

## Solution Statement
<the approach, and how it fits the existing design>

## Relevant Files
<bullet per existing file: path, why it is relevant, what changes in it; then, under a `### New Files` heading, a bullet per file to create with its purpose>

## Implementation Plan
### Phase 1: Foundation
<shared groundwork needed before the feature itself: types, schema, config, scaffolding>
### Phase 2: Core Implementation
<the feature's main behaviour>
### Phase 3: Integration
<wiring into existing entry points, interfaces and documentation>

## Step by Step Tasks
IMPORTANT: execute every step in order, top to bottom. The last step runs every command under Validation Commands and fixes until all pass.
### <step title>
- <one concrete action per bullet: file, symbol, change>

## Testing Strategy
### Unit Tests
<test path, then what it asserts>
### Integration Tests
<test path, then the flow it covers>
### Edge Cases
<each edge case and the test that covers it>

## Acceptance Criteria
<checkable statements, each tied to a test, command or artefact>

## Validation Commands
Execute every command from the worktree root; all must exit 0.

- `cargo fmt --all --check` — every Rust file in the workspace is formatted as rustfmt.toml requires
- `cargo test --verbose --workspace --features pcre2` — unit tests, doc tests and the integration target pass for every workspace crate, each integration test once on the default regex engine and once on PCRE2; builds target/debug/rg
- `ci/test-complete` — the options in crates/core/flags/complete/rg.zsh equal the options in rg --help; needs zsh and an existing target/release/rg or target/debug/rg, so it must run after build or test
- `RUSTDOCFLAGS="-D warnings" cargo doc --no-deps --document-private-items --workspace` — rustdoc for every workspace crate, private items included, builds with no warnings
<additional feature-specific commands, each with what it proves; delete this line if none>

## Notes
<new dependencies; artefacts that prove the user-visible result; REQUIRES HUMAN lines; deliberate omissions and follow-ups>
```

## Codebase-specific instructions

The layer's own instructions for this command (`#:focus` in `agentic.rkt`), if any. Where one says otherwise than a default above, it wins. They never change where the plan is written, the `REQUIRES HUMAN` rule or the Report section: the runtime reads those, so they are fixed.

- A test for a new feature of rg goes in tests/feature.rs, written with rgtest! and asserting the exact standard output, standard error or exit status of rg; do not add tests to tests/misc.rs.
- A new or changed command line flag is one Flag implementation in crates/core/flags/defs.rs, listed in FLAGS, with its documentation in that implementation.
- Every option added to or changed in crates/core/flags/defs.rs must also be added to crates/core/flags/complete/rg.zsh so that ci/test-complete prints OK.
- Integration tests must pass on both regex engines: rgtest! runs each test again with PCRE2 when feature pcre2 is enabled.
- A unit test goes in the #[cfg(test)] mod tests block of the source file it covers.
- Add an entry under the TBD heading at the top of CHANGELOG.md in the form '* [FEATURE #<n>](<URL>):' or '* [PERF #<n>](<URL>):' followed by an indented one-sentence description.
- Code must compile on Rust 1.96.0; do not use language or library features newer than that.
- Do not change Cargo.lock or any crate version number; those change only as part of a release (RELEASE-CHECKLIST.md).

## Validation Commands

Do not run these now. They are the commands the plan embeds; the implementer runs them.

- `cargo fmt --all --check` — every Rust file in the workspace is formatted as rustfmt.toml requires
- `cargo test --verbose --workspace --features pcre2` — unit tests, doc tests and the integration target pass for every workspace crate, each integration test once on the default regex engine and once on PCRE2; builds target/debug/rg
- `ci/test-complete` — the options in crates/core/flags/complete/rg.zsh equal the options in rg --help; needs zsh and an existing target/release/rg or target/debug/rg, so it must run after build or test
- `RUSTDOCFLAGS="-D warnings" cargo doc --no-deps --document-private-items --workspace` — rustdoc for every workspace crate, private items included, builds with no warnings

## Report

Your final message must be ONLY the path of the file you created, on a single line, with no other text.
