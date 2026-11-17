---
description: plan-chore for <NAME>
argument-hint: [prompt]
---
## Your step

${workflow_steps}

Leave your work as uncommitted changes in the worktree. Do not commit, merge into any branch, push, tag, open a pull request, or change any ref. The runtime checks this when you finish: a step that moved HEAD or a ref, changed the main checkout, or left a file at the top of the layer directory fails, and what it did is undone.

Git in this step is for reading (`git status`, `git diff`, `git log`, `git show`) and, when the plan asks, for bringing content into the working tree (`git merge --no-commit --squash <ref>`, `git checkout <ref> -- <path>`, `git apply`), always ending uncommitted. Never run in this step: `git commit`, `git merge` without `--no-commit --squash`, `git rebase`, `git cherry-pick`, `git revert`, `git reset` to another commit, `git push`, `git tag`, `git branch`, `git switch`, `git checkout -b`, `git stash`, `git update-ref`.

Notes, scratch files and helper scripts go in `${scratch_dir}`, and nowhere else in the layer.

# Plan Chore

`plan-chore` in layer `<NAME>`: turn a high-level chore prompt into a surgical plan file that another agent can execute without asking a single question. Plan only; implement nothing.

## Variables

PROMPT: $1
PLAN_FILE: ${specs_dir}/chore-${adw_id}-<descriptive-name>.md

## Codebase

express is a web framework for Node.js, published to the npm registry as the package express and consumed through require('express'). CommonJS JavaScript with no build step: index.js re-exports lib/express.js. Tested with mocha and supertest, linted with eslint 8, published to npm by a GitHub Actions workflow when a GitHub release is published.

Languages: javascript. Entry document: `Readme.md`. Default branch: `master`.

## Instructions

- You are in `${worktree}`, a git worktree of the application. Read it, do not modify it. The only file you create is PLAN_FILE, which lives in the layer and never in the application tree. Never switch branches, push, or touch the main checkout.
- Research before writing: read `Readme.md`, then the Relevant Files, then follow the code until you can name every file the chore touches.
- Think hard about the smallest change that fully satisfies PROMPT. A chore changes no behaviour users rely on; if PROMPT needs a behaviour change, say so in Notes and plan only the chore part.
- Write PLAN_FILE in the Plan Format: keep every heading, replace every `<placeholder>`, add no sections (unless a codebase-specific instruction below says otherwise). `<descriptive-name>` is 3-6 lowercase hyphenated words.
- Be surgical: list only files that must change, name the function or block in each, and leave unrelated code, formatting and dependencies alone.
- Tests: name every test to add or update, with its path. If the chore needs none, state why in Notes.
- Keep the commands already present under `## Validation Commands` in the Plan Format unchanged and in order; add chore-specific commands only after them.
- If the result is user-visible, name in Notes the artefacts that will prove it (screenshots, video, reports) and the step that produces them.
- Never plan a change under a protected path. If the chore cannot be done without one, plan everything else and add a line starting `REQUIRES HUMAN` to Notes naming the path and the reason.
- A plan covers building and validating only: no step commits, merges, pushes, tags, releases or opens a pull request; delivery is the workflow's. Integration work (bringing in another branch or patch) is planned as 'apply onto the worktree, uncommitted', e.g. `git merge --no-commit --squash <ref>`, `git checkout <ref> -- <path>` or `git apply`.
- A new dependency is added with `npm install` and listed in Notes with its justification.
- Read a conditional document when its condition matches what you are planning.

## Relevant Files

- `Readme.md`
- `lib`
- `index.js`
- `test`

Protected paths (do not plan changes here):

- `.github`
- `LICENSE`
- `.npmrc`

## Conditional Documentation

- `examples/README.md` — When adding, renaming or removing a directory under examples/
- `History.md` — When a change alters behaviour that an earlier release entry describes



## Plan Format

Codebase-specific instructions below take precedence over the defaults in this section.

```md
# Chore: <chore name>

## Chore Description
<what must be done and why, in enough detail that the prompt is no longer needed>

## Relevant Files
<bullet per existing file: path, then why it is relevant and what changes in it>

## Step by Step Tasks
IMPORTANT: execute every step in order, top to bottom.

### <step title>
- <one concrete action per bullet: file, symbol, change>

### Run validation
- Run every command under Validation Commands and fix until all pass.

## Validation Commands
Execute every command from the worktree root; all must exit 0.

- `npm run lint` — eslint reports no violation of .eslintrc.yml in any JavaScript file of the repository
- `npm run test-ci` — the whole mocha suite (test/ and test/acceptance/) passes with global-leak checking, under nyc, writing coverage/lcov.info and a text coverage table to stdout
<additional chore-specific commands, each with what it proves; delete this line if none>

## Notes
<tests added or why none; new dependencies; artefacts that prove a user-visible result; REQUIRES HUMAN lines; anything the implementer must know>
```

## Codebase-specific instructions

The layer's own instructions for this command (`#:focus` in `agentic.rkt`), if any. Where one says otherwise than a default above, it wins. They never change where the plan is written, the `REQUIRES HUMAN` rule or the Report section: the runtime reads those, so they are fixed.

- No lock file is committed: never add package-lock.json, npm-shrinkwrap.json or yarn.lock.
- A test-only addition goes in test/<object>.<member>.js (unit) or test/acceptance/<example>.js (acceptance test for examples/<example>).

## Validation Commands

Do not run these now. They are the commands the plan embeds; the implementer runs them.

- `npm run lint` — eslint reports no violation of .eslintrc.yml in any JavaScript file of the repository
- `npm run test-ci` — the whole mocha suite (test/ and test/acceptance/) passes with global-leak checking, under nyc, writing coverage/lcov.info and a text coverage table to stdout

## Report

Your final message must be ONLY the path of the file you created, on a single line, with no other text.
