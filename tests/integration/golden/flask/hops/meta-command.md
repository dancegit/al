---
description: meta-command for <NAME>
argument-hint: [problem-class-description]
---

# Meta-Command (template meta-prompt)

`meta-command` in layer `<NAME>`: turn the description of a recurring class of problem into a NEW reusable command for this layer, so the problem is solved by a template from now on instead of by hand.

## Variables

PROBLEM_CLASS: $1
COMMANDS_DIR: ${layer_dir}/commands
NEW_COMMAND_FILE: ${layer_dir}/templates/custom/<command-name>.md
LAYER_SOURCE: ${layer_dir}/agentic.rkt
DSL_REFERENCE: ${layer_dir}/AL-DSL.llms.txt

## Codebase

Flask is a Python WSGI web application framework distributed as the Flask package (requires Python >=3.11). It is used as a library and through its flask command line entry point, is built on Werkzeug, Jinja, Click, ItsDangerous, MarkupSafe and Blinker, is managed with uv and tox, and ships to PyPI.

- Languages: python
- Entry document: `README.md`

## Instructions

- You write exactly one file, NEW_COMMAND_FILE, in the layer's `templates/custom/`. Do not edit application code and do not edit LAYER_SOURCE: you output the line to add, a human or another step adds it. Never switch branches, push, or touch the main checkout.
- COMMANDS_DIR holds generated prompts: read them, never write there. `al gen` overwrites every generated file and refuses to run over a hand edit.
- Change only the layer's source: LAYER_SOURCE and files under `${layer_dir}/templates/` (to change a kit prompt, copy it there first with `${layer_dir}/bin/al eject ${layer_dir} command NAME`). These edits are not reviewed by anyone; for a reviewed change, a person runs `al improve ${layer_dir} "<request>"` instead, which makes the change in a worktree, checks and regenerates the layer, and opens a pull request.
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
- Then write the `(command …)` line that registers it: template `custom`, `#:file "templates/custom/<command-name>.md"`, a `#:description`, and `#:model-set`, `#:output` and `#:validation` as the command needs. Use only fields listed for `command` in DSL_REFERENCE and only cmd names declared in LAYER_SOURCE.


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

1. Read the closest existing commands and the `command` record in DSL_REFERENCE.
2. Research the codebase just enough to name the files and commands this problem class always involves.
3. Decide the arguments, the output type and the validation.
4. Write NEW_COMMAND_FILE.
5. Re-read it as the agent that will receive it, with no other context: fix every place where you would have to guess.
6. Write the registration line.

## Validation Commands

Codebase commands the new command may list as its validation:

- **lint**: `uv run --locked --no-default-groups --group pre-commit pre-commit run --show-diff-on-failure --color=always --all-files` — Every pre-commit hook passes on all files: ruff lint rules B, E, F, I, UP, W; ruff format; uv.lock matches pyproject.toml; codespell; whitespace and end-of-file hooks. Hooks rewrite files in place and exit non-zero when they changed something.
- **typecheck**: `uv run --locked --no-default-groups --group dev tox run -e typing` — mypy in strict mode reports no errors for src and tests/type_check.
- **test**: `TOX_ENV=py3.12 uv run --locked --no-default-groups --group dev tox run -- --junitxml=.tox/reports/junit.xml` — The pytest suite in tests passes on Python 3.12 against the locked dependency versions, with every warning treated as an error; writes a JUnit XML report.
- **build**: `uv build` — The sdist and wheel build into dist/.
- **docs**: `uv run --locked --no-default-groups --group dev tox run -e docs` — The Sphinx documentation builds to docs/_build/dirhtml with warnings treated as errors.

The new command is valid when it has every required section, uses only arguments and variables found in existing commands, and names only declared cmds.

## Report

- The path of NEW_COMMAND_FILE.
- The exact line to add to LAYER_SOURCE, in a code block, and where to add it (after the last existing `(command …)` clause).
- One example invocation with realistic arguments.
- The one-sentence request a person can give to `al improve` to have this command added through review.

Finish with a concise report in plain markdown. No preamble.
