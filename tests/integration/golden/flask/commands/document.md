---
description: document for <NAME>
argument-hint: [plan-path]
---
## Your step

${workflow_steps}

Leave your work as uncommitted changes in the worktree. Do not commit, merge into any branch, push, tag, open a pull request, or change any ref. The runtime checks this when you finish: a step that moved HEAD or a ref, changed the main checkout, or left a file at the top of the layer directory fails, and what it did is undone.

Git in this step is for reading (`git status`, `git diff`, `git log`, `git show`) and, when the plan asks, for bringing content into the working tree (`git merge --no-commit --squash <ref>`, `git checkout <ref> -- <path>`, `git apply`), always ending uncommitted. Never run in this step: `git commit`, `git merge` without `--no-commit --squash`, `git rebase`, `git cherry-pick`, `git revert`, `git reset` to another commit, `git push`, `git tag`, `git branch`, `git switch`, `git checkout -b`, `git stash`, `git update-ref`.

Notes, scratch files and helper scripts go in `${scratch_dir}`, and nowhere else in the layer.

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
- Read PLAN_FILE for what was asked. Read `git diff main...HEAD --stat` and then the diff itself for what was built. Document what was built; where it differs from the plan, say so.
- Every statement must be traceable to the diff or to an artefact. No aspirations, no marketing, no restating of the plan.
- List ARTEFACTS_DIR and name each artefact that proves the change by its path relative to ARTEFACTS_DIR (never an absolute path: the document is published), with one line saying what it shows. Open an image before describing it.
- How to Use is written for the next person: the exact command, call, setting or screen, with one minimal example.
- Keep it under 80 lines. `<descriptive-name>` is 3-6 lowercase hyphenated words; reuse the plan's name when there is one.
- The **Read when** line names the concrete condition under which a future agent must read this document: code or behaviour ("when changing anything under <path>", "when working on <feature>"), never "when relevant". A person may register it as a `(conditional-doc …)` of the layer.

## Relevant Files

- `README.md`
- `src/flask`
- `tests`

## Conditional Documentation

Already registered for the codebase; read those whose condition holds, and do not repeat one as a Read when line:

- `src/flask/sansio/README.md` — When changing any file under src/flask/sansio
- `docs/web-security.rst` — When changing session, cookie, JSON or response header handling
- `docs/extensiondev.rst` — When changing an API that extensions hook into (Flask.extensions, init_app patterns, signals, teardown callbacks)
- `docs/async-await.rst` — When changing async view support or ensure_sync
- `docs/cli.rst` — When changing src/flask/cli.py
- `docs/testing.rst` — When changing src/flask/testing.py
- `docs/contributing.rst` — When preparing a pull request



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

- Documentation is reStructuredText under docs; a change to documented behaviour also adds a '.. versionchanged:: <version>' or '.. versionadded:: <version>' entry in the docstring of the changed code.

## Validation Commands

Do not run these. Report their results from the logs in ARTEFACTS_DIR:

- `uv run --locked --no-default-groups --group pre-commit pre-commit run --show-diff-on-failure --color=always --all-files` — Every pre-commit hook passes on all files: ruff lint rules B, E, F, I, UP, W; ruff format; uv.lock matches pyproject.toml; codespell; whitespace and end-of-file hooks. Hooks rewrite files in place and exit non-zero when they changed something.
- `uv run --locked --no-default-groups --group dev tox run -e typing` — mypy in strict mode reports no errors for src and tests/type_check.
- `TOX_ENV=py3.12 uv run --locked --no-default-groups --group dev tox run -- --junitxml=.tox/reports/junit.xml` — The pytest suite in tests passes on Python 3.12 against the locked dependency versions, with every warning treated as an error; writes a JUnit XML report.
- `uv build` — The sdist and wheel build into dist/.

## Report

State the path of DOC_FILE and its Read when line.

Finish with a concise report in plain markdown. No preamble.
