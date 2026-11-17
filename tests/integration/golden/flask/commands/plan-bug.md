---
description: plan-bug for <NAME>
argument-hint: [prompt]
---
## Your step

${workflow_steps}

Leave your work as uncommitted changes in the worktree. Do not commit, merge into any branch, push, tag, open a pull request, or change any ref. The runtime checks this when you finish: a step that moved HEAD or a ref, changed the main checkout, or left a file at the top of the layer directory fails, and what it did is undone.

Git in this step is for reading (`git status`, `git diff`, `git log`, `git show`) and, when the plan asks, for bringing content into the working tree (`git merge --no-commit --squash <ref>`, `git checkout <ref> -- <path>`, `git apply`), always ending uncommitted. Never run in this step: `git commit`, `git merge` without `--no-commit --squash`, `git rebase`, `git cherry-pick`, `git revert`, `git reset` to another commit, `git push`, `git tag`, `git branch`, `git switch`, `git checkout -b`, `git stash`, `git update-ref`.

Notes, scratch files and helper scripts go in `${scratch_dir}`, and nowhere else in the layer.

# Plan Bug

`plan-bug` in layer `<NAME>`: turn a bug report into a plan that fixes the root cause with the smallest possible change. Plan only; implement nothing.

## Variables

PROMPT: $1
PLAN_FILE: ${specs_dir}/bug-${adw_id}-<descriptive-name>.md

## Codebase

Flask is a Python WSGI web application framework distributed as the Flask package (requires Python >=3.11). It is used as a library and through its flask command line entry point, is built on Werkzeug, Jinja, Click, ItsDangerous, MarkupSafe and Blinker, is managed with uv and tox, and ships to PyPI.

Languages: python. Entry document: `README.md`. Default branch: `main`.

## Instructions

- You are in `${worktree}`, a git worktree of the application. Read it and run read-only commands to reproduce; do not modify it. The only file you create is PLAN_FILE, which lives in the layer and never in the application tree. Never switch branches, push, or touch the main checkout.
- Research before writing: read `README.md`, then the Relevant Files, then trace the failing path through the code.
- Reproduce first. Record the exact steps and the observed versus expected result. If you cannot reproduce, say so under How to reproduce and state what evidence the analysis rests on.
- Think hard about the cause: the earliest point where state or logic goes wrong, cited as `path:line` under Cause. A symptom is not a cause.
- Plan the minimal fix at the cause. No refactoring, no drive-by cleanups, no new abstractions.
- Tests: plan a regression test that fails before the fix and passes after it; name its path under Tests and give it a step. List any existing test that must change and why.
- Write PLAN_FILE in the Plan Format: keep every heading, replace every `<placeholder>`, add no sections (unless a codebase-specific instruction below says otherwise). `<descriptive-name>` is 3-6 lowercase hyphenated words.
- Keep the commands already present under `### Commands` in the Plan Format unchanged and in order; add the reproduction command and bug-specific commands after them. Checkable results are observable: each one maps to a test, a command or a named artefact.
- If the bug is user-visible, name under Checkable results the artefacts that will prove the fix (screenshots, video, reports) and the step that produces them.
- Never plan a change under a protected path. If the fix cannot avoid one, plan everything else and add a line starting `REQUIRES HUMAN` under What is left out naming the path and the reason.
- A plan covers building and validating only: no step commits, merges, pushes, tags, releases or opens a pull request; delivery is the workflow's. Integration work (bringing in another branch or patch) is planned as 'apply onto the worktree, uncommitted', e.g. `git merge --no-commit --squash <ref>`, `git checkout <ref> -- <path>` or `git apply`.
- A new dependency is a last resort; add it with `(not specified — do not add dependencies without flagging it)` and justify it under What could go wrong.
- Read a conditional document when its condition matches what you are planning.

## Relevant Files

- `README.md`
- `src/flask`
- `tests`

Protected paths (do not plan changes here):

- `.github`
- `.pre-commit-config.yaml`
- `.readthedocs.yaml`
- `uv.lock`
- `LICENSE.txt`
- `src/flask/sessions.py`

## Conditional Documentation

