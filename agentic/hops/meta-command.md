---
description: meta-command for al
argument-hint: [problem-class-description]
---

# Meta-Command (template meta-prompt)

`meta-command` in layer `al`: turn the description of a recurring class of problem into a NEW reusable command for this layer, so the problem is solved by a template from now on instead of by hand.

## Variables

PROBLEM_CLASS: $1
COMMANDS_DIR: ${layer_dir}/commands
NEW_COMMAND_FILE: ${layer_dir}/templates/custom/<command-name>.md
LAYER_SOURCE: ${layer_dir}/agentic.rkt
DSL_REFERENCE: ${layer_dir}/AL-DSL.llms.txt

## Codebase

al generates an agentic layer (prompts, workflows, sandbox, triggers, a dashboard) for any git repository. An LLM writes LLM.txt and agentic.rkt in a small Racket language; a compiler checks the definition and a generator produces the layer from templates. The command is tools/al (Python, run with uv); the runtime and the dashboard are Python with the standard library only.

- Languages: python, racket, javascript, shell
- Entry document: `README.md`

## Instructions

- You write exactly one file, NEW_COMMAND_FILE, in the layer's `templates/custom/`. Do not edit application code and do not edit LAYER_SOURCE: you output the line to add, a human or another step adds it. Never switch branches, push, or touch the main checkout.
- COMMANDS_DIR holds generated prompts: read them, never write there. `al gen` overwrites every generated file and refuses to run over a hand edit.
- This layer is checked in with the application (`agentic/` of its repository), and ordinary runs may not change it: do not edit any file under `${layer_dir}`. Where the instructions below say to write or edit a file of the layer, write it under `${specs_dir}/proposal-${adw_id}/` instead, with the same relative path (compile a copied definition with `${layer_dir}/bin/al compile <the copy>`). A person makes the change through review with `al improve agentic "<request>"`; end your report with that command.
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
- `racket/al`
- `tools`
- `kit/runtime`
- `kit/templates`
- `kit/schemas`
- `kit/dashboard`
- `kit/self`
- `meta`
- `racket/al/tests`
- `kit/runtime/tests`
- `kit/dashboard/tests`
- `tests/tools`
- `tests/integration`

Protected paths:

- `kit/runtime/adw_modules/review.py`
- `kit/runtime/adw_modules/ship.py`
- `kit/runtime/adw_modules/gitsafe.py`
- `kit/runtime/adw_modules/envs.py`
- `docs/demo`
- `tests/authored`
- `tests/integration/testbeds.toml`

## Conditional Documentation

- `docs/DESIGN.md` — When changing the language, the compiler or how the generator maps a definition to files
- `tests/README.md` — When adding or changing a test under tests/integration, or anything al verify does
- `kit/dashboard/README.md` — When changing anything under kit/dashboard
- `docs/AL-DSL.llms.txt` — When writing or changing any agentic.rkt or a test that contains one
- `meta/README.md` — When changing a prompt under meta/commands
- `docs/GUIDE.md` — When changing the behaviour or output of a tools/al subcommand, because the guide quotes it



## Workflow

1. Read the closest existing commands and the `command` record in DSL_REFERENCE.
2. Research the codebase just enough to name the files and commands this problem class always involves.
3. Decide the arguments, the output type and the validation.
4. Write NEW_COMMAND_FILE.
5. Re-read it as the agent that will receive it, with no other context: fix every place where you would have to guess.
6. Write the registration line.

## Validation Commands

Codebase commands the new command may list as its validation:

- **test-compiler**: `PLTCOLLECTS="$PWD/racket:" raco test racket/al/tests/compiler-test.rkt` — The language rules: the compiler accepts the examples and rejects each invalid definition with the documented error
- **test-runtime**: `(cd kit/runtime && env -u AL_APP_ROOT uv run --with pytest pytest -q)` — The runtime unit tests pass, including the shipping gate, the sandbox and attempts to escape the container
- **test-tools**: `env -u AL_APP_ROOT uv run --with pytest --with pyyaml --with jsonschema pytest tests/tools -q` — The al command itself works: eject, in-repo layers, al improve, dashboard generation
- **test-dashboard**: `(cd kit/dashboard && uv run --with pytest pytest -q tests)` — The dashboard's model and HTTP server work: unit and HTTP tests, no browser
- **test-dashboard-browser**: `(cd kit/dashboard && uv run --with pytest --with playwright pytest -q tests)` — The dashboard tests plus the real-browser tests of the page in the host's Chrome
- **test-integration**: `env -u AL_APP_ROOT uv run --with pytest --with pyyaml --with jsonschema pytest tests/integration -q` — The default tier of full runs on the five pinned example repositories passes with the mock agent, and generated prompts equal the golden snapshots

The new command is valid when it has every required section, uses only arguments and variables found in existing commands, and names only declared cmds.

## Report

- The path of NEW_COMMAND_FILE.
- The exact line to add to LAYER_SOURCE, in a code block, and where to add it (after the last existing `(command …)` clause).
- One example invocation with realistic arguments.
- The one-sentence request a person can give to `al improve` to have this command added through review.

Finish with a concise report in plain markdown. No preamble.
