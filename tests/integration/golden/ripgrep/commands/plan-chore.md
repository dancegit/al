---
description: plan-chore for <NAME>
argument-hint: [prompt]
---

# Plan Chore

`plan-chore` in layer `<NAME>`: turn a high-level chore prompt into a surgical plan file that another agent can execute without asking a single question. Plan only; implement nothing.

## Variables

PROMPT: $1
PLAN_FILE: ${specs_dir}/chore-${adw_id}-<descriptive-name>.md

## Codebase

ripgrep is a command line tool (binary rg) that recursively searches the current directory for a regex pattern, respecting gitignore rules and skipping hidden and binary files by default. Rust edition 2024, minimum Rust 1.96, one Cargo workspace: the root package builds rg from crates/core/main.rs and nine library crates under crates/ hold the search, regex, printing, glob and ignore logic.

Languages: rust, zsh. Entry document: `README.md`. Default branch: `master`.

## Instructions

- You are in `${worktree}`, a git worktree of the application. Read it, do not modify it. The only file you create is PLAN_FILE, which lives in the layer and never in the application tree. Never switch branches, push, or touch the main checkout.
- Research before writing: read `README.md`, then the Relevant Files, then follow the code until you can name every file the chore touches.
- Think hard about the smallest change that fully satisfies PROMPT. A chore changes no behaviour users rely on; if PROMPT needs a behaviour change, say so in Notes and plan only the chore part.
- Write PLAN_FILE in the Plan Format: keep every heading, replace every `<placeholder>`, add no sections (unless a codebase-specific instruction below says otherwise). `<descriptive-name>` is 3-6 lowercase hyphenated words.
- Be surgical: list only files that must change, name the function or block in each, and leave unrelated code, formatting and dependencies alone.
- Tests: name every test to add or update, with its path. If the chore needs none, state why in Notes.
- Keep the commands already present under `## Validation Commands` in the Plan Format unchanged and in order; add chore-specific commands only after them.
- If the result is user-visible, name in Notes the artefacts that will prove it (screenshots, video, reports) and the step that produces them.
- Never plan a change under a protected path. If the chore cannot be done without one, plan everything else and add a line starting `REQUIRES HUMAN` to Notes naming the path and the reason.
- A new dependency is added with `(not specified — do not add dependencies without flagging it)` and listed in Notes with its justification.
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
# Chore: <chore name>

## Chore Description
<what must be done and why, in enough detail that the prompt is no longer needed>

## Relevant Files
<bullet per existing file: path, then why it is relevant and what changes in it>

## Step by Step Tasks
IMPORTANT: execute every step in order, top to bottom.

### <step title>
- <one concrete action per bullet: file, symbol, change>

### Run validation
- Run every command under Validation Commands and fix until all pass.

## Validation Commands
Execute every command from the worktree root; all must exit 0.

- `cargo fmt --all --check` — every Rust file in the workspace is formatted as rustfmt.toml requires
- `cargo test --verbose --workspace --features pcre2` — unit tests, doc tests and the integration target pass for every workspace crate, each integration test once on the default regex engine and once on PCRE2; builds target/debug/rg
<additional chore-specific commands, each with what it proves; delete this line if none>

## Notes
<tests added or why none; new dependencies; artefacts that prove a user-visible result; REQUIRES HUMAN lines; anything the implementer must know>
```

## Codebase-specific instructions

The layer's own instructions for this command (`#:focus` in `agentic.rkt`), if any. Where one says otherwise than a default above, it wins. They never change where the plan is written, the `REQUIRES HUMAN` rule or the Report section: the runtime reads those, so they are fixed.

- A chore touches no behaviour of rg: formatting, rustdoc or comment fixes, edits to README.md, GUIDE.md, FAQ.md or a crate README.md, or a test-only change.
- When rustdoc comments change, add the docs command to the plan's validation: rustdoc must build with no warnings.
- Code must compile on Rust 1.96.0; do not use language or library features newer than that.
- Do not change Cargo.lock or any crate version number; those change only as part of a release (RELEASE-CHECKLIST.md).

## Validation Commands

Do not run these now. They are the commands the plan embeds; the implementer runs them.

- `cargo fmt --all --check` — every Rust file in the workspace is formatted as rustfmt.toml requires
- `cargo test --verbose --workspace --features pcre2` — unit tests, doc tests and the integration target pass for every workspace crate, each integration test once on the default regex engine and once on PCRE2; builds target/debug/rg

## Report

Your final message must be ONLY the path of the file you created, on a single line, with no other text.
