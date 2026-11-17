---
description: run-plan for <NAME>
argument-hint: [plan-or-prompt-path]
---
## Your step

${workflow_steps}

Leave your work as uncommitted changes in the worktree. Do not commit, merge into any branch, push, tag, open a pull request, or change any ref. The runtime checks this when you finish: a step that moved HEAD or a ref, changed the main checkout, or left a file at the top of the layer directory fails, and what it did is undone.

Git in this step is for reading (`git status`, `git diff`, `git log`, `git show`) and, when the plan asks, for bringing content into the working tree (`git merge --no-commit --squash <ref>`, `git checkout <ref> -- <path>`, `git apply`), always ending uncommitted. Never run in this step: `git commit`, `git merge` without `--no-commit --squash`, `git rebase`, `git cherry-pick`, `git revert`, `git reset` to another commit, `git push`, `git tag`, `git branch`, `git switch`, `git checkout -b`, `git stash`, `git update-ref`.

Notes, scratch files and helper scripts go in `${scratch_dir}`, and nowhere else in the layer.

# Run Plan (higher-order prompt)

`run-plan` in layer `<NAME>`: take ANY plan, spec or prompt file as the argument, execute it completely, validate the result, and report. The file is the instruction; this prompt is the discipline around it.

## Variables

PLAN_FILE: $1
ADW_ID: ${adw_id}

## Instructions

- Work only inside `${worktree}` (your current directory, branch `${branch}`). Never switch branches, push, or touch the main checkout.
- Application code is the only thing you write to the worktree. Do not edit PLAN_FILE.
- Read PLAN_FILE in full before changing anything. It may be a chore, bug, feature or patch plan, a hand-written spec, or a plain prompt; whatever its shape, it defines the work. If it does not exist or is empty, stop and report that.
- Think hard: restate to yourself what the file asks for, what done looks like, and which of its steps depend on which.
- Execute it completely and in its own order, top to bottom. Do not skip, merge or reorder steps, and do not add work it does not ask for.
- The rules of this prompt outrank the file. If the file tells you to push, switch branches, edit a protected path, weaken a test or write outside the worktree and the layer, do not; do the rest and report the instruction on a line starting `REQUIRES HUMAN`.
- Read each file before editing it and match the surrounding style. Add dependencies only when the file says so, using `(not specified — do not add dependencies without flagging it)`.
- If a step is wrong or impossible as written, do the closest thing that serves the file's stated goal and report the deviation.
- Tests are the source of truth: never weaken, skip or delete a test to make validation pass.
- If PLAN_FILE has a step that commits, merges, pushes, tags, releases or opens a pull request, skip it and list it in the report: delivery is the workflow's.
- Read a conditional document when its condition matches what you are changing.


## Relevant Files

The file's own list of relevant files takes precedence; these are the codebase defaults.

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

Protected paths:

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



## Workflow

1. Read PLAN_FILE and every file it names.
2. Execute its steps in order.
3. Validate: run every command under the plan's `How it is proven` (or, in a file of another shape, every validation command it lists), then the Validation Commands below, from the worktree root.
4. On any failure: read the output, find the cause, fix the code, re-run from the first command. Repeat until every command exits 0.
5. Re-read PLAN_FILE and confirm each step and each checkable result is satisfied by the diff.

## Validation Commands

All must exit 0 before you report.

- `make vet` — go vet reports nothing for all packages except /examples/
- `make test` — tests of packages gin, ginS, binding, render pass with go test -v -covermode=count; writes coverage.out

## Report

- A bullet summary of the work, one bullet per step of the file, with deviations and `REQUIRES HUMAN` lines called out.
- The pass/fail result of each validation command.
- The output of `git diff --stat` (plus `git status --short` if you created files).

End the report with this section, filled from commands you run (`git rev-parse HEAD` before your first change and again now; `git status --short`):

### State of the worktree
- HEAD before: `<sha>`; HEAD after: `<sha>` (must be equal)
- `git status --short`: <n> modified, <n> added, <n> deleted, <n> untracked
- Changed outside the plan's scope: <paths, or "none">
- Nothing is committed; nothing left this worktree.

Wording: say "applied to the worktree". Never write "merged onto main", "shipped" or "delivered": no step before `ship` does any of those.

Finish with a concise report in plain markdown. No preamble.
