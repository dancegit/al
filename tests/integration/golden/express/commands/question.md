---
description: Answer a question about the codebase without changing it
argument-hint: [question]
---
## Your step

${workflow_steps}

Leave your work as uncommitted changes in the worktree. Do not commit, merge into any branch, push, tag, open a pull request, or change any ref. The runtime checks this when you finish: a step that moved HEAD or a ref, changed the main checkout, or left a file at the top of the layer directory fails, and what it did is undone.

Git in this step is for reading (`git status`, `git diff`, `git log`, `git show`) and, when the plan asks, for bringing content into the working tree (`git merge --no-commit --squash <ref>`, `git checkout <ref> -- <path>`, `git apply`), always ending uncommitted. Never run in this step: `git commit`, `git merge` without `--no-commit --squash`, `git rebase`, `git cherry-pick`, `git revert`, `git reset` to another commit, `git push`, `git tag`, `git branch`, `git switch`, `git checkout -b`, `git stash`, `git update-ref`.

Notes, scratch files and helper scripts go in `${scratch_dir}`, and nowhere else in the layer.

# Question

`question` in layer `<NAME>`: answer one question about the codebase from the code itself, with citations. Read-only.

## Variables

QUESTION: $1

## Codebase

express is a web framework for Node.js, published to the npm registry as the package express and consumed through require('express'). CommonJS JavaScript with no build step: index.js re-exports lib/express.js. Tested with mocha and supertest, linted with eslint 8, published to npm by a GitHub Actions workflow when a GitHub release is published.

- Languages: javascript
- Entry document: `Readme.md`

## Instructions

- Read-only. Do not edit, create, install, build, commit or run anything that changes state. Never switch branches, push, or touch the main checkout.
- Answer from what is in `${worktree}` now: code first, then tests, then documentation. When documentation and code disagree, the code is right; say that they disagree.
- Search before you answer: `git ls-files` and `git grep` to locate, then read the code around each hit. Do not answer from the file name or from general knowledge of the language or framework.
- Cite every factual claim as `path:line` (or `path:start-end`), with paths relative to the worktree root. A claim you cannot cite is marked as an inference.
- Answer the question that was asked. No tour of the codebase, no unrequested advice, no plan.
- If QUESTION asks for a change, do not make it: explain what would have to change and where, and say that a planning command is the way to request it.
- If the answer is not in the repository, say so plainly and name where you looked.
- Read a conditional document when its condition matches the question.


## Relevant Files

- `Readme.md`
- `lib`
- `index.js`
- `test`

## Conditional Documentation

- `examples/README.md` — When adding, renaming or removing a directory under examples/
- `History.md` — When a change alters behaviour that an earlier release entry describes



## Workflow

1. Restate QUESTION in one line to fix its scope.
2. Locate the relevant code with `git ls-files` and `git grep`.
3. Read it, and the tests that exercise it.
4. Answer, citing as you go.

## Validation Commands

None to run: this command changes nothing. For reference when the question is about how something is checked or run:

- **install**: `npm install` — dependencies and devDependencies resolve and install into node_modules
- **lint**: `npm run lint` — eslint reports no violation of .eslintrc.yml in any JavaScript file of the repository
- **test**: `npm run test-ci` — the whole mocha suite (test/ and test/acceptance/) passes with global-leak checking, under nyc, writing coverage/lcov.info and a text coverage table to stdout
- **test-fast**: `npm test` — the whole mocha suite (test/ and test/acceptance/) passes with global-leak checking, without coverage

## Report

- The direct answer in one or two sentences.
- Supporting detail as bullets, each with its `path:line` citation.
- Inferences and unknowns, labelled as such.

Finish with a concise report in plain markdown. No preamble.
