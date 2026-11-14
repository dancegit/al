---
description: meta-command for <NAME>
argument-hint: [problem-class-description]
---

# Meta-Command (template meta-prompt)

`new-command` in layer `<NAME>`: turn the description of a recurring class of problem into a NEW reusable command for this layer, so the problem is solved by a template from now on instead of by hand.

## Variables

PROBLEM_CLASS: $1
COMMANDS_DIR: ${layer_dir}/commands
NEW_COMMAND_FILE: ${layer_dir}/commands/<command-name>.md
LAYER_SOURCE: ${layer_dir}/agentic.rkt
DSL_REFERENCE: ${layer_dir}/AL-DSL.llms.txt

## Codebase

ripgrep is a command line tool (binary rg) that recursively searches the current directory for a regex pattern, respecting gitignore rules and skipping hidden and binary files by default. Rust edition 2024, minimum Rust 1.96, one Cargo workspace: the root package builds rg from crates/core/main.rs and nine library crates under crates/ hold the search, regex, printing, glob and ignore logic.

- Languages: rust, zsh
- Entry document: `README.md`

## Instructions

- You write exactly one file, NEW_COMMAND_FILE, in the layer. Do not edit application code and do not edit LAYER_SOURCE: you output the line to add, a human or another step adds it. Never switch branches, push, or touch the main checkout.
- You are writing a prompt for another agent, not solving one instance of the problem. Template the class: everything that varies between instances becomes an argument, everything that is always true of this codebase is written into the command.
- One agent, one prompt, one purpose. If PROBLEM_CLASS needs two purposes (for example plan and implement), write the first and name the second in your report.
- First list COMMANDS_DIR and read the two existing commands closest to PROBLEM_CLASS. If one already covers it, stop and report its name instead of writing a duplicate.
- `<command-name>` is 1-3 lowercase hyphenated words, unique among the commands and hops declared in LAYER_SOURCE.
- Follow the section structure of the existing commands exactly: YAML front matter with `description` and `argument-hint`; a title; a one-sentence purpose line; `Variables`; `Instructions`; `Relevant Files` and `Conditional Documentation` where they help; a `Plan Format` or `Workflow`; `Validation Commands`; and a final `Report` that states exactly what the last message must be.
- Spell positional arguments and run variables exactly as the existing commands do, and use only the ones that appear in them. Give each a name in `Variables`.
- Write the codebase's facts in concretely: real paths under Relevant Files, the real validation commands from the list below, the protected paths. The new file contains no unresolved template placeholders.
- Instructions are imperative, specific and checkable. Carry over the standing rules: work in the worktree, non-application files go to the layer, no branch switching, no pushing, no protected paths without `REQUIRES HUMAN`, never weaken a test.
- Close the loop: the command must say how its own result is validated.
- Keep it between 40 and 110 lines.
- Then write the `(command …)` line that registers it: template `custom`, `#:file` relative to the layer, a `#:description`, and `#:model-set`, `#:output` and `#:validation` as the command needs. Use only fields listed for `command` in DSL_REFERENCE and only cmd names declared in LAYER_SOURCE.


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

1. Read the closest existing commands and the `command` record in DSL_REFERENCE.
2. Research the codebase just enough to name the files and commands this problem class always involves.
3. Decide the arguments, the output type and the validation.
4. Write NEW_COMMAND_FILE.
5. Re-read it as the agent that will receive it, with no other context: fix every place where you would have to guess.
6. Write the registration line.

## Validation Commands

Codebase commands the new command may list as its validation:

- **format**: `cargo fmt --all --check` — every Rust file in the workspace is formatted as rustfmt.toml requires
- **build**: `cargo build --verbose --workspace` — the rg binary and every workspace crate compile; produces target/debug/rg
- **test**: `cargo test --verbose --workspace --features pcre2` — unit tests, doc tests and the integration target pass for every workspace crate, each integration test once on the default regex engine and once on PCRE2; builds target/debug/rg
- **e2e**: `ci/test-complete` — the options in crates/core/flags/complete/rg.zsh equal the options in rg --help; needs zsh and an existing target/release/rg or target/debug/rg, so it must run after build or test
- **docs**: `RUSTDOCFLAGS="-D warnings" cargo doc --no-deps --document-private-items --workspace` — rustdoc for every workspace crate, private items included, builds with no warnings

The new command is valid when it has every required section, uses only arguments and variables found in existing commands, and names only declared cmds.

## Report

- The path of NEW_COMMAND_FILE.
- The exact line to add to LAYER_SOURCE, in a code block, and where to add it (after the last existing `(command …)` clause).
- One example invocation with realistic arguments.

Finish with a concise report in plain markdown. No preamble.
