---
description: prime for <NAME>
argument-hint: (none)
---
## Your step

${workflow_steps}

Leave your work as uncommitted changes in the worktree. Do not commit, merge into any branch, push, tag, open a pull request, or change any ref. The runtime checks this when you finish: a step that moved HEAD or a ref, changed the main checkout, or left a file at the top of the layer directory fails, and what it did is undone.

Git in this step is for reading (`git status`, `git diff`, `git log`, `git show`) and, when the plan asks, for bringing content into the working tree (`git merge --no-commit --squash <ref>`, `git checkout <ref> -- <path>`, `git apply`), always ending uncommitted. Never run in this step: `git commit`, `git merge` without `--no-commit --squash`, `git rebase`, `git cherry-pick`, `git revert`, `git reset` to another commit, `git push`, `git tag`, `git branch`, `git switch`, `git checkout -b`, `git stash`, `git update-ref`.

Notes, scratch files and helper scripts go in `${scratch_dir}`, and nowhere else in the layer.

# Prime

`prime` in layer `<NAME>`: build an accurate working picture of the codebase before doing anything else. Read-only.

## Variables

WORKTREE: ${worktree}
LAYER_DIR: ${layer_dir}

## Codebase

Flask is a Python WSGI web application framework distributed as the Flask package (requires Python >=3.11). It is used as a library and through its flask command line entry point, is built on Werkzeug, Jinja, Click, ItsDangerous, MarkupSafe and Blinker, is managed with uv and tox, and ships to PyPI.

- Languages: python
- Entry document: `README.md`
- Dependencies are added with: `(not specified — do not add dependencies without flagging it)`
- Default branch: `main`

## Instructions

- Read only. Do not edit, create, install, build or run anything that changes state. Never switch branches, push, or touch the main checkout.
- Your current directory is WORKTREE, a git worktree of the application. The agentic layer lives outside it, in LAYER_DIR; anything that is not application code belongs there.
- Learn the structure from the repository itself, not from assumptions about the language or framework.
- Read enough of each relevant path to know what lives there and how the parts call each other. Skim; do not read every file.
- Learn the commands below by name. They are the only supported way to install, check, build and run this codebase.
- Note the protected paths. They are off limits without a human.
- Do not read conditional documents now. Remember each condition so you read the document when it applies.


## Relevant Files

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

1. Run `git ls-files` and read the shape of the tree.
2. Read `README.md`.
3. Read the entry points and the main modules under the Relevant Files.
4. Read one representative test to learn how tests are written and named here.
5. Run `git log --oneline -15` to see how recent work is described.

## Validation Commands

Do not run these now. These are every command of the codebase, with what each proves:

- **lint**: `uv run --locked --no-default-groups --group pre-commit pre-commit run --show-diff-on-failure --color=always --all-files` — Every pre-commit hook passes on all files: ruff lint rules B, E, F, I, UP, W; ruff format; uv.lock matches pyproject.toml; codespell; whitespace and end-of-file hooks. Hooks rewrite files in place and exit non-zero when they changed something.
- **typecheck**: `uv run --locked --no-default-groups --group dev tox run -e typing` — mypy in strict mode reports no errors for src and tests/type_check.
- **test**: `TOX_ENV=py3.12 uv run --locked --no-default-groups --group dev tox run -- --junitxml=.tox/reports/junit.xml` — The pytest suite in tests passes on Python 3.12 against the locked dependency versions, with every warning treated as an error; writes a JUnit XML report.
- **build**: `uv build` — The sdist and wheel build into dist/.
- **docs**: `uv run --locked --no-default-groups --group dev tox run -e docs` — The Sphinx documentation builds to docs/_build/dirhtml with warnings treated as errors.

## Report

Summarise your understanding in at most 15 bullets: what the codebase does, how it is laid out, where the entry points and tests are, which command proves what, and which paths are protected. State anything you expected to find and did not.

Finish with a concise report in plain markdown. No preamble.
