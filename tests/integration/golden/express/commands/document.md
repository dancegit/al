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


## Relevant Files

- `Readme.md`
- `lib`
- `index.js`
- `test`

## Conditional Documentation

Already registered for the codebase; do not duplicate a condition that exists:

- `examples/README.md` — When adding, renaming or removing a directory under examples/
- `History.md` — When a change alters behaviour that an earlier release entry describes



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

- `npm run lint` — eslint reports no violation of .eslintrc.yml in any JavaScript file of the repository
- `npm run test-ci` — the whole mocha suite (test/ and test/acceptance/) passes with global-leak checking, under nyc, writing coverage/lcov.info and a text coverage table to stdout

## Report

State the path of DOC_FILE and the line appended to CONDITIONAL_DOCS.

Finish with a concise report in plain markdown. No preamble.
