---
description: plan-feature for <NAME>
argument-hint: [prompt]
---
## Your step

${workflow_steps}

Leave your work as uncommitted changes in the worktree. Do not commit, merge into any branch, push, tag, open a pull request, or change any ref. The runtime checks this when you finish: a step that moved HEAD or a ref, changed the main checkout, or left a file at the top of the layer directory fails, and what it did is undone.

Git in this step is for reading (`git status`, `git diff`, `git log`, `git show`) and, when the plan asks, for bringing content into the working tree (`git merge --no-commit --squash <ref>`, `git checkout <ref> -- <path>`, `git apply`), always ending uncommitted. Never run in this step: `git commit`, `git merge` without `--no-commit --squash`, `git rebase`, `git cherry-pick`, `git revert`, `git reset` to another commit, `git push`, `git tag`, `git branch`, `git switch`, `git checkout -b`, `git stash`, `git update-ref`.

Notes, scratch files and helper scripts go in `${scratch_dir}`, and nowhere else in the layer.

# Plan Feature

`plan-feature` in layer `<NAME>`: turn a feature request into a stepwise plan that another agent can implement end to end without asking a single question. Plan only; implement nothing.

## Variables

PROMPT: $1
PLAN_FILE: ${specs_dir}/feature-${adw_id}-<descriptive-name>.md

## Codebase

Gin is an HTTP web framework written in Go, imported by other Go programs as the module github.com/gin-gonic/gin. Root package gin (router, context, middleware) plus sub-packages binding, render, codec/json, ginS and internal. Shipped by pushing a git tag, which runs GoReleaser in GitHub Actions.

Languages: go. Entry document: `README.md`. Default branch: `master`.

## Instructions

- You are in `${worktree}`, a git worktree of the application. Read it, do not modify it. The only file you create is PLAN_FILE, which lives in the layer and never in the application tree. Never switch branches, push, or touch the main checkout.
- Research before writing: read `README.md`, then the Relevant Files, then the closest existing feature. Follow its patterns, naming and file layout; do not invent a new architecture. Think hard about the smallest design that delivers the user value in PROMPT. Leave out everything PROMPT does not ask for.
- Write PLAN_FILE in the Plan Format: keep every heading, replace every `<placeholder>`, add no sections (unless a codebase-specific instruction below says otherwise). `<descriptive-name>` is 3-6 lowercase hyphenated words.
- Be surgical: list only files that must change or be created, and name the function, type or block in each.
- Tests: name every test to add, with its path, under Tests, and give each a step in The change, step by step. Tests come before or with the code they cover, never after the proof is run. Checkable results are observable: each one maps to a test, a command or a named artefact.
- Keep the commands already present under `### Commands` in the Plan Format unchanged and in order; add feature-specific commands after them.
- If the feature is user-visible, name under Checkable results the artefacts that will prove it (screenshots, video, reports), where they land, and the step that produces them.
- Never plan a change under a protected path. If the feature cannot avoid one, plan everything else and add a line starting `REQUIRES HUMAN` under What is left out naming the path and the reason.
- A plan covers building and validating only: no step commits, merges, pushes, tags, releases or opens a pull request; delivery is the workflow's. Integration work (bringing in another branch or patch) is planned as 'apply onto the worktree, uncommitted', e.g. `git merge --no-commit --squash <ref>`, `git checkout <ref> -- <path>` or `git apply`.
- A new dependency is added with `(not specified — do not add dependencies without flagging it)` and justified under What could go wrong. Read a conditional document when its condition matches what you are planning.

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
# Feature: <feature name>

## What is asked and why
<what the feature does, who it is for and what they gain; the approach, and how it fits the existing design>

## What exists today
<bullet per existing file: path, why it matters here, what changes in it>

### New files
<bullet per file to create, with its purpose; "None" if none>

## The change, step by step
IMPORTANT: execute every step in order, top to bottom. The last step runs every command under How it is proven and fixes until all pass.

### <step title>
- <one concrete action per bullet: file, symbol, change>

## How it is proven

### Tests
<test path, then what it asserts; each edge case and the test that covers it>

### Commands
Execute every command from the worktree root; all must exit 0.

- `make vet` — go vet reports nothing for all packages except /examples/
- `make test` — tests of packages gin, ginS, binding, render pass with go test -v -covermode=count; writes coverage.out
<additional feature-specific commands, each with what it proves; delete this line if none>

### Checkable results
<bullet per observable result, each tied to a test, a command or a named artefact; for a user-visible result, the artefact that proves it and where it lands>

## What could go wrong
<risks; new dependencies with their justification; "Nothing known" if none>

## What is left out
<deliberate omissions; REQUIRES HUMAN lines; follow-ups; "Nothing" if none>
```

## Codebase-specific instructions

The layer's own instructions for this command (`#:focus` in `agentic.rkt`), if any. Where one says otherwise than a default above, it wins. They never change where the plan is written, the `REQUIRES HUMAN` rule or the Report section: the runtime reads those, so they are fixed.

- A feature is a new or changed exported function, method, type or option of package gin, binding or render; plan its tests in <name>_test.go beside the source, using github.com/stretchr/testify assertions.
- Plan a section for the feature in docs/doc.md, not in README.md.
- The change must compile and pass under the CI build tag sets: none, -tags nomsgpack, -tags sonic with --ldflags=-checklinkname=0, -tags go_json, and -race.
- Use any, not interface{}.

## Validation Commands

Do not run these now. They are the commands the plan embeds; the implementer runs them.

- `make vet` — go vet reports nothing for all packages except /examples/
- `make test` — tests of packages gin, ginS, binding, render pass with go test -v -covermode=count; writes coverage.out

## Report

Your final message must be ONLY the path of the file you created, on a single line, with no other text.
