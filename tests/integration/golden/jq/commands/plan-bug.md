---
description: plan-bug for <NAME>
argument-hint: [prompt]
---
## Your step

${workflow_steps}

Leave your work as uncommitted changes in the worktree. Do not commit, merge into any branch, push, tag, open a pull request, or change any ref. The runtime checks this when you finish: a step that moved HEAD or a ref, changed the main checkout, or left a file at the top of the layer directory fails, and what it did is undone.

Git in this step is for reading (`git status`, `git diff`, `git log`, `git show`) and, when the plan asks, for bringing content into the working tree (`git merge --no-commit --squash <ref>`, `git checkout <ref> -- <path>`, `git apply`), always ending uncommitted. Never run in this step: `git commit`, `git merge` without `--no-commit --squash`, `git rebase`, `git cherry-pick`, `git revert`, `git reset` to another commit, `git push`, `git tag`, `git branch`, `git switch`, `git checkout -b`, `git stash`, `git update-ref`.

Notes, scratch files and helper scripts go in `${scratch_dir}`, and nowhere else in the layer.

# Plan Bug

`plan-bug` in layer `<NAME>`: turn a bug report into a plan that fixes the root cause with the smallest possible change. Plan only; implement nothing.

## Variables

PROMPT: $1
PLAN_FILE: ${specs_dir}/bug-${adw_id}-<descriptive-name>.md

## Codebase

jq is a command-line JSON processor and the C library libjq, written in portable C with no runtime dependencies. It is built with GNU autotools (autoconf, automake, libtool) and optionally links the vendored Oniguruma regex library (git submodule). The manual and website are generated from YAML with Python.

Languages: c, shell, python. Entry document: `README.md`. Default branch: `master`.

## Instructions

- You are in `${worktree}`, a git worktree of the application. Read it and run read-only commands to reproduce; do not modify it. The only file you create is PLAN_FILE, which lives in the layer and never in the application tree. Never switch branches, push, or touch the main checkout.
- Research before writing: read `README.md`, then the Relevant Files, then trace the failing path through the code.
- Reproduce first. Record the exact steps and the observed versus expected result. If you cannot reproduce, say so under How to reproduce and state what evidence the analysis rests on.
- Think hard about the cause: the earliest point where state or logic goes wrong, cited as `path:line` under Cause. A symptom is not a cause.
- Plan the minimal fix at the cause. No refactoring, no drive-by cleanups, no new abstractions.
- Tests: plan a regression test that fails before the fix and passes after it; name its path under Tests and give it a step. List any existing test that must change and why.
- Write PLAN_FILE in the Plan Format: keep every heading, replace every `<placeholder>`, add no sections (unless a codebase-specific instruction below says otherwise). `<descriptive-name>` is 3-6 lowercase hyphenated words.
- Keep the commands already present under `### Commands` in the Plan Format unchanged and in order; add the reproduction command and bug-specific commands after them. Checkable results are observable: each one maps to a test, a command or a named artefact.
- If the bug is user-visible, name under Checkable results the artefacts that will prove the fix (screenshots, video, reports) and the step that produces them.
- End How it is proven with the Proof plan table, one row per checkable result. Proven by names one of the layer's declared checks below in backticks, by its name or by its command verbatim, optionally narrowed to test ids (pytest paths or node ids under its own test directory, `-k EXPR`, `-q`/`-x`/`-v`/`-rA`, `-n N`; for raco, test files of the same collection), then the test ids it runs, each in backticks; it never names another command. When a row names test ids, narrow the command to them and add `-rA` (pytest) so its log has a line per test; the declared command alone logs only counts. Writes lists the files it writes as globs relative to the worktree with `(count)`; files for this run only go under `${proof_dir}/` and are listed as `proof:GLOB` (relative to `${proof_dir}/`), and every one of them reaches the reviewers; `-` when it writes none. Needs lists the tokens, or `none`: `tool:NAME`, `engine:podman|docker`, `network`, `model:NAME`, `secret:NAME`, `host:ID`, `approval:KIND[:ARG]`. A decision only a person may take is `approval:edit:PATH`, `approval:dependency:NAME` or a layer word such as `approval:vocabulary`. Pre-approved for this run: ${approvals}.
- Declared checks (name, then the command verbatim):
- `install` — `autoreconf -i && ./configure --disable-docs --with-oniguruma=yes --enable-static`
- `build` — `make -j"$(nproc)"`
- `test` — `make check VERBOSE=yes`
- Right after you, the runtime checks every row without an agent; a row it cannot prove here stops the run with the declaration or question, so write needs truthfully.
- Never plan a change under a protected path. If the fix cannot avoid one, plan everything else and add a line starting `REQUIRES HUMAN` under What is left out naming the path and the reason.
- A plan covers building and validating only: no step commits, merges, pushes, tags, releases or opens a pull request; delivery is the workflow's. Integration work (bringing in another branch or patch) is planned as 'apply onto the worktree, uncommitted', e.g. `git merge --no-commit --squash <ref>`, `git checkout <ref> -- <path>` or `git apply`.
- A new dependency is a last resort; add it with `(not specified — do not add dependencies without flagging it)` and justify it under What could go wrong.
- Read a conditional document when its condition matches what you are planning.

