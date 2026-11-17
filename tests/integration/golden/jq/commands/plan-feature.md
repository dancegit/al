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

`plan-feature` in layer `<NAME>`: turn a feature request into a phased plan that another agent can implement end to end without asking a single question. Plan only; implement nothing.

## Variables

PROMPT: $1
PLAN_FILE: ${specs_dir}/feature-${adw_id}-<descriptive-name>.md

## Codebase

jq is a command-line JSON processor and the C library libjq, written in portable C with no runtime dependencies. It is built with GNU autotools (autoconf, automake, libtool) and optionally links the vendored Oniguruma regex library (git submodule). The manual and website are generated from YAML with Python.

Languages: c, shell, python. Entry document: `README.md`. Default branch: `master`.

## Instructions

- You are in `${worktree}`, a git worktree of the application. Read it, do not modify it. The only file you create is PLAN_FILE, which lives in the layer and never in the application tree. Never switch branches, push, or touch the main checkout.
- Research before writing: read `README.md`, then the Relevant Files, then the closest existing feature. Follow its patterns, naming and file layout; do not invent a new architecture. Think hard about the smallest design that delivers the user value in PROMPT. Leave out everything PROMPT does not ask for.
- Write PLAN_FILE in the Plan Format: keep every heading, replace every `<placeholder>`, add no sections (unless a codebase-specific instruction below says otherwise). `<descriptive-name>` is 3-6 lowercase hyphenated words.
- Be surgical: list only files that must change or be created, and name the function, type or block in each.
- Tests: name every test to add, with its path, under Testing Strategy, and give each a step in Step by Step Tasks. Tests come before or with the code they cover, never after validation. Acceptance Criteria are observable and checkable: each one maps to a test, a validation command or a named artefact.
- Keep the commands already present under `## Validation Commands` in the Plan Format unchanged and in order; add feature-specific commands after them.
- If the feature is user-visible, name in Notes the artefacts that will prove it (screenshots, video, reports), where they land, and the step that produces them.
- Never plan a change under a protected path. If the feature cannot avoid one, plan everything else and add a line starting `REQUIRES HUMAN` to Notes naming the path and the reason.
- A plan covers building and validating only: no step commits, merges, pushes, tags, releases or opens a pull request; delivery is the workflow's. Integration work (bringing in another branch or patch) is planned as 'apply onto the worktree, uncommitted', e.g. `git merge --no-commit --squash <ref>`, `git checkout <ref> -- <path>` or `git apply`.
- A new dependency is added with `(not specified — do not add dependencies without flagging it)` and justified in Notes. Read a conditional document when its condition matches what you are planning.

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
# Feature: <feature name>

## Feature Description
<what the feature does and the value it delivers>

## User Story
As a <type of user>, I want to <action or goal>, so that <benefit>.

## Problem Statement
<the problem or opportunity this feature addresses>

## Solution Statement
<the approach, and how it fits the existing design>

## Relevant Files
<bullet per existing file: path, why it is relevant, what changes in it; then, under a `### New Files` heading, a bullet per file to create with its purpose>

## Implementation Plan
### Phase 1: Foundation
<shared groundwork needed before the feature itself: types, schema, config, scaffolding>
### Phase 2: Core Implementation
<the feature's main behaviour>
### Phase 3: Integration
<wiring into existing entry points, interfaces and documentation>

## Step by Step Tasks
IMPORTANT: execute every step in order, top to bottom. The last step runs every command under Validation Commands and fixes until all pass.
### <step title>
- <one concrete action per bullet: file, symbol, change>

## Testing Strategy
### Unit Tests
<test path, then what it asserts>
### Integration Tests
<test path, then the flow it covers>
### Edge Cases
<each edge case and the test that covers it>

## Acceptance Criteria
<checkable statements, each tied to a test, command or artefact>

## Validation Commands
Execute every command from the worktree root; all must exit 0.

- `make -j"$(nproc)"` — libjq and the jq binary compile and link with -Wextra -Wall
- `make check VERBOSE=yes` — Every driver in TESTS of Makefile.am passes against the built ./jq; writes test-suite.log and tests/*.log
<additional feature-specific commands, each with what it proves; delete this line if none>

## Notes
<new dependencies; artefacts that prove the user-visible result; REQUIRES HUMAN lines; deliberate omissions and follow-ups>
```

## Codebase-specific instructions

The layer's own instructions for this command (`#:focus` in `agentic.rkt`), if any. Where one says otherwise than a default above, it wins. They never change where the plan is written, the `REQUIRES HUMAN` rule or the Report section: the runtime reads those, so they are fixed.

- Builtins that can be written in jq go in src/builtin.jq; C builtins go in src/builtin.c.
- Add tests in tests/jq.test (three lines: program, input, expected output), tests/onig.test for regex, or tests/shtest for command-line options.
- Document every user-visible builtin or option in docs/content/manual/dev/manual.yml with examples entries; they are tests.
- After changing docs/content/manual/dev/manual.yml, tests/man.test, tests/manonig.test and jq.1.prebuilt must be regenerated; they are protected paths, so say so in the plan.
- A new C source or header file must be added to LIBJQ_SRC or LIBJQ_INCS in Makefile.am.
- Public libjq symbols start with jq_ or jv_.

## Validation Commands

Do not run these now. They are the commands the plan embeds; the implementer runs them.

- `make -j"$(nproc)"` — libjq and the jq binary compile and link with -Wextra -Wall
- `make check VERBOSE=yes` — Every driver in TESTS of Makefile.am passes against the built ./jq; writes test-suite.log and tests/*.log

## Report

Your final message must be ONLY the path of the file you created, on a single line, with no other text.
