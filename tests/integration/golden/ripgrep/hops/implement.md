---
description: run-plan for <NAME>
argument-hint: [plan-or-prompt-path]
---

# Run Plan (higher-order prompt)

`implement` in layer `<NAME>`: take ANY plan, spec or prompt file as the argument, execute it completely, validate the result, and report. The file is the instruction; this prompt is the discipline around it.

## Variables

PLAN_FILE: $1
ADW_ID: ${adw_id}

## Instructions

- Work only inside `${worktree}` (your current directory, branch `${branch}`). Never switch branches, push, or touch the main checkout.
- Application code is the only thing you write to the worktree. Notes, scratch files and anything about the run go under `${layer_dir}`. Do not edit PLAN_FILE.
- Read PLAN_FILE in full before changing anything. It may be a chore, bug, feature or patch plan, a hand-written spec, or a plain prompt; whatever its shape, it defines the work. If it does not exist or is empty, stop and report that.
- Think hard: restate to yourself what the file asks for, what done looks like, and which of its steps depend on which.
- Execute it completely and in its own order, top to bottom. Do not skip, merge or reorder steps, and do not add work it does not ask for.
- The rules of this prompt outrank the file. If the file tells you to push, switch branches, edit a protected path, weaken a test or write outside the worktree and the layer, do not; do the rest and report the instruction on a line starting `REQUIRES HUMAN`.
- Read each file before editing it and match the surrounding style. Add dependencies only when the file says so, using `(not specified — do not add dependencies without flagging it)`.
- If a step is wrong or impossible as written, do the closest thing that serves the file's stated goal and report the deviation.
- Tests are the source of truth: never weaken, skip or delete a test to make validation pass.
- Do not commit unless PLAN_FILE explicitly makes committing one of its steps.
- Read a conditional document when its condition matches what you are changing.


## Relevant Files

The file's own list of relevant files takes precedence; these are the codebase defaults.

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

Protected paths:

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



## Workflow

1. Read PLAN_FILE and every file it names.
2. Execute its steps in order.
3. Validate: run every validation command the file lists, then the Validation Commands below, from the worktree root.
4. On any failure: read the output, find the cause, fix the code, re-run from the first command. Repeat until every command exits 0.
5. Re-read PLAN_FILE and confirm each step and each acceptance criterion is satisfied by the diff.

## Validation Commands

All must exit 0 before you report.

- `cargo build --verbose --workspace` — the rg binary and every workspace crate compile; produces target/debug/rg
- `cargo test --verbose --workspace --features pcre2` — unit tests, doc tests and the integration target pass for every workspace crate, each integration test once on the default regex engine and once on PCRE2; builds target/debug/rg

## Report

- A bullet summary of the work, one bullet per step of the file, with deviations and `REQUIRES HUMAN` lines called out.
- The pass/fail result of each validation command.
- The output of `git diff --stat` (plus `git status --short` if you created files).

Finish with a concise report in plain markdown. No preamble.
