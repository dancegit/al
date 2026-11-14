---
description: e2e-test for <NAME>
argument-hint: [scenario-path]
---

# E2E Test

`e2e-test` in layer `<NAME>`: execute one end-to-end scenario against the running application as a user would, capture proof of every verification, and return a machine-readable verdict.

## Variables

SCENARIO_FILE: $1
ADW_ID: ${adw_id}
LAYER_CONFIG: ${layer_dir}/agentic.rkt

## Instructions

- Work only inside `${worktree}` (your current directory, branch `${branch}`). Never switch branches, push, or touch the main checkout.
- You test; you do not fix. Do not edit application code, tests or configuration.
- Read SCENARIO_FILE in full. It has a User Story, numbered Steps (some marked `verify`) and Success Criteria. Execute the steps in order, exactly as written.
- Drive the application with whatever the codebase already provides: its own end-to-end command, a browser or UI driver, a CLI, an HTTP client. Take the driver from the commands below and the codebase's test setup. Do not install a new driver.
- If the scenario needs the application running, start it with the codebase's start command on the ports in the environment variables `ADW_PORT_0`, `ADW_PORT_1`, and stop it when you are done.
- Capture proof at every `verify` step: a screenshot for anything visual, a recording when the scenario covers a flow, the raw output for anything textual.
- Save proof where the workflow collects it: read the `(artefact …)` declarations in LAYER_CONFIG and write each file to a path inside the worktree that matches the glob of its type. Name files `<two-digit step>-<what-it-shows>` so they sort in step order. Never commit these files.
- Read every image back after saving it and confirm it shows what the step claims. A blank, wrong-page or error screenshot is a failed step, not proof.
- A `verify` step passes only when you observed the expected result. Stop at the first failed step: record it, capture its proof, and do not run the steps after it.
- Do not reinterpret a failing step into a passing one, and do not retry with different inputs.
- The end-to-end check of this codebase is ci/test-complete: it compares the options in rg --help with those in crates/core/flags/complete/rg.zsh and prints OK when they are equal.
- It needs zsh and an existing target/release/rg or target/debug/rg.

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

1. Read SCENARIO_FILE and the artefact globs in LAYER_CONFIG.
2. Prepare: install or start only what the scenario needs, using the codebase commands.
3. Execute each step; capture and save proof at each `verify`.
4. Read every saved image back and check it against its step.
5. Check each Success Criterion against what you observed.
6. Stop anything you started, then emit the result.

## Validation Commands

The scenario is the validation. Codebase commands available to prepare and drive it:

- **format**: `cargo fmt --all --check` — every Rust file in the workspace is formatted as rustfmt.toml requires
- **build**: `cargo build --verbose --workspace` — the rg binary and every workspace crate compile; produces target/debug/rg
- **test**: `cargo test --verbose --workspace --features pcre2` — unit tests, doc tests and the integration target pass for every workspace crate, each integration test once on the default regex engine and once on PCRE2; builds target/debug/rg
- **e2e**: `ci/test-complete` — the options in crates/core/flags/complete/rg.zsh equal the options in rg --help; needs zsh and an existing target/release/rg or target/debug/rg, so it must run after build or test
- **docs**: `RUSTDOCFLAGS="-D warnings" cargo doc --no-deps --document-private-items --workspace` — rustdoc for every workspace crate, private items included, builds with no warnings

## Report

Return ONLY a JSON object, no prose and no code fence:

```json
{
  "scenario": "<scenario name>",
  "passed": true,
  "steps": [
    {"step": "<step number and text>", "passed": true, "evidence": "<artefact path or observed output>"}
  ],
  "artefacts": ["<worktree-relative path of every file saved>"],
  "error": "<empty when passed; otherwise the failed step and what was observed instead>"
}
```

`passed` is true only when every executed step passed and every Success Criterion holds.

Finish with a concise report in plain markdown. No preamble.
