---
description: resolve-test for <NAME>
argument-hint: [failing-command] [output-tail]
---

# Resolve Failing Check

`resolve-test` in layer `<NAME>`: make one failing check pass by fixing its root cause in the application code, with the smallest possible change.

## Variables

FAILING_COMMAND: $1
OUTPUT_TAIL: $2
PLAN_FILE: ${plan}

## Instructions

- Work only inside `${worktree}` (your current directory, branch `${branch}`). Never switch branches, push, or touch the main checkout.
- Reproduce before you touch anything: run FAILING_COMMAND from the worktree root and read the full output. OUTPUT_TAIL is a hint, not the whole story.
- If it passes on reproduction, run it once more. If it still passes, change nothing and report it as not reproducible, with both outputs summarised.
- Think hard about the root cause: the earliest wrong line, not the line that raised. Read the failing test and the code it exercises before forming a fix.
- If PLAN_FILE is set, read it: the fix must keep what the plan asked for.
- Fix the cause with the minimal change. No refactoring, no unrelated cleanups, no dependency changes unless the failure is a missing or wrong dependency (then use `(not specified — do not add dependencies without flagging it)`).
- Tests are the source of truth. Never weaken an assertion, mark a test skipped or expected-to-fail, delete a test, loosen a lint or type rule, or edit a check's configuration to make it pass.
- If the test itself is wrong (it contradicts the plan, or asserts behaviour that was deliberately changed), do not edit it on your own judgement: say which test, why it is wrong, what the correct assertion would be, and stop.
- Do not modify anything under a protected path. If the fix lies there, stop and report it on a line starting `REQUIRES HUMAN`.
- Fix only this failure. Other failures you notice go in the report, not in the diff.
- Do not commit.
- When format fails, change only formatting: max_width = 79, use_small_heuristics = "max", edition = "2024".
- When e2e fails with 'rg not found', no rg binary exists in target/debug or target/release: the test or build command must run first.
- When e2e reports that zsh completion options differ from --help options, edit crates/core/flags/complete/rg.zsh to match the flags in crates/core/flags/defs.rs; never edit ci/test-complete.
- An integration test that fails only in its second run fails on PCRE2: rgtest! runs each test once per regex engine.

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

1. Reproduce: run FAILING_COMMAND.
2. Isolate: narrow to the single failing test or message; re-run just that if the tool allows it.
3. Diagnose: state the root cause as `path:line` and one sentence.
4. Fix: make the minimal change.
5. Verify: re-run FAILING_COMMAND, exactly as given. Loop to step 2 until it exits 0 or you hit a stop condition above.

## Validation Commands

FAILING_COMMAND itself is the validation. For reference, every command of the codebase:

- **format**: `cargo fmt --all --check` — every Rust file in the workspace is formatted as rustfmt.toml requires
- **build**: `cargo build --verbose --workspace` — the rg binary and every workspace crate compile; produces target/debug/rg
- **test**: `cargo test --verbose --workspace --features pcre2` — unit tests, doc tests and the integration target pass for every workspace crate, each integration test once on the default regex engine and once on PCRE2; builds target/debug/rg
- **e2e**: `ci/test-complete` — the options in crates/core/flags/complete/rg.zsh equal the options in rg --help; needs zsh and an existing target/release/rg or target/debug/rg, so it must run after build or test
- **docs**: `RUSTDOCFLAGS="-D warnings" cargo doc --no-deps --document-private-items --workspace` — rustdoc for every workspace crate, private items included, builds with no warnings

## Report

- Root cause: `path:line` and one sentence.
- Fix: what changed, one bullet per file.
- Result: the exit code of the final run of FAILING_COMMAND, or the stop condition you hit (not reproducible, test is wrong, `REQUIRES HUMAN`).
- The output of `git diff --stat`.

Finish with a concise report in plain markdown. No preamble.
