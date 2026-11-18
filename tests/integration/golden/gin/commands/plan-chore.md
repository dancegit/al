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

Gin is an HTTP web framework written in Go, imported by other Go programs as the module github.com/gin-gonic/gin. Root package gin (router, context, middleware) plus sub-packages binding, render, codec/json, ginS and internal. Shipped by pushing a git tag, which runs GoReleaser in GitHub Actions.

Languages: go. Entry document: `README.md`. Default branch: `master`.

## Instructions

- You are in `${worktree}`, a git worktree of the application. Read it, do not modify it. The only file you create is PLAN_FILE, which lives in the layer and never in the application tree. Never switch branches, push, or touch the main checkout.
- Research before writing: read `README.md`, then the Relevant Files, then follow the code until you can name every file the chore touches.
- Think hard about the smallest change that fully satisfies PROMPT. A chore changes no behaviour users rely on; if PROMPT needs a behaviour change, say so under What is left out and plan only the chore part.
- Write PLAN_FILE in the Plan Format: keep every heading, replace every `<placeholder>`, add no sections (unless a codebase-specific instruction below says otherwise). `<descriptive-name>` is 3-6 lowercase hyphenated words.
- Be surgical: list only files that must change, name the function or block in each, and leave unrelated code, formatting and dependencies alone.
- Tests: name every test to add or update, with its path. If the chore needs none, state why under What is left out.
- Keep the commands already present under `### Commands` in the Plan Format unchanged and in order; add chore-specific commands only after them. Checkable results are observable: each one maps to a command, a test or a named artefact.
- If the result is user-visible, name under Checkable results the artefacts that will prove it (screenshots, video, reports) and the step that produces them.
- Never plan a change under a protected path. If the chore cannot be done without one, plan everything else and add a line starting `REQUIRES HUMAN` under What is left out naming the path and the reason.
- A plan covers building and validating only: no step commits, merges, pushes, tags, releases or opens a pull request; delivery is the workflow's. Integration work (bringing in another branch or patch) is planned as 'apply onto the worktree, uncommitted', e.g. `git merge --no-commit --squash <ref>`, `git checkout <ref> -- <path>` or `git apply`.
- A new dependency is added with `(not specified — do not add dependencies without flagging it)` and listed under What could go wrong with its justification.
- Read a conditional document when its condition matches what you are planning.

## Relevant Files

- `README.md`
- `.`
- `binding`
- `render`
- `codec/json`
- `internal/bytesconv`
- `internal/fs`
- `ginS`
- `.`
- `binding`
- `render`
- `internal/bytesconv`
- `internal/fs`
- `ginS`
- `testdata`

Protected paths (do not plan changes here):

- `.github`
- `.goreleaser.yaml`
- `.golangci.yml`
- `codecov.yml`
- `LICENSE`
- `auth.go`
- `testdata/certificate`
- `testdata/protoexample/test.pb.go`

## Conditional Documentation

- `docs/doc.md` — When adding or changing a feature (it must be documented there), or when working with the build tags jsoniter, go_json, sonic, nomsgpack
- `CONTRIBUTING.md` — When preparing a pull request
- `.github/PULL_REQUEST_TEMPLATE.md` — When writing a pull request description
- `ginS/README.md` — When changing anything under ginS/
- `BENCHMARKS.md` — When changing router performance in tree.go
- `CHANGELOG.md` — When checking in which release a feature or fix shipped



## Plan Format

Codebase-specific instructions below take precedence over the defaults in this section.

```md
# Chore: <chore name>

## What is asked and why
<what must be done and why, in enough detail that the prompt is no longer needed>

## What exists today
<bullet per existing file: path, then why it matters here and what changes in it>

## The change, step by step
IMPORTANT: execute every step in order, top to bottom.

### <step title>
- <one concrete action per bullet: file, symbol, change>

### Run the proof
- Run every command under How it is proven and fix until all pass.

## How it is proven

### Commands
Execute every command from the worktree root; all must exit 0.

- `make vet` — go vet reports nothing for all packages except /examples/
- `make test` — tests of packages gin, ginS, binding, render pass with go test -v -covermode=count; writes coverage.out
<additional chore-specific commands, each with what it proves; delete this line if none>

### Checkable results
<bullet per observable result, each tied to a command, a test or a named artefact>

## What could go wrong
<risks; new dependencies with their justification; anything the implementer must know; "Nothing known" if none>

## What is left out
<tests not added and why; REQUIRES HUMAN lines; follow-ups; "Nothing" if none>
```

## Codebase-specific instructions

The layer's own instructions for this command (`#:focus` in `agentic.rkt`), if any. Where one says otherwise than a default above, it wins. They never change where the plan is written, the `REQUIRES HUMAN` rule or the Report section: the runtime reads those, so they are fixed.

- A code change adds or modifies tests in <name>_test.go in the same directory as the changed <name>.go.
- Use any, not interface{}.

## Validation Commands

Do not run these now. They are the commands the plan embeds; the implementer runs them.

- `make vet` — go vet reports nothing for all packages except /examples/
- `make test` — tests of packages gin, ginS, binding, render pass with go test -v -covermode=count; writes coverage.out

## Report

Your final message must be ONLY the path of the file you created, on a single line, with no other text.
