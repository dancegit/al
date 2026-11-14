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

Gin is an HTTP web framework written in Go, imported by other Go programs as the module github.com/gin-gonic/gin. Root package gin (router, context, middleware) plus sub-packages binding, render, codec/json, ginS and internal. Shipped by pushing a git tag, which runs GoReleaser in GitHub Actions.

- Languages: go
- Entry document: `README.md`
- Dependencies are added with: `(not specified — do not add dependencies without flagging it)`
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

1. Run `git ls-files` and read the shape of the tree.
2. Read `README.md`.
3. Read the entry points and the main modules under the Relevant Files.
4. Read one representative test to learn how tests are written and named here.
5. Run `git log --oneline -15` to see how recent work is described.

## Validation Commands

Do not run these now. These are every command of the codebase, with what each proves:

- **lint**: `make vet` — go vet reports nothing for all packages except /examples/
- **format**: `make fmt` — rewrites every *.go file with gofmt -s -w; proves nothing, it changes files
- **test**: `make test` — tests of packages gin, ginS, binding, render pass with go test -v -covermode=count; writes coverage.out

## Report

Summarise your understanding in at most 15 bullets: what the codebase does, how it is laid out, where the entry points and tests are, which command proves what, and which paths are protected. State anything you expected to find and did not.

Finish with a concise report in plain markdown. No preamble.