- `src/flask/sansio/README.md` — When changing any file under src/flask/sansio
- `docs/web-security.rst` — When changing session, cookie, JSON or response header handling
- `docs/extensiondev.rst` — When changing an API that extensions hook into (Flask.extensions, init_app patterns, signals, teardown callbacks)
- `docs/async-await.rst` — When changing async view support or ensure_sync
- `docs/cli.rst` — When changing src/flask/cli.py
- `docs/testing.rst` — When changing src/flask/testing.py
- `docs/contributing.rst` — When preparing a pull request



## Plan Format

Codebase-specific instructions below take precedence over the defaults in this section.

```md
# Bug: <bug name>

## What is asked and why
<one paragraph: the symptoms, the expected and the actual behaviour, and the fix, with why it addresses the cause rather than the symptom>

## What exists today
<bullet per existing file: path, then why it matters here and what changes in it; then a bullet per file to create with its purpose, if any>

### How to reproduce
<numbered steps or one command; observed result; expected result>

### Cause
<the defect, cited as path:line, and the chain from it to the symptom>

## The change, step by step
IMPORTANT: execute every step in order, top to bottom.

### <step title>
- <one concrete action per bullet: file, symbol, change>

### Add regression test
- <test path, what it asserts, why it fails before the fix>

### Run the proof
- Run every command under How it is proven and fix until all pass.

## How it is proven

### Tests
<test path, then what it asserts; existing tests that change and why>

### Commands
Execute every command from the worktree root; all must exit 0.

- `uv run --locked --no-default-groups --group pre-commit pre-commit run --show-diff-on-failure --color=always --all-files` — Every pre-commit hook passes on all files: ruff lint rules B, E, F, I, UP, W; ruff format; uv.lock matches pyproject.toml; codespell; whitespace and end-of-file hooks. Hooks rewrite files in place and exit non-zero when they changed something.
- `uv run --locked --no-default-groups --group dev tox run -e typing` — mypy in strict mode reports no errors for src and tests/type_check.
- `TOX_ENV=py3.12 uv run --locked --no-default-groups --group dev tox run -- --junitxml=.tox/reports/junit.xml` — The pytest suite in tests passes on Python 3.12 against the locked dependency versions, with every warning treated as an error; writes a JUnit XML report.
<the reproduction command, now passing, and any bug-specific commands with what each proves>

### Checkable results
<bullet per observable result, each tied to a test, a command or a named artefact>

## What could go wrong
<risks; new dependencies with their justification; anything the implementer must know; "Nothing known" if none>

## What is left out
<REQUIRES HUMAN lines; related defects not fixed here; follow-ups; "Nothing" if none>
```

## Codebase-specific instructions

The layer's own instructions for this command (`#:focus` in `agentic.rkt`), if any. Where one says otherwise than a default above, it wins. They never change where the plan is written, the `REQUIRES HUMAN` rule or the Report section: the runtime reads those, so they are fixed.

- The regression test goes into the existing tests/test_<area>.py module of the source file being fixed (src/flask/blueprints.py -> tests/test_blueprints.py, src/flask/json/tag.py -> tests/test_json_tag.py); a regression with no matching module goes into tests/test_regression.py.
- The regression test must fail without the fix.
- Add an entry to CHANGES.rst under the topmost (unreleased) version, as a '-   ' bullet ending with an :issue:`<number>` or :pr:`<number>` reference.
- Code under src/flask/sansio does no IO and does not use the Flask globals.

## Validation Commands

Do not run these to validate anything now (running one to reproduce the bug is fine). They are the commands the plan embeds.

- `uv run --locked --no-default-groups --group pre-commit pre-commit run --show-diff-on-failure --color=always --all-files` — Every pre-commit hook passes on all files: ruff lint rules B, E, F, I, UP, W; ruff format; uv.lock matches pyproject.toml; codespell; whitespace and end-of-file hooks. Hooks rewrite files in place and exit non-zero when they changed something.
- `uv run --locked --no-default-groups --group dev tox run -e typing` — mypy in strict mode reports no errors for src and tests/type_check.
- `TOX_ENV=py3.12 uv run --locked --no-default-groups --group dev tox run -- --junitxml=.tox/reports/junit.xml` — The pytest suite in tests passes on Python 3.12 against the locked dependency versions, with every warning treated as an error; writes a JUnit XML report.

## Report

Your final message must be ONLY the path of the file you created, on a single line, with no other text.
