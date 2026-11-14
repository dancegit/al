---
description: document for <NAME>
argument-hint: [plan-path]
---

# Document

`document` in layer `<NAME>`: write a short, accurate record of what this run built, backed by the diff and the artefacts, and register it so future agents read it when it matters.

## Variables

PLAN_FILE: $1
ADW_ID: ${adw_id}
WORK_CLASS: ${class}
DOC_FILE: ${layer_dir}/docs/${class}-${adw_id}-<descriptive-name>.md
CONDITIONAL_DOCS: ${layer_dir}/conditional_docs.md
ARTEFACTS_DIR: ${artefacts_dir}

## Instructions

- Your current directory is `${worktree}`. Read it; write nothing to it. Both files you write are in the layer.
- Never switch branches, push, or touch the main checkout.
- Read PLAN_FILE for what was asked. Read `git diff master...HEAD --stat` and then the diff itself for what was built. Document what was built; where it differs from the plan, say so.
- Every statement must be traceable to the diff or to an artefact. No aspirations, no marketing, no restating of the plan.
- List ARTEFACTS_DIR and link each artefact that proves the change by its path, with one line saying what it shows. Open an image before describing it.
- How to Use is written for the next person: the exact command, call, setting or screen, with one minimal example.
- Keep it under 80 lines. `<descriptive-name>` is 3-6 lowercase hyphenated words; reuse the plan's name when there is one.
- Append exactly one line to CONDITIONAL_DOCS (create the file if absent), never rewriting existing lines:
  `- docs/<doc file name> — when <the concrete condition under which an agent must read this>`
- The condition names code or behaviour ("when changing anything under <path>", "when working on <feature>"), never "when relevant".
- Flag documentation lives in the Flag implementation in crates/core/flags/defs.rs; --help and the man page are generated from it.
- Update GUIDE.md or FAQ.md only when the change alters behaviour they describe.
- A user-visible change needs its entry under the TBD heading at the top of CHANGELOG.md.

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

## Conditional Documentation

Already registered for the codebase; do not duplicate a condition that exists:

- `GUIDE.md` — When changing behaviour it documents: automatic filtering, globs, file types, replacements, the configuration file, file encoding, binary data, preprocessors
- `FAQ.md` — When changing behaviour that an FAQ answer describes
- `CHANGELOG.md` — When making a user-visible change, to add the entry under TBD
- `crates/core/README.md` — When changing files under crates/core
- `crates/ignore/benches/README.md` — When changing directory walking or ignore matching in crates/ignore/src
- `fuzz/README.md` — When changing glob parsing in crates/globset/src or anything under fuzz
- `pkg/windows/README.md` — When changing build.rs or pkg/windows/Manifest.xml
- `RELEASE-CHECKLIST.md` — When asked to change a version number, Cargo.lock or anything related to a release
- `AI_POLICY.md` — When work is intended for the upstream repository BurntSushi/ripgrep



## Document Format

```md
# <title of the change>

**ADW ID:** <adw id>  **Class:** <work class>  **Plan:** <plan path>

## Overview
<2-3 sentences: what was built and why>

## What Changed
<bullets: behaviour first, then the notable implementation points; deviations from the plan>

## How to Use
<numbered steps or a command, with one minimal example>

## Proof / Artefacts
<bullet per artefact: path, then what it shows; validation commands and their results>

## Files Modified
<bullet per file: path, then one line on the change>
```

## Validation Commands

Do not run these. Report their results from the logs in ARTEFACTS_DIR:

- `cargo build --verbose --workspace` — the rg binary and every workspace crate compile; produces target/debug/rg
- `cargo test --verbose --workspace --features pcre2` — unit tests, doc tests and the integration target pass for every workspace crate, each integration test once on the default regex engine and once on PCRE2; builds target/debug/rg

## Report

State the path of DOC_FILE and the line appended to CONDITIONAL_DOCS.

Finish with a concise report in plain markdown. No preamble.
