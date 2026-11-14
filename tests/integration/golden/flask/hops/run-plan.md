---
description: run-plan for <NAME>
argument-hint: [plan-or-prompt-path]
---

# Run Plan (higher-order prompt)

`run-plan` in layer `<NAME>`: take ANY plan, spec or prompt file as the argument, execute it completely, validate the result, and report. The file is the instruction; this prompt is the discipline around it.

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
- `src/flask`
- `tests`

Protected paths:

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



## Workflow

1. Read PLAN_FILE and every file it names.
2. Execute its steps in order.
3. Validate: run every validation command the file lists, then the Validation Commands below, from the worktree root.
4. On any failure: read the output, find the cause, fix the code, re-run from the first command. Repeat until every command exits 0.
5. Re-read PLAN_FILE and confirm each step and each acceptance criterion is satisfied by the diff.

## Validation Commands

All must exit 0 before you report.

- `uv run --locked --no-default-groups --group pre-commit pre-commit run --show-diff-on-failure --color=always --all-files` — Every pre-commit hook passes on all files: ruff lint rules B, E, F, I, UP, W; ruff format; uv.lock matches pyproject.toml; codespell; whitespace and end-of-file hooks. Hooks rewrite files in place and exit non-zero when they changed something.
- `uv run --locked --no-default-groups --group dev tox run -e typing` — mypy in strict mode reports no errors for src and tests/type_check.
- `TOX_ENV=py3.12 uv run --locked --no-default-groups --group dev tox run -- --junitxml=.tox/reports/junit.xml` — The pytest suite in tests passes on Python 3.12 against the locked dependency versions, with every warning treated as an error; writes a JUnit XML report.
- `uv build` — The sdist and wheel build into dist/.

## Report

- A bullet summary of the work, one bullet per step of the file, with deviations and `REQUIRES HUMAN` lines called out.
- The pass/fail result of each validation command.
- The output of `git diff --stat` (plus `git status --short` if you created files).

Finish with a concise report in plain markdown. No preamble.