## Relevant Files

- `README.md`
- `src`
- `Makefile.am`
- `configure.ac`
- `config/m4`
- `m4`
- `scripts`
- `tests`

Protected paths (do not plan changes here):

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



## Plan Format

Codebase-specific instructions below take precedence over the defaults in this section.

```md
# Bug: <bug name>

## What is asked and why
<one paragraph: the symptoms, the expected and the actual behaviour, and the fix, with why it addresses the cause rather than the symptom>

## What exists today
<bullet per existing file: path, then why it matters here and what changes in it; then a bullet per file to create with its purpose, if any>

### How to reproduce
<numbered steps or one command; observed result; expected result>

### Cause
<the defect, cited as path:line, and the chain from it to the symptom>

## The change, step by step
IMPORTANT: execute every step in order, top to bottom.

### <step title>
- <one concrete action per bullet: file, symbol, change>

### Add regression test
- <test path, what it asserts, why it fails before the fix>

### Run the proof
- Run every command under How it is proven and fix until all pass.

## How it is proven

### Tests
<test path, then what it asserts; existing tests that change and why>

### Commands
Execute every command from the worktree root; all must exit 0.

- `make -j"$(nproc)"` — libjq and the jq binary compile and link with -Wextra -Wall
- `make check VERBOSE=yes` — Every driver in TESTS of Makefile.am passes against the built ./jq; writes test-suite.log and tests/*.log
<the reproduction command, now passing, and any bug-specific commands with what each proves>

### Checkable results
<bullet per observable result, each tied to a test, a command or a named artefact>

### Proof plan
| # | Criterion | Proven by | Writes | Needs |
|---|---|---|---|---|
| C1 | <checkable result> | `<validation command>` `<test id>` | <globs (count), proof:GLOB, or -> | <none, or need tokens> |

## What could go wrong
<risks; new dependencies with their justification; anything the implementer must know; "Nothing known" if none>

## What is left out
<REQUIRES HUMAN lines; related defects not fixed here; follow-ups; "Nothing" if none>
```

## Codebase-specific instructions

The layer's own instructions for this command (`#:focus` in `agentic.rkt`), if any. Where one says otherwise than a default above, it wins. They never change where the plan is written, the `REQUIRES HUMAN` rule or the Report section: the runtime reads those, so they are fixed.

- Add a regression test: language or builtin behaviour in tests/jq.test, regex behaviour in tests/onig.test, command-line or process behaviour in tests/shtest.
- A test in tests/jq.test is three lines: jq program, input, expected output; blank lines and lines starting with # are ignored.
- Never hand-edit src/lexer.c, src/lexer.h, src/parser.c or src/parser.h; change src/lexer.l or src/parser.y instead.
- Building and running the tests must leave no change in tracked files.

## Validation Commands

Do not run these to validate anything now (running one to reproduce the bug is fine). They are the commands the plan embeds.

- `make -j"$(nproc)"` — libjq and the jq binary compile and link with -Wextra -Wall
- `make check VERBOSE=yes` — Every driver in TESTS of Makefile.am passes against the built ./jq; writes test-suite.log and tests/*.log

## Report

Your final message must be ONLY the path of the file you created, on a single line, with no other text.
