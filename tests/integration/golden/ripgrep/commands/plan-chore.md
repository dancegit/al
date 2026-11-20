---
description: plan-chore for <NAME>
argument-hint: [prompt]
---
## Your step

${workflow_steps}

Leave your work as uncommitted changes in the worktree. Do not commit, merge into any branch, push, tag, open a pull request, or change any ref. The runtime checks this when you finish: a step that moved HEAD or a ref, changed the main checkout, or left a file at the top of the layer directory fails, and what it did is undone.

Git in this step is for reading (`git status`, `git diff`, `git log`, `git show`) and, when the plan asks, for bringing content into the working tree (`git merge --no-commit --squash <ref>`, `git checkout <ref> -- <path>`, `git apply`), always ending uncommitted. Never run in this step: `git commit`, `git merge` without `--no-commit --squash`, `git rebase`, `git cherry-pick`, `git revert`, `git reset` to another commit, `git push`, `git tag`, `git branch`, `git switch`, `git checkout -b`, `git stash`, `git update-ref`.

Notes, scratch files and helper scripts go in `${scratch_dir}`, and nowhere else in the layer.

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
- Think hard about the smallest change that fully satisfies PROMPT. A chore changes no behaviour users rely on; if PROMPT needs a behaviour change, say so under What is left out and plan only the chore part.
- Write PLAN_FILE in the Plan Format: keep every heading, replace every `<placeholder>`, add no sections (unless a codebase-specific instruction below says otherwise). `<descriptive-name>` is 3-6 lowercase hyphenated words.
- Be surgical: list only files that must change, name the function or block in each, and leave unrelated code, formatting and dependencies alone.
- Tests: name every test to add or update, with its path. If the chore needs none, state why under What is left out.
- Keep the commands already present under `### Commands` in the Plan Format unchanged and in order; add chore-specific commands only after them. Checkable results are observable: each one maps to a command, a test or a named artefact.
- If the result is user-visible, name under Checkable results the artefacts that will prove it (screenshots, video, reports) and the step that produces them.
- End How it is proven with the Proof plan table, one row per checkable result. Proven by names one of the layer's declared checks below in backticks, by its name or by its command verbatim, optionally narrowed to test ids (pytest paths or node ids under its own test directory, `-k EXPR`, `-q`/`-x`/`-v`/`-rA`, `-n N`; for raco, test files of the same collection), then the test ids it runs, each in backticks; it never names another command. When a row names test ids, narrow the command to them and add `-rA` (pytest) so its log has a line per test; the declared command alone logs only counts. Writes lists the files it writes as globs relative to the worktree with `(count)`; files for this run only go under `${proof_dir}/` and are listed as `proof:GLOB` (relative to `${proof_dir}/`), and every one of them reaches the reviewers; `-` when it writes none. Needs lists the tokens, or `none`: `tool:NAME`, `engine:podman|docker`, `network`, `model:NAME`, `secret:NAME`, `host:ID`, `approval:KIND[:ARG]`. A decision only a person may take is `approval:edit:PATH`, `approval:dependency:NAME` or a layer word such as `approval:vocabulary`. Pre-approved for this run: ${approvals}.
- Declared checks (name, then the command verbatim):
- `format` — `cargo fmt --all --check`
- `test` — `cargo test --verbose --workspace --features pcre2`
- `e2e` — `ci/test-complete`
- `docs` — `RUSTDOCFLAGS="-D warnings" cargo doc --no-deps --document-private-items --workspace`
- Right after you, the runtime checks every row without an agent; a row it cannot prove here stops the run with the declaration or question, so write needs truthfully.
- Never plan a change under a protected path. If the chore cannot be done without one, plan everything else and add a line starting `REQUIRES HUMAN` under What is left out naming the path and the reason.
- A plan covers building and validating only: no step commits, merges, pushes, tags, releases or opens a pull request; delivery is the workflow's. Integration work (bringing in another branch or patch) is planned as 'apply onto the worktree, uncommitted', e.g. `git merge --no-commit --squash <ref>`, `git checkout <ref> -- <path>` or `git apply`.
- A new dependency is added with `(not specified — do not add dependencies without flagging it)` and listed under What could go wrong with its justification.
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

## What is asked and why
<what must be done and why, in enough detail that the prompt is no longer needed>

## What exists today
<bullet per existing file: path, then why it matters here and what changes in it>

## The change, step by step
IMPORTANT: execute every step in order, top to bottom.

### <step title>
- <one concrete action per bullet: file, symbol, change>

### Run the proof
- Run every command under How it is proven and fix until all pass.

## How it is proven

### Commands
Execute every command from the worktree root; all must exit 0.

- `cargo fmt --all --check` — every Rust file in the workspace is formatted as rustfmt.toml requires
- `cargo test --verbose --workspace --features pcre2` — unit tests, doc tests and the integration target pass for every workspace crate, each integration test once on the default regex engine and once on PCRE2; builds target/debug/rg
<additional chore-specific commands, each with what it proves; delete this line if none>

### Checkable results
<bullet per observable result, each tied to a command, a test or a named artefact>

### Proof plan
| # | Criterion | Proven by | Writes | Needs |
|---|---|---|---|---|
| C1 | <checkable result> | `<validation command>` `<test id>` | <globs (count), proof:GLOB, or -> | <none, or need tokens> |

## What could go wrong
<risks; new dependencies with their justification; anything the implementer must know; "Nothing known" if none>

## What is left out
<tests not added and why; REQUIRES HUMAN lines; follow-ups; "Nothing" if none>
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
