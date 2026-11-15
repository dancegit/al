---
description: meta-command for <NAME>
argument-hint: [problem-class-description]
---

# Meta-Command (template meta-prompt)

`new-command` in layer `<NAME>`: turn the description of a recurring class of problem into a NEW reusable command for this layer, so the problem is solved by a template from now on instead of by hand.

## Variables

PROBLEM_CLASS: $1
COMMANDS_DIR: ${layer_dir}/commands
NEW_COMMAND_FILE: ${layer_dir}/commands/<command-name>.md
LAYER_SOURCE: ${layer_dir}/agentic.rkt
DSL_REFERENCE: ${layer_dir}/AL-DSL.llms.txt

## Codebase

jq is a command-line JSON processor and the C library libjq, written in portable C with no runtime dependencies. It is built with GNU autotools (autoconf, automake, libtool) and optionally links the vendored Oniguruma regex library (git submodule). The manual and website are generated from YAML with Python.

- Languages: c, shell, python
- Entry document: `README.md`

## Instructions

- You write exactly one file, NEW_COMMAND_FILE, in the layer. Do not edit application code and do not edit LAYER_SOURCE: you output the line to add, a human or another step adds it. Never switch branches, push, or touch the main checkout.
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
- Then write the `(command …)` line that registers it: template `custom`, `#:file` relative to the layer, a `#:description`, and `#:model-set`, `#:output` and `#:validation` as the command needs. Use only fields listed for `command` in DSL_REFERENCE and only cmd names declared in LAYER_SOURCE.


## Relevant Files

- `README.md`
- `src`
- `Makefile.am`
- `configure.ac`
- `config/m4`
- `m4`
- `scripts`
- `tests`

Protected paths:

- `.github`
- `Dockerfile`
- `COPYING`
- `SECURITY.md`
- `KEYS`
- `sig`
- `scripts/version`
- `jq.spec`
- `vendor`
- `.gitmodules`
- `src/lexer.c`
- `src/lexer.h`
- `src/parser.c`
- `src/parser.h`
- `tests/man.test`
- `tests/manonig.test`
- `jq.1.prebuilt`
- `docs/Pipfile.lock`
- `docs/public/CNAME`

## Conditional Documentation

- `docs/README.md` — When changing anything under docs/, or regenerating jq.1.prebuilt, tests/man.test or tests/manonig.test
- `docs/content/manual/dev/manual.yml` — When adding or changing a builtin, operator or command-line option
- `docs/manual_schema.yml` — When editing any manual.yml
- `tests/setup` — When adding or changing a test driver script under tests/
- `.github/workflows/ci.yml` — When changing configure options or cross-compilation behaviour
- `NEWS.md` — When a change is user-visible and belongs in release notes
- `SECURITY.md` — When the task concerns a security vulnerability



## Workflow

1. Read the closest existing commands and the `command` record in DSL_REFERENCE.
2. Research the codebase just enough to name the files and commands this problem class always involves.
3. Decide the arguments, the output type and the validation.
4. Write NEW_COMMAND_FILE.
5. Re-read it as the agent that will receive it, with no other context: fix every place where you would have to guess.
6. Write the registration line.

## Validation Commands

Codebase commands the new command may list as its validation:

- **install**: `autoreconf -i && ./configure --disable-docs --with-oniguruma=yes --enable-static` — The build system is generated and configured against the system Oniguruma (libonig-dev); the vendor/oniguruma submodule is not fetched, because a run's container mounts the repository's .git read-only and git submodule update cannot register it; must run once in a fresh checkout before build or test
- **build**: `make -j"$(nproc)"` — libjq and the jq binary compile and link with -Wextra -Wall
- **test**: `make check VERBOSE=yes` — Every driver in TESTS of Makefile.am passes against the built ./jq; writes test-suite.log and tests/*.log
- **docs**: `(cd docs && pipenv sync && pipenv run python3 build_website.py)` — The website renders from docs/content into docs/output/

The new command is valid when it has every required section, uses only arguments and variables found in existing commands, and names only declared cmds.

## Report

- The path of NEW_COMMAND_FILE.
- The exact line to add to LAYER_SOURCE, in a code block, and where to add it (after the last existing `(command …)` clause).
- One example invocation with realistic arguments.

Finish with a concise report in plain markdown. No preamble.
