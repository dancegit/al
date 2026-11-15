---
description: document for <NAME>
argument-hint: [plan-path]
---

# Document

`document` in layer `<NAME>`: write a short, accurate record of what this run built, backed by the diff and the artefacts, for the person who reviews the change.

## Variables

PLAN_FILE: $1
ADW_ID: ${adw_id}
WORK_CLASS: ${class}
DOC_FILE: ${layer_dir}/docs/${class}-${adw_id}-<descriptive-name>.md
ARTEFACTS_DIR: ${artefacts_dir}

## Instructions

- Your current directory is `${worktree}`. Read it; write nothing to it. DOC_FILE is the only file you write.
- DOC_FILE is the run's record, not part of the change: the runtime puts it in the pull request (or `PR.md`) for the reviewer. Do not edit `conditional_docs.md` or any other file of the layer: what agents read is changed through `al improve`.
- Never switch branches, push, or touch the main checkout.
- Read PLAN_FILE for what was asked. Read `git diff master...HEAD --stat` and then the diff itself for what was built. Document what was built; where it differs from the plan, say so.
- Every statement must be traceable to the diff or to an artefact. No aspirations, no marketing, no restating of the plan.
- List ARTEFACTS_DIR and name each artefact that proves the change by its path relative to ARTEFACTS_DIR (never an absolute path: the document is published), with one line saying what it shows. Open an image before describing it.
- How to Use is written for the next person: the exact command, call, setting or screen, with one minimal example.
- Keep it under 80 lines. `<descriptive-name>` is 3-6 lowercase hyphenated words; reuse the plan's name when there is one.
- The **Read when** line names the concrete condition under which a future agent must read this document: code or behaviour ("when changing anything under <path>", "when working on <feature>"), never "when relevant". A person may register it as a `(conditional-doc …)` of the layer.

## Relevant Files

- `README.md`
- `src`
- `Makefile.am`
- `configure.ac`
- `config/m4`
- `m4`
- `scripts`
- `tests`

## Conditional Documentation

Already registered for the codebase; read those whose condition holds, and do not repeat one as a Read when line:

- `docs/README.md` — When changing anything under docs/, or regenerating jq.1.prebuilt, tests/man.test or tests/manonig.test
- `docs/content/manual/dev/manual.yml` — When adding or changing a builtin, operator or command-line option
- `docs/manual_schema.yml` — When editing any manual.yml
- `tests/setup` — When adding or changing a test driver script under tests/
- `.github/workflows/ci.yml` — When changing configure options or cross-compilation behaviour
- `NEWS.md` — When a change is user-visible and belongs in release notes
- `SECURITY.md` — When the task concerns a security vulnerability



## Document Format

Codebase-specific instructions below take precedence over the defaults in this section.

```md
# <title of the change>

**ADW ID:** <adw id>  **Class:** <work class>  **Plan:** <plan file name>

**Read when:** <the condition>

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

## Codebase-specific instructions

The layer's own instructions for this command (`#:focus` in `agentic.rkt`), if any. Where one says otherwise than a default above, it wins. They never change where the document is written, that it is the only file you write, or the Report section: the runtime reads those, so they are fixed.

- User-visible builtins and options are documented in docs/content/manual/dev/manual.yml, never in docs/content/manual/v*/manual.yml.

## Validation Commands

Do not run these. Report their results from the logs in ARTEFACTS_DIR:

- `make -j"$(nproc)"` — libjq and the jq binary compile and link with -Wextra -Wall
- `make check VERBOSE=yes` — Every driver in TESTS of Makefile.am passes against the built ./jq; writes test-suite.log and tests/*.log

## Report

State the path of DOC_FILE and its Read when line.

Finish with a concise report in plain markdown. No preamble.
