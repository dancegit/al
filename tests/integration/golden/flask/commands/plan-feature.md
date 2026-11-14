---
description: plan-feature for <NAME>
argument-hint: [prompt]
---

# Plan Feature

`plan-feature` in layer `<NAME>`: turn a feature request into a phased plan that another agent can implement end to end without asking a single question. Plan only; implement nothing.

## Variables

PROMPT: $1
PLAN_FILE: ${specs_dir}/feature-${adw_id}-<descriptive-name>.md

## Codebase

Flask is a Python WSGI web application framework distributed as the Flask package (requires Python >=3.11). It is used as a library and through its flask command line entry point, is built on Werkzeug, Jinja, Click, ItsDangerous, MarkupSafe and Blinker, is managed with uv and tox, and ships to PyPI.

Languages: python. Entry document: `README.md`. Default branch: `main`.

## Instructions

- You are in `${worktree}`, a git worktree of the application. Read it, do not modify it. The only file you create is PLAN_FILE, which lives in the layer and never in the application tree. Never switch branches, push, or touch the main checkout.
- Research before writing: read `README.md`, then the Relevant Files, then the closest existing feature. Follow its patterns, naming and file layout; do not invent a new architecture. Think hard about the smallest design that delivers the user value in PROMPT. Leave out everything PROMPT does not ask for.
- Write PLAN_FILE in exactly the Plan Format: keep every heading, replace every `<placeholder>`, add no sections. `<descriptive-name>` is 3-6 lowercase hyphenated words.
- Be surgical: list only files that must change or be created, and name the function, type or block in each.
- Tests: name every test to add, with its path, under Testing Strategy, and give each a step in Step by Step Tasks. Tests come before or with the code they cover, never after validation. Acceptance Criteria are observable and checkable: each one maps to a test, a validation command or a named artefact.
- Keep the commands already present under `## Validation Commands` in the Plan Format unchanged and in order; add feature-specific commands after them.
- If the feature is user-visible, name in Notes the artefacts that will prove it (screenshots, video, reports), where they land, and the step that produces them.
- Never plan a change under a protected path. If the feature cannot avoid one, plan everything else and add a line starting `REQUIRES HUMAN` to Notes naming the path and the reason.
- A new dependency is added with `(not specified — do not add dependencies without flagging it)` and justified in Notes. Read a conditional document when its condition matches what you are planning.
- New tests go into the existing tests/test_<area>.py module of the source file being changed, as functions named test_<behaviour>; shared fixtures live in tests/conftest.py.
- A feature adds a CHANGES.rst entry under the topmost (unreleased) version, '.. versionadded:: <version>' or '.. versionchanged:: <version>' entries in the docstrings of the changed code, and documentation under docs.
- Code under src passes mypy with strict = true; every warning raised during the tests fails the suite.
- Code under src/flask/sansio does no IO and does not use the Flask globals.

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

```md
# Feature: <feature name>

## Feature Description
<what the feature does and the value it delivers>

## User Story
As a <type of user>, I want to <action or goal>, so that <benefit>.

## Problem Statement
<the problem or opportunity this feature addresses>

## Solution Statement
<the approach, and how it fits the existing design>

## Relevant Files
<bullet per existing file: path, why it is relevant, what changes in it; then, under a `### New Files` heading, a bullet per file to create with its purpose>

## Implementation Plan
### Phase 1: Foundation
<shared groundwork needed before the feature itself: types, schema, config, scaffolding>
### Phase 2: Core Implementation
<the feature's main behaviour>
### Phase 3: Integration
<wiring into existing entry points, interfaces and documentation>

## Step by Step Tasks
IMPORTANT: execute every step in order, top to bottom. The last step runs every command under Validation Commands and fixes until all pass.
### <step title>
- <one concrete action per bullet: file, symbol, change>

## Testing Strategy
### Unit Tests
<test path, then what it asserts>
### Integration Tests
<test path, then the flow it covers>
### Edge Cases
<each edge case and the test that covers it>

## Acceptance Criteria
<checkable statements, each tied to a test, command or artefact>

## Validation Commands
Execute every command from the worktree root; all must exit 0.

- `uv run --locked --no-default-groups --group pre-commit pre-commit run --show-diff-on-failure --color=always --all-files` — Every pre-commit hook passes on all files: ruff lint rules B, E, F, I, UP, W; ruff format; uv.lock matches pyproject.toml; codespell; whitespace and end-of-file hooks. Hooks rewrite files in place and exit non-zero when they changed something.
- `uv run --locked --no-default-groups --group dev tox run -e typing` — mypy in strict mode reports no errors for src and tests/type_check.
- `TOX_ENV=py3.12 uv run --locked --no-default-groups --group dev tox run -- --junitxml=.tox/reports/junit.xml` — The pytest suite in tests passes on Python 3.12 against the locked dependency versions, with every warning treated as an error; writes a JUnit XML report.
<additional feature-specific commands, each with what it proves; delete this line if none>

## Notes
<new dependencies; artefacts that prove the user-visible result; REQUIRES HUMAN lines; deliberate omissions and follow-ups>
```

## Validation Commands

Do not run these now. They are the commands the plan embeds; the implementer runs them.

- `uv run --locked --no-default-groups --group pre-commit pre-commit run --show-diff-on-failure --color=always --all-files` — Every pre-commit hook passes on all files: ruff lint rules B, E, F, I, UP, W; ruff format; uv.lock matches pyproject.toml; codespell; whitespace and end-of-file hooks. Hooks rewrite files in place and exit non-zero when they changed something.
- `uv run --locked --no-default-groups --group dev tox run -e typing` — mypy in strict mode reports no errors for src and tests/type_check.
- `TOX_ENV=py3.12 uv run --locked --no-default-groups --group dev tox run -- --junitxml=.tox/reports/junit.xml` — The pytest suite in tests passes on Python 3.12 against the locked dependency versions, with every warning treated as an error; writes a JUnit XML report.

## Report

Your final message must be ONLY the path of the file you created, on a single line, with no other text.
