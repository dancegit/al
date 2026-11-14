---
description: prime for <NAME>
argument-hint: (none)
---

# Prime

`prime` in layer `<NAME>`: build an accurate working picture of the codebase before doing anything else. Read-only.

## Variables

WORKTREE: ${worktree}
LAYER_DIR: ${layer_dir}

## Codebase

express is a web framework for Node.js, published to the npm registry as the package express and consumed through require('express'). CommonJS JavaScript with no build step: index.js re-exports lib/express.js. Tested with mocha and supertest, linted with eslint 8, published to npm by a GitHub Actions workflow when a GitHub release is published.

- Languages: javascript
- Entry document: `Readme.md`
- Dependencies are added with: `npm install`
- Default branch: `master`

## Instructions

- Read only. Do not edit, create, install, build or run anything that changes state. Never switch branches, push, or touch the main checkout.
- Your current directory is WORKTREE, a git worktree of the application. The agentic layer lives outside it, in LAYER_DIR; anything that is not application code belongs there.
- Learn the structure from the repository itself, not from assumptions about the language or framework.
- Read enough of each relevant path to know what lives there and how the parts call each other. Skim; do not read every file.
- Learn the commands below by name. They are the only supported way to install, check, build and run this codebase.
- Note the protected paths. They are off limits without a human.
- Do not read conditional documents now. Remember each condition so you read the document when it applies.


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

1. Run `git ls-files` and read the shape of the tree.
2. Read `Readme.md`.
3. Read the entry points and the main modules under the Relevant Files.
4. Read one representative test to learn how tests are written and named here.
5. Run `git log --oneline -15` to see how recent work is described.

## Validation Commands

Do not run these now. These are every command of the codebase, with what each proves:

- **install**: `npm install` — dependencies and devDependencies resolve and install into node_modules
- **lint**: `npm run lint` — eslint reports no violation of .eslintrc.yml in any JavaScript file of the repository
- **test**: `npm run test-ci` — the whole mocha suite (test/ and test/acceptance/) passes with global-leak checking, under nyc, writing coverage/lcov.info and a text coverage table to stdout
- **test-fast**: `npm test` — the whole mocha suite (test/ and test/acceptance/) passes with global-leak checking, without coverage

## Report

Summarise your understanding in at most 15 bullets: what the codebase does, how it is laid out, where the entry points and tests are, which command proves what, and which paths are protected. State anything you expected to find and did not.

Finish with a concise report in plain markdown. No preamble.
