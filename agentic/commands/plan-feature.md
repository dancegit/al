---
description: plan-feature for al
argument-hint: [prompt]
---
## Your step

${workflow_steps}

Leave your work as uncommitted changes in the worktree. Do not commit, merge into any branch, push, tag, open a pull request, or change any ref. The runtime checks this when you finish: a step that moved HEAD or a ref, changed the main checkout, or left a file at the top of the layer directory fails, and what it did is undone.

Git in this step is for reading (`git status`, `git diff`, `git log`, `git show`) and, when the plan asks, for bringing content into the working tree (`git merge --no-commit --squash <ref>`, `git checkout <ref> -- <path>`, `git apply`), always ending uncommitted. Never run in this step: `git commit`, `git merge` without `--no-commit --squash`, `git rebase`, `git cherry-pick`, `git revert`, `git reset` to another commit, `git push`, `git tag`, `git branch`, `git switch`, `git checkout -b`, `git stash`, `git update-ref`.

Notes, scratch files and helper scripts go in `${scratch_dir}`, and nowhere else in the layer.

# Plan Feature

`plan-feature` in layer `al`: turn a feature request into a phased plan that another agent can implement end to end without asking a single question. Plan only; implement nothing.

## Variables

PROMPT: $1
PLAN_FILE: ${specs_dir}/feature-${adw_id}-<descriptive-name>.md

## Codebase

al generates an agentic layer (prompts, workflows, sandbox, triggers, a dashboard) for any git repository. An LLM writes LLM.txt and agentic.rkt in a small Racket language; a compiler checks the definition and a generator produces the layer from templates. The command is tools/al (Python, run with uv); the runtime and the dashboard are Python with the standard library only.

Languages: python, racket, javascript, shell. Entry document: `README.md`. Default branch: `main`.

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
- A new dependency is added with `inline script metadata in the script that needs it, or uv run --with for a test command; there is no manifest or lock file` and justified in Notes. Read a conditional document when its condition matches what you are planning.

## Relevant Files

- `README.md`
- `racket/al`
- `tools`
- `kit/runtime`
- `kit/templates`
- `kit/schemas`
- `kit/dashboard`
- `kit/self`
- `meta`
- `racket/al/tests`
- `kit/runtime/tests`
- `kit/dashboard/tests`
- `tests/tools`
- `tests/integration`

Protected paths (do not plan changes here):

- `kit/runtime/adw_modules/review.py`
- `kit/runtime/adw_modules/ship.py`
- `kit/runtime/adw_modules/gitsafe.py`
- `kit/runtime/adw_modules/envs.py`
- `docs/demo`
- `tests/authored`
- `tests/integration/testbeds.toml`

## Conditional Documentation

- `docs/DESIGN.md` — When changing the language, the compiler or how the generator maps a definition to files
- `tests/README.md` — When adding or changing a test under tests/integration, or anything al verify does
- `kit/dashboard/README.md` — When changing anything under kit/dashboard
- `docs/AL-DSL.llms.txt` — When writing or changing any agentic.rkt or a test that contains one
- `meta/README.md` — When changing a prompt under meta/commands
- `docs/GUIDE.md` — When changing the behaviour or output of a tools/al subcommand, because the guide quotes it



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

- `PLTCOLLECTS="$PWD/racket:" raco test racket/al/tests/compiler-test.rkt` — The language rules: the compiler accepts the examples and rejects each invalid definition with the documented error
- `(cd kit/runtime && env -u AL_APP_ROOT uv run --with pytest pytest -q)` — The runtime unit tests pass, including the shipping gate, the sandbox and attempts to escape the container
- `env -u AL_APP_ROOT uv run --with pytest --with pytest-xdist --with pyyaml --with jsonschema pytest tests/tools -q -n 4` — The al command itself works: eject, in-repo layers, al improve, dashboard generation
- `(cd kit/dashboard && uv run --with pytest pytest -q tests)` — The dashboard's model and HTTP server work: unit and HTTP tests, no browser
- `env -u AL_APP_ROOT uv run --with pytest --with pyyaml --with jsonschema pytest tests/integration -q` — The default tier of full runs on the five pinned example repositories passes with the mock agent, and generated prompts equal the golden snapshots
<additional feature-specific commands, each with what it proves; delete this line if none>

## Notes
<new dependencies; artefacts that prove the user-visible result; REQUIRES HUMAN lines; deliberate omissions and follow-ups>
```

## Codebase-specific instructions

The layer's own instructions for this command (`#:focus` in `agentic.rkt`), if any. Where one says otherwise than a default above, it wins. They never change where the plan is written, the `REQUIRES HUMAN` rule or the Report section: the runtime reads those, so they are fixed.

- A feature is user-visible as tools/al output, a generated file or a dashboard screen; the plan names which, and the test that shows it.
- A change to a template under kit/templates changes generated prompts: update the snapshots with --update-golden on the integration suite and include the snapshot diff in the same change.
- The vocabulary (racket/al/private/vocab.rkt) is the single source of the schema and the language reference; both are regenerated, never hand-edited, and a vocabulary change is handed to a human.
- kit/runtime and kit/dashboard use the Python standard library only; a script that needs a third-party package declares it in its inline `# /// script` header.
- A test that needs podman, nix or a browser carries that marker and skips when the tool is missing; it never downloads one.
- No personal paths and no private repository, employer or customer names in committed files.

## Validation Commands

Do not run these now. They are the commands the plan embeds; the implementer runs them.

- `PLTCOLLECTS="$PWD/racket:" raco test racket/al/tests/compiler-test.rkt` — The language rules: the compiler accepts the examples and rejects each invalid definition with the documented error
- `(cd kit/runtime && env -u AL_APP_ROOT uv run --with pytest pytest -q)` — The runtime unit tests pass, including the shipping gate, the sandbox and attempts to escape the container
- `env -u AL_APP_ROOT uv run --with pytest --with pytest-xdist --with pyyaml --with jsonschema pytest tests/tools -q -n 4` — The al command itself works: eject, in-repo layers, al improve, dashboard generation
- `(cd kit/dashboard && uv run --with pytest pytest -q tests)` — The dashboard's model and HTTP server work: unit and HTTP tests, no browser
- `env -u AL_APP_ROOT uv run --with pytest --with pyyaml --with jsonschema pytest tests/integration -q` — The default tier of full runs on the five pinned example repositories passes with the mock agent, and generated prompts equal the golden snapshots

## Report

Your final message must be ONLY the path of the file you created, on a single line, with no other text.
