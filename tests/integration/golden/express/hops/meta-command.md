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

express is a web framework for Node.js, published to the npm registry as the package express and consumed through require('express'). CommonJS JavaScript with no build step: index.js re-exports lib/express.js. Tested with mocha and supertest, linted with eslint 8, published to npm by a GitHub Actions workflow when a GitHub release is published.

- Languages: javascript
- Entry document: `Readme.md`

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

- `Readme.md`
- `lib`
- `index.js`
- `test`

Protected paths:

- `.github`
- `LICENSE`
- `.npmrc`

## Conditional Documentation

- `examples/README.md` — When adding, renaming or removing a directory under examples/
- `History.md` — When a change alters behaviour that an earlier release entry describes



## Workflow

1. Read the closest existing commands and the `command` record in DSL_REFERENCE.
2. Research the codebase just enough to name the files and commands this problem class always involves.
3. Decide the arguments, the output type and the validation.
4. Write NEW_COMMAND_FILE.
5. Re-read it as the agent that will receive it, with no other context: fix every place where you would have to guess.
6. Write the registration line.

## Validation Commands

Codebase commands the new command may list as its validation:

- **install**: `npm install` — dependencies and devDependencies resolve and install into node_modules
- **lint**: `npm run lint` — eslint reports no violation of .eslintrc.yml in any JavaScript file of the repository
- **test**: `npm run test-ci` — the whole mocha suite (test/ and test/acceptance/) passes with global-leak checking, under nyc, writing coverage/lcov.info and a text coverage table to stdout
- **test-fast**: `npm test` — the whole mocha suite (test/ and test/acceptance/) passes with global-leak checking, without coverage

The new command is valid when it has every required section, uses only arguments and variables found in existing commands, and names only declared cmds.

## Report

- The path of NEW_COMMAND_FILE.
- The exact line to add to LAYER_SOURCE, in a code block, and where to add it (after the last existing `(command …)` clause).
- One example invocation with realistic arguments.
- The one-sentence request a person can give to `al improve` to have this command added through review.

Finish with a concise report in plain markdown. No preamble.
